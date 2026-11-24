"""Step 2, extraction: cache, then plain HTTP, then Chrome; article text and page dates."""

from __future__ import annotations

import re
import time
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
import trafilatura
from selenium.common.exceptions import WebDriverException

from . import browser, filtering, health, models, patterns, settings, state


# ============================================================================
# STEP 2 — Extraction (Selenium primary)
# ============================================================================
def _text_from_container(container) -> str:
    """Read the paragraph text inside one article container."""
    ps = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    ps = [p for p in ps if len(p) > 40]
    if not ps:  # some CMSs wrap each paragraph in a <div> instead of <p>
        ps = [d.get_text(" ", strip=True) for d in container.find_all("div", recursive=False)
              if len(d.get_text(strip=True)) > 60]
    return "\n".join(ps)


def _extract_article_text(page: str, url: str) -> str:
    """Look *inside* the article's <div>/<article> containers and read their
    paragraphs. Picks the richest matching container; falls back to whole-page
    extraction only if no container yields enough text."""
    text = ""
    try:
        soup = BeautifulSoup(page, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "header",
                         "footer", "aside", "form", "figure", "iframe"]):
            tag.decompose()
        best = ""
        for sel in patterns.ARTICLE_DIV_SELECTORS:
            for container in soup.select(sel):
                t = _text_from_container(container)
                if len(t) > len(best):
                    best = t
        text = best
    except Exception:
        text = ""
    if len(text) < 250:  # no good container — let trafilatura try the page
        try:
            t2 = trafilatura.extract(page, url=url, include_comments=False,
                                     favor_recall=True) or ""
            if len(t2) > len(text):
                text = t2
        except Exception:
            pass
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Where publishers state an article's publication date, most reliable first.
_DATE_META = [
    ("property", "article:published_time"), ("name", "article:published_time"),
    ("itemprop", "datePublished"), ("property", "og:article:published_time"),
    ("name", "parsely-pub-date"), ("name", "sailthru.date"), ("name", "pubdate"),
    ("name", "publishdate"), ("name", "publish-date"), ("name", "publication_date"),
    ("name", "dc.date.issued"), ("name", "dcterms.created"), ("name", "date"),
]


def _iso_date(value) -> str:
    """A plausible publication datetime as ISO with a timezone, else ''."""
    dt = filtering._parse_dt(str(value or "").strip())
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if dt > now + timedelta(days=1) or dt.year < 1995:
        return ""
    return dt.isoformat()


def _jsonld_dates(node):
    if isinstance(node, dict):
        if "datePublished" in node:
            yield node["datePublished"]
        for v in node.values():
            yield from _jsonld_dates(v)
    elif isinstance(node, list):
        for v in node:
            yield from _jsonld_dates(v)


def page_published(page: str) -> str:
    """The publication date an article page states about itself (meta tags,
    JSON-LD, <time>), as ISO, or ''. Google News feed dates are sometimes the
    date a story was re-indexed; the page's own date is the truth."""
    try:
        soup = BeautifulSoup(page or "", "html.parser")
    except Exception:
        return ""
    for attr, val in _DATE_META:
        el = soup.find("meta", attrs={attr: re.compile(rf"^{re.escape(val)}$", re.I)})
        iso = _iso_date(el.get("content")) if el else ""
        if iso:
            return iso
    for s in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except ValueError:
            continue
        for v in _jsonld_dates(data):
            iso = _iso_date(v)
            if iso:
                return iso
    el = soup.find("time", attrs={"itemprop": re.compile("datePublished", re.I)}) or \
        soup.find("time", attrs={"datetime": True})
    return _iso_date(el.get("datetime")) if el else ""


def _is_authentic(text: str) -> bool:
    """Authentic = a real, readable article body — not a paywall / consent /
    login stub or a near-empty shell. Such pages are left out (not summarised)."""
    if not text or len(text) < 400:
        return False
    low = text.lower()
    if len(text) <= patterns.BOT_WALL_MAX_CHARS and any(m in low for m in patterns.BOT_WALL_MARKERS):
        return False
    if sum(low.count(m) for m in patterns.JUNK_MARKERS) >= 2:
        return False
    if len(re.findall(r"[.!?]", text)) < 4:   # needs several sentences of prose
        return False
    return True


def extract_body(driver, article: models.Article, cfg: settings.Config) -> models.Article:
    """Open the story in Chrome, follow the redirect to the publisher, read the
    text from inside the article's <div> containers, and flag whether it's an
    authentic article. Fully guarded so a slow/broken page can't hang the run.
    Filings are trusted by construction; pre-filled bodies are re-checked."""
    if article.kind == "filing":
        article.authentic = True
        return article
    if article.body:
        article.authentic = _is_authentic(article.body)
        return article
    if not article.url:
        return article

    try:
        driver.get(article.url)
    except Exception:
        health._safe(lambda: driver.execute_script("window.stop()"))

    deadline = time.time() + cfg.resolve_wait
    while time.time() < deadline:
        cur = health._safe(lambda: driver.current_url)
        if cur and "news.google.com" not in cur and cur.startswith("http"):
            break
        time.sleep(0.4)

    article.final_url = health._safe(lambda: driver.current_url) or article.url
    health._safe(lambda: driver.execute_script("window.stop()"))
    page = health._safe(lambda: driver.page_source)
    if not page:
        return article

    article.body = patterns._clean_body(_extract_article_text(page, article.final_url))[: cfg.max_article_chars]
    article.authentic = _is_authentic(article.body)
    article.page_published = page_published(page)
    return article


_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _orig_key(a: models.Article) -> str:
    """Cache key for a story: its discovery URL (stable across reruns)."""
    u = (a.url or "").lower()
    return re.sub(r"^https?://(www\.)?", "", u).split("#")[0].rstrip("/")


