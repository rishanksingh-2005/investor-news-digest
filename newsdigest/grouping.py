"""Step 4, dedupe and topic grouping (LLM with a rule-based fallback)."""

from __future__ import annotations

import re
import hashlib
import json
import difflib

from . import filtering, health, models, patterns, settings, state, summarising


# ============================================================================
# STEP 4 — Dedupe
# ============================================================================
def _title_key(a: models.Article) -> str:
    t = filtering._norm(a.headline)
    t = re.sub(r"\b(the|a|an|to|of|for|and|in|on|at|as|is|are|with|by)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _url_key(a: models.Article) -> str:
    u = (a.final_url or a.url or "").lower()
    u = re.sub(r"^https?://(www\.)?", "", u).split("?")[0].split("#")[0].rstrip("/")
    return u


def dedupe(articles: list[models.Article], cfg: settings.Config) -> list[models.Article]:
    out, keys, urls = [], [], set()
    for a in articles:
        uk = _url_key(a)
        if uk and uk in urls:
            continue
        tk = _title_key(a)
        if any(tk and k and (tk == k or difflib.SequenceMatcher(None, tk, k).ratio() >= cfg.title_similarity)
               for k in keys):
            continue
        out.append(a)
        keys.append(tk)
        if uk:
            urls.add(uk)
    return out


def _is_trusted(a: models.Article) -> bool:
    u = (a.final_url or a.url or "").lower()
    return any(site in u for site in patterns.TRUSTED_SITES)


def _bullet_tokens(a: models.Article, company: models.Company) -> set:
    ctoks = set(filtering._company_tokens(company.name))
    stop = {"plc", "group", "the", "and", "its", "has", "will", "to", "of", "in",
            "for", "on", "with", "as", "is", "by", "at", "this", "that", "from",
            "be", "are", "new", "an", "a", "it", "was", "were", "amid", "over"}
    toks = set(filtering._norm(f"{a.headline} {a.bullet}").split())
    return {t for t in toks if len(t) > 2 and t not in ctoks and t not in stop}


def _distinctive(tokens: set) -> set:
    return {t for t in tokens if len(t) >= 7 or any(ch.isdigit() for ch in t)}


def _key_figures(text: str) -> set:
    """The specific figures in a bullet, as comparable values: "£40m" and
    "£40 million" are the same figure, "22%" and "22 percent" too. Small counts
    (1-9) and bare years are left out: unrelated stories share those all the time."""
    out = set()
    for value, _dec, _scale, num, pct, has_unit in summarising._figures(text):
        if not pct and not has_unit and num.is_integer() and (num < 10 or 1900 <= num <= 2100):
            continue
        out.add((round(value, 4), pct))
    return out


def _headline_tokens(a: models.Article, company: models.Company) -> set:
    ctoks = set(filtering._company_tokens(company.name))
    for alias in company.aliases:
        ctoks |= set(filtering._company_tokens(alias))
    return {t for t in filtering._norm(a.headline).split() if len(t) > 2 and t not in ctoks and t not in _MERGE_STOP}


_MERGE_STOP = {"plc", "group", "the", "and", "its", "has", "will", "for", "with", "this", "that",
               "from", "are", "new", "amid", "over", "after", "says", "said", "shares", "stock"}


def _same_event(a: models.Article, b: models.Article, company: models.Company, cfg: settings.Config) -> bool:
    if difflib.SequenceMatcher(None, (a.bullet or "").lower(),
                               (b.bullet or "").lower()).ratio() >= cfg.merge_text_ratio:
        return True
    ta, tb = _bullet_tokens(a, company), _bullet_tokens(b, company)
    if ta and tb:
        if len(ta & tb) / len(ta | tb) >= cfg.merge_token_overlap:
            return True
        if len(_distinctive(ta) & _distinctive(tb)) >= cfg.merge_distinctive:
            return True
    # Same figures = same announcement, however differently it's worded:
    # "US sales rose 22%, guidance now 7-10%" vs "US revenue jumped 22 percent;
    # sees growth of up to 10%". Two shared figures, or one plus a shared word.
    shared = _key_figures(a.bullet) & _key_figures(b.bullet)
    if len(shared) >= 2:
        return True
    # words, not the figures again ("1bn" is the shared figure, not a second clue)
    words = {t for t in (_distinctive(ta) & _distinctive(tb)) if not any(ch.isdigit() for ch in t)}
    if shared and len(words) >= 2:   # one figure + one word isn't enough: two £1bn contracts
        return True
    # Near-identical headlines (the outlets' own framing of the story).
    ha, hb = _headline_tokens(a, company), _headline_tokens(b, company)
    if len(ha) >= 3 and len(hb) >= 3 and len(ha & hb) / len(ha | hb) >= cfg.merge_headline_overlap:
        return True
    return False


def _merge_reps(reps: list, company: models.Company, cfg: settings.Config) -> list:
    """Second, deterministic merge pass over the chosen representatives — catches
    same-event duplicates the LLM grouping missed."""
    if len(reps) <= 1:
        return reps
    merged = []
    for cluster in _heuristic_groups(reps, company, cfg):
        rep = _pick_representative(cluster)
        for other in cluster:
            if other is rep:
                continue
            rep.extra_sources.append({"source": other.source, "published": other.published,
                                      "url": other.final_url or other.url})
            rep.extra_sources.extend(other.extra_sources)
            if not rep.heading and other.heading:
                rep.heading = other.heading
        merged.append(rep)
    merged.sort(key=lambda a: reps.index(a))
    return merged


def _heuristic_groups(articles: list[models.Article], company: models.Company, cfg: settings.Config) -> list[list[models.Article]]:
    n = len(articles)
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]   # path halving
            x = parent[x]
        return x
    for i in range(n):
        for j in range(i + 1, n):
            if _same_event(articles[i], articles[j], company, cfg):
                parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(articles[i])
    return list(groups.values())


