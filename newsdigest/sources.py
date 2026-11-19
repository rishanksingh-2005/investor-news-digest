"""Step 1, discovery: Google News, trusted outlets, regulated feeds, SEC EDGAR, Finnhub, GDELT."""

from __future__ import annotations

import os
import re
import time
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
import email.utils
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup

from . import extraction, filtering, health, models, patterns, settings, state


# ============================================================================
# STEP 1 — Discovery
# ============================================================================
def _google_news_rss(query: str, cfg: settings.Config, origin: str, source_override=None) -> list[models.Article]:
    """Fetch + parse a Google News RSS search. Reliable (plain XML, no JS)."""
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query) + "&hl=en-US&gl=US&ceid=US:en")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    })
    try:
        raw = state._CACHE.get_feed(url, cfg.feed_cache_minutes * 60)
        if raw is None:
            raw = health._urlopen_retry(req, timeout=20)
            root = ET.fromstring(raw)
            if root.tag != "rss":
                # e.g. a consent / rate-limit page that happens to parse as XML
                raise ValueError(f"not an RSS feed (root <{root.tag}>)")
            state._CACHE.put_feed(url, raw)           # only real feeds are cached
        else:
            root = ET.fromstring(raw)
    except Exception as e:
        print(f"      [{origin}] feed failed: {e}")
        state._REPORT.source(origin.split(" (")[0], "error", str(e))
        return []
    state._REPORT.source(origin.split(" (")[0], "ok")

    out = []
    for item in root.findall(".//item"):
        headline = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not headline or not link:
            continue
        src_el = item.find("{*}source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if source and headline.endswith(f" - {source}"):
            headline = headline[: -len(f" - {source}")].strip()
        try:
            published = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "").isoformat()
        except Exception:
            published = ""
        snippet = BeautifulSoup(item.findtext("description") or "", "html.parser").get_text(" ", strip=True)
        out.append(models.Article(headline=headline, url=link, source=source_override or source,
                            origin=origin, published=published, snippet=snippet))
        if len(out) >= cfg.max_items_per_source:
            break
    return out


def _name_query(company: models.Company, quoted: bool = True) -> str:
    """The company's search term, with any aliases OR'ed in: ("Jet2 Plc" OR "Jet2")."""
    names = filtering._names(company)
    if len(names) == 1:
        return f'"{names[0]}"' if quoted else names[0]
    return "(" + " OR ".join(f'"{n}"' for n in names) + ")"


def discover_google_news(company: models.Company, cfg: settings.Config) -> list[models.Article]:
    return _google_news_rss(f"{_name_query(company, quoted=False)} when:{cfg.days_back}d",
                            cfg, "Google News")


def discover_trusted(company: models.Company, cfg: settings.Config) -> list[models.Article]:
    """Seek the same news specifically from a basket of trusted outlets."""
    sites = " OR ".join(f"site:{s}" for s in patterns.TRUSTED_SITES)
    query = f"{_name_query(company)} ({sites}) when:{cfg.days_back}d"
    return _google_news_rss(query, cfg, "Trusted outlets")


def discover_regulated(company: models.Company, cfg: settings.Config) -> list[models.Article]:
    """Authentic / official market news for the company's exchange, pulled via the
    Google News feed with a site: filter over that market's regulated-news domains
    (RNS/Investegate for UK, SEDAR+/Cision for Canada, Actusnews for France,
    MFN/Cision for Sweden, EQS for Germany, MAYA for Israel, ASX for Australia,
    US PR wires, etc.). Works for any tier / exchange."""
    sites = patterns.exchange_sources(company.exchange)
    if not sites:
        return []
    site_filter = " OR ".join(f"site:{s}" for s in sites)
    query = f"{_name_query(company)} ({site_filter}) when:{cfg.days_back}d"
    return _google_news_rss(query, cfg, origin=f"Official / regulated ({company.exchange})")