def _needs_browser(a: models.Article) -> bool:
    """Google News links are JavaScript redirects — only a real browser can
    follow them to the publisher. Everything else can be tried over plain HTTP."""
    target = a.final_url or a.url
    return "news.google.com" in urllib.parse.urlparse(target).netloc


def extract_http(article: models.Article, cfg: settings.Config) -> bool:
    """Fast path: fetch the page with plain HTTP (no browser) and extract the
    article text. Returns True if a readable, authentic body was found; on
    False the caller falls back to Chrome."""
    url = article.final_url or article.url
    if not url or _needs_browser(article):
        return False
    req = urllib.request.Request(url, headers={
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=cfg.page_load_timeout)
        headers = getattr(resp, "headers", None)
        ctype = (headers.get("Content-Type", "") if headers else "") or "text/html"
        if "html" not in ctype and "xml" not in ctype:
            return False
        raw = resp.read(3_000_000)
        charset = (headers.get_content_charset() if headers else None) or "utf-8"
        page = raw.decode(charset, "replace")
        final = getattr(resp, "geturl", lambda: url)() or url
    except Exception:
        return False
    body = patterns._clean_body(_extract_article_text(page, final))[: cfg.max_article_chars]
    if not _is_authentic(body):
        return False
    article.final_url, article.body, article.authentic = final, body, True
    article.page_published = page_published(page)
    return True


_DRIVER_LOCK = threading.Lock()   # start Chromes one at a time (driver download is not thread-safe)


def _extract_with_browsers(articles: list[models.Article], cfg: settings.Config) -> None:
    """Open stories in a small pool of Chrome windows (``cfg.browser_workers``).
    Each worker owns one driver, restarts it if it dies, and quits it at the end.
    If Chrome can't start at all, the remaining stories are simply left unread."""
    if not articles:
        return
    work: queue.Queue = queue.Queue()
    for a in articles:
        work.put(a)
    failed_start = threading.Event()

    def worker():
        driver = None
        try:
            while not failed_start.is_set():
                try:
                    a = work.get_nowait()
                except queue.Empty:
                    return
                if driver is None or not health._driver_alive(driver):
                    if driver is not None:
                        health._safe(driver.quit)
                    try:
                        with _DRIVER_LOCK:
                            driver = browser.build_driver(cfg)
                    except Exception as e:
                        print(f"   Chrome failed to start ({e.__class__.__name__}: {e}) — "
                              f"skipping stories that need a browser")
                        state._REPORT.source("Chrome", "error", f"failed to start: {e.__class__.__name__}")
                        state._REPORT.warn("Chrome failed to start: Google News stories could not be read")
                        failed_start.set()
                        return
                try:
                    extract_body(driver, a, cfg)
                except WebDriverException:
                    health._safe(driver.quit)
                    driver = None
                time.sleep(cfg.polite_delay)
        finally:
            if driver is not None:
                health._safe(driver.quit)

    n = max(1, min(cfg.browser_workers, len(articles)))
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def extract_all(articles: list[models.Article], cfg: settings.Config) -> dict:
    """Step 2 for every candidate of every company, fastest route first:
        1. cache  — already resolved + extracted in an earlier run
        2. HTTP   — plain fetch for non-Google links, in parallel
        3. Chrome — Google redirects and anything HTTP couldn't read, in parallel
    Results (including failures, retried after a day) are written to the cache.
    Returns counts per route for the run log."""
    stats = {"cached": 0, "http": 0, "browser": 0, "unreadable": 0}
    todo = []
    for a in articles:
        if a.kind == "filing":
            a.authentic = True
            continue
        hit = state._CACHE.get_article(_orig_key(a), cfg.article_cache_days * 86400,
                                 cfg.failed_article_retry_hours * 3600)
        if hit is not None:
            a.final_url = hit["final_url"] or a.final_url
            a.body = hit["body"] or ""
            # re-checked so a stricter check also applies to pages cached before it
            a.authentic = bool(hit["authentic"]) and _is_authentic(a.body)
            a.page_published = hit.get("published", "")
            stats["cached"] += 1
            continue
        todo.append(a)

    fast = [a for a in todo if cfg.http_fast_path and not _needs_browser(a)]
    if fast:
        with ThreadPoolExecutor(max_workers=max(1, cfg.http_workers)) as ex:
            ok = list(ex.map(lambda a: extract_http(a, cfg), fast))
        stats["http"] = sum(ok)
        fast_done = {id(a) for a, good in zip(fast, ok) if good}
    else:
        fast_done = set()

    slow = [a for a in todo if id(a) not in fast_done]
    _extract_with_browsers(slow, cfg)
    stats["browser"] = sum(1 for a in slow if a.authentic)

    for a in todo:
        state._CACHE.put_article(_orig_key(a), a.final_url, a.body if a.authentic else "", a.authentic,
                           a.page_published)
        if not a.authentic:
            stats["unreadable"] += 1
    stats["redated"] = sum(apply_page_date(a) for a in articles)
    return stats


def apply_page_date(a: models.Article) -> bool:
    """Trust the date the article page states over the feed's date when they
    disagree by more than a day (feeds sometimes carry the re-index date, so an
    old story looks new). Returns True if the date was corrected."""
    page_dt, feed_dt = filtering._parse_dt(a.page_published), filtering._parse_dt(a.published)
    if page_dt is None:
        return False
    if feed_dt is not None:
        page_dt = page_dt if page_dt.tzinfo else page_dt.replace(tzinfo=timezone.utc)
        feed_dt = feed_dt if feed_dt.tzinfo else feed_dt.replace(tzinfo=timezone.utc)
        if abs((page_dt - feed_dt).total_seconds()) <= 86400:
            return False
    a.published = a.page_published
    return True
