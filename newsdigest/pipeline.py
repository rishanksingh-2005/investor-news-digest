"""The orchestrator: run() and the five phases of a digest."""

from __future__ import annotations

import os
import re
import time
import sys
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import delivery, extraction, filtering, grouping, health, models, publishing, settings, sources, state, storage, summarising, watchlists


# ============================================================================
# Orchestrator
# ============================================================================
def apply_page_budget(companies: list[models.Company], cfg: settings.Config) -> list[models.Company]:
    """Global page budget: keep only the most important items across the whole
    digest so the PDF stays within ~8 pages. Each Tier-1 company keeps its best
    item even if that pushes past the cap. Companies left with no items are
    dropped entirely. Mutates each company's ``articles`` and returns the
    companies that still have something to publish."""
    all_items = [(c, a) for c in companies for a in c.articles]
    if len(all_items) > cfg.max_total_items:
        protected = set()
        for c in companies:
            if c.tier == 1 and c.articles:
                protected.add(id(max(c.articles, key=lambda a: a.importance)))
        ranked = sorted(
            all_items,
            key=lambda ca: (id(ca[1]) not in protected, ca[0].tier, -ca[1].importance),
        )
        keep_ids = {id(a) for _, a in ranked[: cfg.max_total_items]} | protected
        for c in companies:
            c.articles = [a for a in c.articles if id(a) in keep_ids]
    return [c for c in companies if c.articles]


def _pmap(fn, items: list, workers: int) -> list:
    """Order-preserving parallel map over a small thread pool."""
    if not items:
        return []
    if workers <= 1 or len(items) == 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        return list(ex.map(fn, items))


def _published_keys(a: models.Article) -> list[str]:
    keys = [extraction._orig_key(a), grouping._url_key(a)]
    keys += [re.sub(r"^https?://(www\.)?", "", (e.get("url") or "").lower()).split("#")[0].rstrip("/")
             for e in a.extra_sources]
    return [k for k in keys if k]


def _open_log(cfg: settings.Config):
    """Log file for this run: <output_dir>/logs/news digest YYYY-MM-DD HHMMSS.log."""
    log_dir = os.path.join(cfg.output_dir, "logs") if cfg.log_dir is None else cfg.log_dir
    if not log_dir:
        return None
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, f"news digest {datetime.now():%Y-%m-%d %H%M%S}.log")
        return open(path, "w", encoding="utf-8")
    except OSError as e:
        print(f"(no log file: {e})")
        return None


def run(cfg: settings.Config) -> list[str]:
    """Run the whole digest. Returns the paths written (empty if nothing to publish).
    The run's health is in the module-level _REPORT afterwards (see RunReport);
    everything printed also goes to a log file."""
    state._REPORT = health.RunReport()
    state._LLM_STATE = summarising._LLMHealth()
    state._GDELT_STATE.update(disabled=False, rate_limited=0)
    log = _open_log(cfg)
    real_stdout = sys.stdout
    if log:
        sys.stdout = health._Tee(real_stdout, log)
    try:
        try:
            return _run_pipeline(cfg)
        except Exception as e:
            state._REPORT.fatal = f"crashed: {e.__class__.__name__}: {e}"
            raise
        finally:
            print("\n" + "\n".join(state._REPORT.lines()))
            if log:
                print(f"  Log : {log.name}")
    finally:
        sys.stdout = real_stdout
        if log:
            log.close()