_SEC_DEFAULT_UA = "NewsDigest research contact@example.com"
state._SEC_UA_WARNED = False


def sec_user_agent(warn: bool = True) -> str:
    """SEC EDGAR requires automated clients to identify themselves with a name and
    a contact email (sec.gov/os/accessing-edgar-data), e.g.
    SEC_USER_AGENT=Jane Smith jane@example.com. Each user sets their own in .env.
    Without one, a generic placeholder is sent, which the SEC may throttle or block."""
    ua = os.environ.get("SEC_USER_AGENT", "").strip().strip("\"'").strip()
    if ua and re.search(r"\S+@\S+\.\S+", ua):
        return ua
    if warn and not state._SEC_UA_WARNED:
        state._SEC_UA_WARNED = True
        why = "has no contact email" if ua else "isn't set"
        print(f"      [SEC EDGAR] SEC_USER_AGENT {why} — the SEC asks for 'Your Name you@domain.com' "
              f"in .env; requests may be refused without it")
    return ua if ua else _SEC_DEFAULT_UA


def discover_sec_edgar(company: models.Company, cfg: settings.Config) -> list[models.Article]:
    """Recent SEC filings via EDGAR full-text search (US-listed names)."""
    if not company.is_us:
        return []
    start = (datetime.now(timezone.utc) - timedelta(days=cfg.days_back)).strftime("%Y-%m-%d")
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    api = ("https://efts.sec.gov/LATEST/search-index?q="
           + urllib.parse.quote(f'"{company.name}"')
           + f"&dateRange=custom&startdt={start}&enddt={end}")
    req = urllib.request.Request(api, headers={"User-Agent": sec_user_agent(),
                                               "Accept": "application/json"})
    try:
        raw = state._CACHE.get_feed(api, cfg.feed_cache_minutes * 60)
        if raw is None:
            # EDGAR full-text search throws occasional 5xx — _urlopen_retry retries once
            raw = health._urlopen_retry(req, timeout=20, backoff=2.0)
            data = json.loads(raw.decode("utf-8", "replace"))
            state._CACHE.put_feed(api, raw)
        else:
            data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        print(f"      [SEC EDGAR] failed: {e}")
        state._REPORT.source("SEC EDGAR", "error", str(e))
        return []
    state._REPORT.source("SEC EDGAR", "ok")

    # Each hit is one *document* (main form or an exhibit), so a single 8-K can
    # appear many times. Keep one entry per filing (accession number), preferring
    # the main form document over its exhibits.
    by_filing: dict = {}
    for h in (data.get("hits", {}).get("hits", []) or []):
        src = h.get("_source", {}) or {}
        acc, _, fn = (h.get("_id") or "").partition(":")
        acc = src.get("adsh") or acc
        if not acc:
            continue
        roots = src.get("root_forms") or []
        form = src.get("form") or (roots[0] if roots else "") or "Filing"
        is_main = (src.get("file_type") or "") == form
        if acc in by_filing and not is_main:
            continue
        by_filing[acc] = (src, fn, form)

    # Full-text search also matches *other* filers that merely mention the company
    # (fund holdings reports, counterparties...). Keep only the company's own filings.
    toks = filtering._company_tokens(company.name)
    distinctive = [t for t in toks if len(t) >= 4] or toks

    out = []
    for acc, (src, fn, form) in by_filing.items():
        if len(out) >= cfg.max_items_per_source:
            break
        names = src.get("display_names") or []
        if names and not any(t in filtering._norm(n) for n in names for t in distinctive):
            continue
        who = re.sub(r"(\s*\([^)]*\))+\s*$", "", names[0]).strip() if names else company.name
        ciks = src.get("ciks") or []
        cik = str(ciks[0] if ciks else "").lstrip("0")
        link = ""
        if cik:
            link = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{fn or ''}"
        desc = (src.get("file_description") or "").strip()
        out.append(models.Article(headline=f"SEC {form} filing — {who}", url=link, final_url=link,
                           source="SEC EDGAR", origin="SEC EDGAR", kind="filing",
                           published=src.get("file_date", ""), snippet=desc))
    return out