def _pick_representative(group: list[models.Article]):
    if not group:
        return None
    pool = [a for a in group if _is_trusted(a)] or group
    return max(pool, key=lambda a: len(a.bullet or ""))


def _derive_heading(members: list[models.Article], company: models.Company) -> str:
    """Short topic label from the tokens shared across a cluster's bullets.
    Single-item clusters get no heading."""
    if len(members) <= 1:
        return ""
    counts = {}
    for a in members:
        for t in _bullet_tokens(a, company):
            counts[t] = counts.get(t, 0) + 1
    common = sorted((t for t, c in counts.items() if c >= 2), key=lambda t: -counts[t])[:3]
    return " ".join(w.capitalize() for w in common) if common else ""


_UNUSABLE = "\x00unusable"   # cache marker: the model's answer for this input was invalid

# The shape the grouping answer must have. Passed to the model as a JSON schema
# (constrained decoding), so even a 3B model can only return parseable JSON.
GROUPS_SCHEMA = {
    "title": "topic_groups",
    "type": "object",
    "properties": {"groups": {"type": "array", "items": {
        "type": "object",
        "properties": {"heading": {"type": "string"},
                       "members": {"type": "array", "items": {"type": "integer"}},
                       "summary": {"type": "string"}},
        "required": ["heading", "members", "summary"],
        "additionalProperties": False}}},
    "required": ["groups"],
    "additionalProperties": False,
}


def _parse_groups(txt: str, n: int) -> list[tuple]:
    """Parse and repair a grouping answer -> [(heading, summary, [member numbers])].

    Accepts {"groups": [...]} or a bare [...]. Small slips are repaired rather
    than throwing the whole answer away: out-of-range or repeated numbers are
    dropped (and that group's merged summary with them, since it may describe
    the dropped bullet), and bullets the model forgot become their own group.
    Raises ValueError if the answer isn't usable at all (no JSON, wrong shape,
    or it places fewer than half of the bullets)."""
    try:
        data = json.loads(txt)
    except ValueError:
        m = re.search(r"[\[{].*[\]}]", txt or "", re.S)
        if not m:
            raise ValueError("no JSON in response")
        data = json.loads(m.group(0))
    if isinstance(data, dict):
        data = data.get("groups")
    if not isinstance(data, list) or not all(isinstance(g, dict) for g in data):
        raise ValueError("grouping is not a list of groups")
    seen, groups = set(), []
    for g in data:
        raw = g.get("members")
        if not isinstance(raw, list):
            raise ValueError("group without a members list")
        members = []
        for x in raw:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= i <= n and i not in seen:
                seen.add(i)
                members.append(i)
        if members:
            clean = len(members) == len(raw)
            groups.append((str(g.get("heading") or "").strip(),
                           str(g.get("summary") or "").strip() if clean else "", members))
    if len(seen) * 2 < n:
        raise ValueError(f"grouping placed only {len(seen)} of {n} bullets")
    groups += [("", "", [i]) for i in range(1, n + 1) if i not in seen]
    return groups