def _run_pipeline(cfg: settings.Config) -> list[str]:
    """Work happens in phases, each spread over a small thread pool:
      1. discover + pre-filter every company     (cfg.discovery_workers)
      2. extract every candidate: cache -> HTTP -> Chrome
      3. summarise every kept story               (cfg.llm_workers)
      4. quality gate + topic grouping per company (cfg.llm_workers)
      5. page budget, publish, remember what was published
    """
    t_start = time.time()
    timings: dict = {}

    def lap(name: str, since: float) -> float:
        now = time.time()
        timings[name] = now - since
        return now

    if cfg.companies:
        print("Companies from the command line ...")
    else:
        print(f"Loading Tier {publishing._tiers_label(cfg)} companies from {os.path.basename(cfg.watchlist)} ...")
    try:
        companies = watchlists.load_companies(cfg)
    except (watchlists.WatchlistError, OSError, json.JSONDecodeError) as e:
        print(f"Watchlist problem: {e}\nRun with --check to see how the file is being read.")
        state._REPORT.fatal = f"watchlist problem: {e}"
        return []
    print(f"  -> {len(companies)} companies: {', '.join(c.name for c in companies)}\n")
    if not companies:
        print("No companies found for the selected tiers. Run with --check to see what the watchlist holds.")
        state._REPORT.fatal = "no companies for the selected tiers"
        return []

    state._CACHE = storage.Cache(cfg.cache_path if cfg.use_cache else None)
    if state._CACHE.enabled:
        print(f"Cache: {os.path.basename(cfg.cache_path)}"
              + ("  (new-only: skipping stories already published)" if cfg.new_only else ""))
    try:
        # 1. Discover. Cheap pre-filter BEFORE the slow extraction step: drop
        # blocklisted, off-topic, stale and low-quality items (judged on
        # headline + snippet) and dedupe, so we only open articles we might keep.
        t = time.time()
        print(f"[1/5] Discovering news ({cfg.discovery_workers} companies at a time) ...")

        def discover_one(c):
            raw = sources.discover(c, cfg)
            return len(raw), grouping.dedupe(filtering.filter_articles(raw, c, cfg), cfg)

        found = _pmap(discover_one, companies, cfg.discovery_workers)
        candidates = {}
        for c, (n_raw, cands) in zip(companies, found):
            candidates[id(c)] = cands
            print(f"   {c.name}: discovered {n_raw} -> {len(cands)} candidates")
        t = lap("discover", t)
        n_found = sum(n for n, _ in found)
        state._REPORT.count("stories discovered", n_found)
        if not n_found:
            # Every source came back empty for every company: almost always a
            # Google consent / rate-limit page or no network, not a quiet week.
            state._REPORT.fatal = ("no stories from any source for any company (Google consent page, "
                             "rate limit or no network? try a run without --headless)")
            print("No stories found at all.")
            return []

        # 2. Extract every candidate of every company in one go.
        everything = [a for c in companies for a in candidates[id(c)]]
        print(f"[2/5] Reading {len(everything)} articles ...")
        summarising.warm_up_llm(cfg)   # model loads while we read, ready for step 3
        stats = extraction.extract_all(everything, cfg)
        print(f"   {stats['cached']} from cache, {stats['http']} via HTTP, "
              f"{stats['browser']} via Chrome, {stats['unreadable']} unreadable")
        state._REPORT.count("articles read", stats["cached"] + stats["http"] + stats["browser"])
        state._REPORT.count("articles unreadable", stats["unreadable"])
        if stats["redated"]:
            state._REPORT.count("dates corrected from article page", stats["redated"])
        t = lap("extract", t)

        # 3. Pick what to summarise: readable, not blocklisted now that the real
        # publisher URL is known (before extraction it was a news.google.com
        # redirect), optionally not already published, capped per company.
        selected = {}
        for c in companies:
            # is_recent again: the page's own date (step 2) may show an old story
            kept = [a for a in candidates[id(c)]
                    if (a.kind == "filing" or a.authentic) and not filtering.is_blocklisted(a) and filtering.is_recent(a, cfg)]
            stale = sum(1 for a in candidates[id(c)] if a.authentic and not filtering.is_recent(a, cfg))
            if stale:
                state._REPORT.count("dropped: old story (page date)", stale)
            if cfg.new_only:
                kept = [a for a in kept if not state._CACHE.was_published(extraction._orig_key(a), grouping._url_key(a))]
            selected[id(c)] = kept[: cfg.max_articles_per_company]
        jobs = [(a, c) for c in companies for a in selected[id(c)]]
        print(f"[3/5] Summarising {len(jobs)} stories ({cfg.llm_provider}, {health._model_name(cfg)}) ...")
        bullets = _pmap(lambda ac: summarising.summarise_bullet(ac[0], ac[1], cfg), jobs, cfg.llm_workers)
        for (a, _), b in zip(jobs, bullets):
            a.bullet = b
        t = lap("summarise", t)

        # 4. Backstop quality gate, importance score, then topic grouping.
        print("[4/5] Grouping by topic ...")

        def finish(c):
            kept = [a for a in selected[id(c)] if a.kind == "filing"
                    or (not filtering.is_low_quality(a.bullet) and filtering.is_material(a.bullet, cfg)
                        and filtering.strongly_relevant(a, c) and not filtering.is_excluded(a.bullet, c))]
            for a in kept:
                a.importance = filtering.importance_score(a.bullet)
            return grouping.organise(kept, c, cfg)

        organised = _pmap(finish, companies, cfg.llm_workers)
        for c, kept in zip(companies, organised):
            c.articles = kept
            if not kept:
                c.note = "No material news in this window."
            print(f"   {c.name}: kept {len(kept)} of {len(candidates[id(c)])}")
        t = lap("organise", t)

        # 5. Page budget + publish.
        companies = apply_page_budget(companies, cfg)
        print(f"[5/5] {len(companies)} companies with material news this window.")
        if not companies:
            print("Nothing to publish.")
            if cfg.email_to and cfg.email_when_empty:
                stamp = datetime.now().strftime("%Y-%m-%d")
                delivery.send(cfg, [], delivery.subject_for(cfg, stamp, publishing._tiers_label(cfg), 0, 0),
                              f"No material news for the watchlist in {publishing._window_label(cfg)}.")
            return []

        stamp = datetime.now().strftime("%Y-%m-%d")
        # Output files are always named "news digest <date>" (see README).
        pdf = os.path.join(cfg.output_dir, f"news digest {stamp}.pdf")
        docx_path = os.path.join(cfg.output_dir, f"news digest {stamp}.docx")
        publishing.build_pdf(companies, cfg, pdf)
        publishing.build_docx(companies, cfg, docx_path)
        for c in companies:
            for a in c.articles:
                state._CACHE.mark_published(_published_keys(a), c.name, stamp)
        if cfg.email_to:
            n_items = sum(len(c.articles) for c in companies)
            delivery.send(cfg, [pdf] + ([docx_path] if cfg.email_attach_docx else []),
                          delivery.subject_for(cfg, stamp, publishing._tiers_label(cfg), len(companies), n_items),
                          delivery.digest_body(companies, publishing._window_label(cfg), state._REPORT.all_warnings()))
        lap("publish", t)
        print(f"Done in {time.time() - t_start:.0f}s  ("
              + ", ".join(f"{k} {v:.0f}s" for k, v in timings.items()) + ")")
        print(f"  PDF : {pdf}\n  DOCX: {docx_path}")
        return [pdf, docx_path]
    finally:
        state._CACHE.close()
        state._CACHE = storage.Cache(None)