state._FINNHUB_SYMBOL_CACHE: dict = {}
state._FINNHUB_DISABLED = False   # set once per run: no key, or the key was rejected
FINNHUB_API = "https://finnhub.io/api/v1"

# Values people leave in .env from a template, which mean "no key".
_KEY_PLACEHOLDERS = {"changeme", "change_me", "xxx", "none", "null", "todo", "key", "apikey", "api_key"}


def env_key(name: str) -> str:
    """An API key from the environment / .env, cleaned of the usual paste mistakes:
    surrounding whitespace or quotes, and template placeholders ("your_key_here")."""
    v = os.environ.get(name, "").strip().strip("\"'").strip()
    low = v.lower()
    if low in _KEY_PLACEHOLDERS or low.startswith(("your", "<", "paste", "insert")):
        return ""
    return v


class _FinnhubAuthError(Exception):
    """401: the key itself is missing or invalid -> stop using Finnhub this run."""


class _FinnhubNoAccess(Exception):
    """403: valid key, but this symbol/endpoint isn't in the key's plan (the free
    plan covers US listings only) -> skip just this company."""


def _finnhub_get(path: str, params: dict, key: str):
    """GET a Finnhub endpoint. The key goes in the X-Finnhub-Token header, never
    the URL, so it can't leak into logs, error messages or the cache. Retries
    once on 429 (free plan: 60 calls/minute)."""
    url = f"{FINNHUB_API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"X-Finnhub-Token": key, "Accept": "application/json"})
    try:
        return json.loads(health._urlopen_retry(req, timeout=20, backoff=5.0).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise _FinnhubAuthError(f"HTTP {e.code}") from e
        if e.code == 403:
            raise _FinnhubNoAccess(f"HTTP {e.code}") from e
        raise


def _clean_ticker(t: str) -> str:
    """'NASDAQ:AAPL' -> 'AAPL'; anything that doesn't look like a symbol -> ''."""
    t = (t or "").strip().upper().split(":")[-1].strip()
    return t if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,11}", t) else ""


_FINNHUB_MAX_QUERY = 20   # /search answers HTTP 422 for longer queries


def _finnhub_queries(name: str) -> list[str]:
    """Search strings for a company, best first, each within Finnhub's length limit:
    'Seaport Entertainment Group Inc.' -> ['Seaport', ...]; legal suffixes dropped."""
    toks = filtering._company_tokens(name)
    out = []
    for n in range(len(toks), 0, -1):
        q = " ".join(toks[:n])
        if len(q) <= _FINNHUB_MAX_QUERY and q not in out:
            out.append(q)
    return out[:2] or [toks[0][:_FINNHUB_MAX_QUERY]]


def _finnhub_match(name: str, results: list) -> str:
    """The plain (North American) symbol whose registered name contains every
    distinctive word of ours. No match -> '' (better no news than another
    company's: 'Liberty Capital' must not resolve to GCI Liberty)."""
    want = set(filtering._company_tokens(name))
    for r in results:
        sym = (r.get("symbol") or "").strip()
        desc = set(filtering._norm(r.get("description") or "").split())
        if sym and "." not in sym and want <= desc:
            return sym
    return ""


def _finnhub_lookup_symbol(name: str, key: str) -> str:
    """Resolve a company name to a Finnhub symbol via the /search endpoint, only
    accepting a result whose registered name matches. Cached per run."""
    if name in state._FINNHUB_SYMBOL_CACHE:
        return state._FINNHUB_SYMBOL_CACHE[name]
    sym = ""
    try:
        for q in _finnhub_queries(name):
            try:
                results = (_finnhub_get("search", {"q": q}, key) or {}).get("result") or []
            except urllib.error.HTTPError as e:
                if e.code == 422:   # query rejected: try the next, shorter one
                    continue
                raise
            sym = _finnhub_match(name, results)
            if sym:
                break
    except (_FinnhubAuthError, _FinnhubNoAccess):
        raise
    except Exception as e:
        print(f"      [Finnhub] symbol lookup failed for '{name}': {e}")
    state._FINNHUB_SYMBOL_CACHE[name] = sym
    return sym