def _llm_organise(articles: list[models.Article], company: models.Company, cfg: settings.Config):
    """Ask the model to group bullets by topic, label each group, and merge each
    group into a single clear point. Returns [(heading, summary, members), ...]."""
    listing = "\n".join(f"{i + 1}. {a.bullet}" for i, a in enumerate(articles))
    prompt = (
        f'Organise this week\'s news bullets about "{company.name}" for an investor '
        f"deciding whether the stock is worth holding. Group bullets that concern the "
        f"SAME topic or event. For each group give: a short 2-4 word \"heading\" (topic "
        f"label), the member bullet numbers, and one \"summary\" (one or two sentences, "
        f"max ~45 words) that merges the members into a single clear point, using only "
        f"facts and figures stated in its bullets. Reply with ONLY JSON like "
        f"{{\"groups\": [{{\"heading\": \"...\", \"members\": [1,2], \"summary\": \"...\"}}]}}. "
        f"Every bullet number from 1 to {len(articles)} must appear in exactly one group.\n\n{listing}"
    )
    # Same bullets -> same prompt -> reuse the answer (bullets are cached too, so a
    # re-run with nothing new skips the LLM entirely). An unusable answer is
    # remembered as well, so the same input goes straight to the heuristic next
    # time instead of paying for the same bad reply again. Network/API errors
    # raise out of _llm_complete before anything is stored, so those are retried.
    provider, model = cfg.llm_provider.lower(), health._model_name(cfg)
    key = "organise:" + hashlib.sha1(prompt.encode("utf-8")).hexdigest()
    cached = state._CACHE.get_bullet(key, provider, model)
    if cached == _UNUSABLE:
        raise ValueError("model gave an unusable grouping for these bullets before")
    txt = cached or summarising._llm_complete(prompt, cfg, max_tokens=700, schema=GROUPS_SCHEMA)
    try:
        parsed = _parse_groups(txt, len(articles))
    except (ValueError, TypeError, KeyError) as e:   # JSONDecodeError is a ValueError
        if not cached:
            state._CACHE.put_bullet(key, provider, model, _UNUSABLE)
        raise ValueError(f"unusable grouping: {e}") from e
    if not cached:
        state._CACHE.put_bullet(key, provider, model, txt)
    state._REPORT.count("topic grouping: AI (cached)" if cached else "topic grouping: AI")
    return [(h, s, [articles[i - 1] for i in members]) for h, s, members in parsed]


def _heuristic_organise(articles: list[models.Article], company: models.Company, cfg: settings.Config):
    """Deterministic fallback: cluster by text similarity, keep the best bullet."""
    groups = []
    for cluster in _heuristic_groups(articles, company, cfg):
        rep = _pick_representative(cluster)
        groups.append((_derive_heading(cluster, company), rep.bullet, cluster))
    return groups


def organise(articles: list[models.Article], company: models.Company, cfg: settings.Config) -> list[models.Article]:
    """Group a company's stories by topic, give each group a short sub-heading, and
    merge the members into one point (keeping the best source, attaching the rest as
    extra links). LLM-driven with a deterministic fallback."""
    # Filings are not grouped/merged (each is its own official document) — they
    # are passed straight through after the news points.
    filings = [a for a in articles if a.kind == "filing"]
    for i, f in enumerate(filings):
        f.heading = "SEC filings" if i == 0 else ""
    news = [a for a in articles if a.kind != "filing"]
    if not news:
        return filings
    if len(news) == 1:
        # Nothing to group — skip the LLM round-trip.
        groups = [("", "", news)]
    else:
        try:
            groups = _llm_organise(news, company, cfg)
        except Exception:
            state._REPORT.count("topic grouping: rule-based")
            groups = _heuristic_organise(news, company, cfg)

    out = []
    for heading, summary, members in groups:
        if not members:
            continue
        rep = _pick_representative(members)
        rep.heading = heading or ""
        if summary and summary != rep.bullet and cfg.verify_figures:
            # A merged summary may only use figures from its own members.
            source = "\n".join(f"{m.headline}\n{m.bullet}\n{m.snippet}\n{m.body[: cfg.max_article_chars]}"
                               for m in members)
            if summarising.unsupported_figures(summary, source):
                state._REPORT.count("summaries: replaced by number check")
                summary = ""
        if summary:
            rep.bullet = patterns._tidy_bullet(summary)
        rep.extra_sources = []
        for other in members:
            if other is rep:
                continue
            rep.extra_sources.append({"source": other.source, "published": other.published,
                                      "url": other.final_url or other.url})
        out.append(rep)
    out.sort(key=lambda a: news.index(a))
    out = _merge_reps(out, company, cfg)
    return out + filings