def discover_finnhub(company: models.Company, cfg: settings.Config) -> list[models.Article]:
    """Finnhub company-news REST API, with the user's own FINNHUB_API_KEY (free at
    finnhub.io/register). The free plan covers North America only, so other
    listings are skipped. Ticker: Config.finnhub_tickers, else the watchlist's
    ticker column, else looked up by name."""
    if state._FINNHUB_DISABLED:
        return []
    key = env_key("FINNHUB_API_KEY")
    if not key:
        # say it once, not once per company
        print("      [Finnhub] no FINNHUB_API_KEY set — skipping Finnhub (optional; get a free key "
              "at finnhub.io/register and put it in .env)")
        state._REPORT.source("Finnhub", "skipped")
        state._FINNHUB_DISABLED = True
        return []
    if not company.is_north_am:
        return []
    start = (datetime.now(timezone.utc) - timedelta(days=cfg.days_back)).strftime("%Y-%m-%d")
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        symbol = (_clean_ticker(cfg.finnhub_tickers.get(company.name, ""))
                  or _clean_ticker(company.ticker)
                  or _finnhub_lookup_symbol(company.name, key))
        if not symbol:
            print(f"      [Finnhub] no confident ticker match for '{company.name}' — skipping "
                  f"(add its ticker to the watchlist's Ticker column)")
            state._REPORT.source("Finnhub", "skipped")
            return []
        data = _finnhub_get("company-news", {"symbol": symbol, "from": start, "to": end}, key)
    except _FinnhubAuthError:
        print("      [Finnhub] FINNHUB_API_KEY was rejected (HTTP 401) — check the key in .env "
              f"({settings.command()} --doctor tests it); skipping Finnhub for this run")
        state._REPORT.source("Finnhub", "error", "API key rejected (401)")
        state._FINNHUB_DISABLED = True
        return []
    except _FinnhubNoAccess:
        print(f"      [Finnhub] '{company.name}' isn't covered by this key's plan (HTTP 403) — skipping")
        state._REPORT.source("Finnhub", "skipped")
        return []
    except Exception as e:
        print(f"      [Finnhub] failed: {e}")
        state._REPORT.source("Finnhub", "error", str(e))
        return []
    if not isinstance(data, list):   # error payloads come back as {"error": "..."}
        if isinstance(data, dict) and data.get("error"):
            print(f"      [Finnhub] {company.name}: {data['error']}")
        state._REPORT.source("Finnhub", "error", str((data or {}).get("error", "unexpected reply"))
                       if isinstance(data, dict) else "unexpected reply")
        return []
    state._REPORT.source("Finnhub", "ok")
    out = []
    for it in (data or [])[: cfg.max_items_per_source]:
        try:
            published = datetime.fromtimestamp(int(it.get("datetime", 0)), tz=timezone.utc).isoformat()
        except Exception:
            published = ""
        out.append(models.Article(headline=(it.get("headline") or "").strip(), url=it.get("url", ""),
                           source=it.get("source", "Finnhub"), origin="Finnhub",
                           published=published, snippet=it.get("summary", "") or ""))
    return out


# ---------------------------------------------------------------------------
# GDELT (optional): a free, key-less global news index. Its links point straight
# at the publisher, so they're read over plain HTTP (no Chrome) and it doesn't
# depend on Google. GDELT allows one request every 5 s per client and answers a
# faster client with HTTP 429 or a plain-text notice, so requests are spaced
# out across all threads and the source switches itself off after repeated 429s.
# ---------------------------------------------------------------------------
GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
_GDELT_LOCK = threading.Lock()
state._GDELT_LAST = [0.0]           # time of the last request (shared by all threads)
state._GDELT_STATE = {"disabled": False, "rate_limited": 0}


def _gdelt_wait(cfg: settings.Config) -> None:
    with _GDELT_LOCK:
        gap = cfg.gdelt_min_interval - (time.time() - state._GDELT_LAST[0])
        if gap > 0:
            time.sleep(gap)
        state._GDELT_LAST[0] = time.time()


def _gdelt_rate_limited(company: models.Company) -> list:
    state._GDELT_STATE["rate_limited"] += 1
    state._REPORT.source("GDELT", "error", "rate-limited (429)")
    if state._GDELT_STATE["rate_limited"] >= 2 and not state._GDELT_STATE["disabled"]:
        state._GDELT_STATE["disabled"] = True
        print("      [GDELT] rate-limited twice — skipping GDELT for the rest of this run")
    return []


def discover_gdelt(company: models.Company, cfg: settings.Config) -> list[models.Article]:
    if state._GDELT_STATE["disabled"]:
        return []
    names = filtering._names(company)[:4]
    q = " OR ".join(f'"{n}"' for n in names)
    q = (f"({q})" if len(names) > 1 else q) + " sourcelang:english"
    url = GDELT_API + "?" + urllib.parse.urlencode({
        "query": q, "mode": "artlist", "format": "json", "sort": "datedesc",
        "maxrecords": max(10, cfg.max_items_per_source * 2), "timespan": f"{cfg.days_back}d"})
    raw = state._CACHE.get_feed(url, cfg.feed_cache_minutes * 60)
    if raw is None:
        _gdelt_wait(cfg)
        req = urllib.request.Request(url, headers={"User-Agent": "news-digest/1.0", "Accept": "application/json"})
        try:
            raw = urllib.request.urlopen(req, timeout=30).read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return _gdelt_rate_limited(company)
            print(f"      [GDELT] failed: {e}")
            state._REPORT.source("GDELT", "error", str(e))
            return []
        except Exception as e:
            print(f"      [GDELT] failed: {e}")
            state._REPORT.source("GDELT", "error", str(e))
            return []
        text = raw.decode("utf-8", "replace").lstrip()
        if not text.startswith("{"):
            if "limit requests" in text.lower():
                return _gdelt_rate_limited(company)
            print(f"      [GDELT] unexpected reply: {text[:80]!r}")
            state._REPORT.source("GDELT", "error", "unexpected reply")
            return []
        state._CACHE.put_feed(url, raw)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as e:
        state._REPORT.source("GDELT", "error", f"bad JSON: {e}")
        return []
    state._GDELT_STATE["rate_limited"] = 0
    state._REPORT.source("GDELT", "ok")
    out, seen = [], set()
    for it in data.get("articles") or []:
        title, link = (it.get("title") or "").strip(), (it.get("url") or "").strip()
        if not title or not link or link in seen:
            continue
        seen.add(link)
        out.append(models.Article(headline=title, url=link, source=(it.get("domain") or "").strip(),
                           origin="GDELT", published=extraction._iso_date(it.get("seendate"))))
        if len(out) >= cfg.max_items_per_source:
            break
    return out


def discover(company: models.Company, cfg: settings.Config) -> list[models.Article]:
    steps = [
        (cfg.use_google_news, discover_google_news),
        (cfg.use_trusted_sites, discover_trusted),
        (cfg.use_regulated, discover_regulated),
        (cfg.use_sec_edgar, discover_sec_edgar),
        (cfg.use_finnhub, discover_finnhub),
        (cfg.use_gdelt, discover_gdelt),
    ]
    found = []
    for i, (enabled, fn) in enumerate(s for s in steps if s[0]):
        if i:
            # ~3 feed hits per company x 112 companies: pace them so Google News
            # doesn't start returning 429 / 503 halfway through the run.
            time.sleep(cfg.polite_delay)
        found += fn(company, cfg)
    return found
