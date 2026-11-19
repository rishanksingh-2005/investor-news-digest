"""Step 3, filters: relevance, recency, noise, promotions, admin filler, importance."""

from __future__ import annotations

import re
import email.utils
from datetime import datetime, timedelta, timezone

from . import models, patterns, settings, sources, state


# ============================================================================
# STEP 3 — Filter
# ============================================================================
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())


def _company_tokens(name: str) -> list[str]:
    stop = {"plc", "inc", "ltd", "limited", "group", "corp", "corporation", "co",
            "company", "holdings", "the", "ab", "publ", "sa", "se", "of", "and"}
    toks = [t for t in _norm(name).split() if t not in stop and len(t) > 2]
    return toks or _norm(name).split()


def is_blocklisted(a: models.Article) -> bool:
    # Match both as-is and with spaces removed, so a feed name like
    # "Simply Wall St" is caught by the "simplywall" entry.
    hay = f"{a.source} {a.final_url or a.url}".lower()
    squashed = re.sub(r"\s+", "", hay)
    return any(bad in hay or bad.replace(" ", "") in squashed for bad in patterns.SOURCE_BLOCKLIST)


def is_relevant(a: models.Article, company: models.Company) -> bool:
    if a.kind == "filing":
        return True
    lead = _norm(f"{a.headline} {a.snippet}")
    full = _norm(f"{a.headline} {a.snippet} {a.body}")
    if not full.strip():
        return False
    if not patterns.RELEVANCE_RX.search(full):
        return False
    # the company (by name, alias or ticker) should appear in the headline/lead,
    # not just buried deep in the body (kills articles that merely mention it).
    return (any(_name_mentioned(n, lead, full) for n in _names(company))
            or ticker_mentioned(f"{a.headline} {a.snippet}", company))


def _names(company: models.Company) -> list[str]:
    return [company.name] + [x for x in company.aliases if x]


def _name_mentioned(name: str, lead: str, full: str) -> bool:
    toks = _company_tokens(name)
    distinctive = [t for t in toks if len(t) >= 5] or toks
    if any(_has_word(t, lead) for t in distinctive):
        return True
    return len(set(toks)) >= 2 and sum(1 for t in set(toks) if _has_word(t, full)) >= 2


def ticker_mentioned(text: str, company: models.Company) -> bool:
    """The ticker written as a ticker in the ORIGINAL-case text: "XPEL", "$PLOW",
    "(NASDAQ: PLOW)". Only tickers of 3+ letters count; shorter ones ("RR", "BA")
    collide with ordinary words and initials."""
    t = sources._clean_ticker(company.ticker).split(".")[0]
    if len(t) < 3 or not t.isalpha():
        return False
    return re.search(rf"(?<![A-Za-z0-9])\$?{re.escape(t)}(?![A-Za-z0-9])", text or "") is not None


def is_excluded(text: str, company: models.Company) -> bool:
    """True if the text mentions one of the company's exclude terms, e.g.
    "Rolls-Royce Motor Cars" for Rolls-Royce Holdings (a different company)."""
    return any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text or "", re.I) for term in company.exclude)


def _has_word(token: str, text: str) -> bool:
    """`token` at the start of a word in `text`: 'xpel' matches "xpel's" / "xpel inc",
    but 'gamma' no longer matches inside "omegagamma" and 'trader' not in "autotrader"."""
    return re.search(rf"\b{re.escape(token)}", text) is not None


# Generic geographic / directional / descriptor words that are >=5 chars but are
# NOT distinctive on their own — a name built only from these (e.g. "North West")
# must be matched as a full phrase, or common articles that merely contain "north"
# leak in as false positives.
GENERIC_NAME_WORDS = {
    # places / directions
    "north", "south", "east", "west", "northern", "southern", "eastern", "western", "central",
    "world", "global", "united", "national", "international", "american", "british", "european",
    "atlantic", "pacific", "metro", "greater", "london", "switzerland", "america", "britain",
    "canada", "europe", "texas", "california",
    # descriptors
    "general", "standard", "first", "great", "royal", "modern", "premier", "prime", "value",
    "smart", "direct", "future", "capital", "century", "liberty", "union", "allied", "advanced",
    "applied", "integrated", "consolidated", "associated", "universal", "dynamic", "strategic",
    "creative", "quality", "superior", "precision", "specialty", "summit", "pioneer", "frontier",
    # what companies do (common words that also appear in ordinary news)
    "watches", "judges", "scientific", "security", "factory", "communications", "dynamics",
    "entertainment", "systems", "technologies", "technology", "solutions", "services", "energy",
    "resources", "industries", "partners", "products", "brands", "foods", "homes", "property",
    "properties", "financial", "insurance", "media", "network", "networks", "software", "digital",
    "health", "healthcare", "medical", "airlines", "airways", "motors", "engineering", "electric",
    "electricity", "power", "water", "mining", "metals", "materials", "chemicals", "logistics",
    "transport", "retail", "stores", "hotels", "leisure", "gaming", "sports", "fashion", "beauty",
    "beverages", "brewing", "construction", "building", "estates", "investments", "trust",
    "ventures", "research", "design", "studios", "pictures", "records", "music", "games",
    "interactive", "mobile", "telecom", "wireless", "cloud", "analytics", "robotics", "aerospace",
    "defence", "defense", "marine", "shipping", "automotive", "equipment", "machines", "tools",
    "packaging", "paper", "plastics", "steel", "glass", "textiles", "apparel", "clothing",
    "jewellery", "jewelry", "luxury", "diamond", "silver", "copper", "solar", "renewables",
    "nuclear", "therapeutics", "pharmaceuticals", "diagnostics", "devices", "instruments",
    "controls", "electronics", "semiconductor", "optical", "outdoor", "travel", "credit",
}

# Legal-form words dropped from the end of a name before phrase matching.
_LEGAL_SUFFIXES = {"plc", "inc", "ltd", "limited", "group", "corp", "corporation", "co", "company",
                   "holdings", "holding", "ab", "publ", "sa", "se", "ag", "nv", "the", "llc", "lp"}


def _name_phrase(name: str) -> str:
    """'Watches Of Switzerland Group Plc' -> 'watches of switzerland' (inner words kept)."""
    words = _norm(name).split()
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def strongly_relevant(a: models.Article, company: models.Company) -> bool:
    """Stricter, precision-first relevance gate applied AFTER summarisation, so an
    article that merely name-drops the company in passing never reaches the digest.

    Judged on the headline + lead (snippet), not the finished bullet — a good
    bullet may legitimately omit the company name, so we must not require it there.

      - If the name has a distinctive token (>=5 chars and not a generic
        geographic/descriptor word, e.g. "Douglas", "Dynamics", "Chemring"),
        that token must appear in the headline/lead.
      - Otherwise the name is only short/common words (e.g. "North West"), so the
        full name must appear as a contiguous phrase in the headline/lead — this
        stops generic articles that merely contain "north" and "west" apart.
    """
    if a.kind == "filing":
        return True
    # headline and snippet are checked separately, so a phrase can't be formed
    # across the join ("...across the north west" + "Electricity was restored")
    parts = [p for p in (_norm(a.headline), _norm(a.snippet)) if p.strip()]
    if not parts:
        return False
    return (any(_strongly_named(n, p) for n in _names(company) for p in parts)
            or ticker_mentioned(f"{a.headline} {a.snippet}", company))


def _strongly_named(name: str, lead: str) -> bool:
    """A distinctive word of the name (5+ letters, not a common word: "Xpel",
    "Douglas", "Seaport") is enough on its own. A name made only of common words
    ("Watches of Switzerland", "North West Electricity", "Liberty Capital") must
    appear as the whole phrase, or every story about watches or the north west
    would count. Short names people really use belong in the Aliases column."""
    toks = _company_tokens(name)
    distinctive = [t for t in toks if len(t) >= 5 and t not in GENERIC_NAME_WORDS]
    if distinctive:
        return any(_has_word(t, lead) for t in distinctive)
    phrase = _name_phrase(name) or " ".join(toks).strip()
    return bool(phrase) and re.search(rf"\b{re.escape(phrase)}\b", lead) is not None


def _parse_dt(iso: str):
    """ISO 8601, RFC 2822 (feeds) or compact 20261117T093000Z (GDELT) -> datetime."""
    if not iso:
        return None
    iso = str(iso).strip()
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T?(\d{2})(\d{2})(\d{2})Z?", iso)
    if m:
        return datetime(*map(int, m.groups()), tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        pass
    # Python 3.10's fromisoformat is strict: retry without fractional seconds
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?", iso)
    if m:
        tz = (m.group(3) or "+00:00").replace("Z", "+00:00")
        if len(tz) == 5:
            tz = tz[:3] + ":" + tz[3:]
        try:
            return datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}{tz}")
        except Exception:
            pass
    try:
        return email.utils.parsedate_to_datetime(iso)
    except Exception:
        return None


def is_recent(a: models.Article, cfg: settings.Config) -> bool:
    """Keep only stories published within the digest window. Unknown dates are
    kept (feeds nearly always supply one, so this only trims the stragglers)."""
    if a.kind == "filing":
        return True
    dt = _parse_dt(a.published)
    if dt is None:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.recency_grace_days)
    return dt >= cutoff


def is_no_news(text: str) -> bool:
    low = (text or "").lower()
    if any(pat in low for pat in patterns.NO_NEWS_PATTERNS):
        return True
    return any(rx.search(low) for rx in patterns.NO_NEWS_REGEXES)


# Material-signal scoring: how "important" a story is. Used to gate out fluff and
# to rank items when trimming to the page budget.
_IMP_MONEY = re.compile(r"[£$€]\s?\d")
_IMP_PCT = re.compile(r"\d+(?:\.\d+)?\s?%")
# Each group scores at most once. Every alternative is anchored to the start of
# a word, and short words must match whole ("order" not "border", "ceo" not in
# other words, "fine" not "finance", "stake" not "stakeholder").
_IMP_EVENTS = [re.compile(p) for p in [
    r"\b(?:acquisition|acquir|merger|takeover|buyback|repurchas|bids?\b|recommended (?:cash )?offer"
    r"|offer for|tender offer)",
    r"\b(?:dividend|guidance|outlook|profit warning)",
    r"\b(?:contracts?\b|orders?\b|ordered\b|order book|awarded|secured|mandates?\b|tenders?\b)",
    r"\b(?:eps\b|revenue|profit|ebitda|margin|earnings|net income|loss(?:es)?\b|pre-?tax)",
    r"\b(?:ceos?\b|cfos?\b|chief executive|chair|appoint|resign|steps? down|stepping down|succeed)",
    r"\b(?:investigation|lawsuit|probe\b|fine[sd]?\b|regulator|delist|sanction)",
    r"\b(?:placing|fundrais|rights issue|convertible|senior notes|stakes?\b|disposal|equity raise)",
    r"\b(?:launch|expan|partnership|joint venture|results\b)",
]]


def importance_score(text: str) -> int:
    low = (text or "").lower()
    s = 0
    if _IMP_MONEY.search(text or ""):
        s += 2
    if _IMP_PCT.search(text or ""):
        s += 1
    for rx in _IMP_EVENTS:
        if rx.search(low):
            s += 1
    return s


def is_material(text: str, cfg: settings.Config) -> bool:
    """Keep only stories carrying a real corporate signal; drop fluff / no-news."""
    if is_no_news(text):
        return False
    return importance_score(text) >= cfg.min_importance


# Anchored to word starts, so e.g. "won a" no longer matches inside "won and",
# "secured" inside "unsecured" or "signed" inside "designed".
_PRICE_NOISE_RX = [re.compile(rf"\b(?:{p})") for p in patterns.PRICE_NOISE_PATTERNS]
_VALUATION_RX = [re.compile(rf"\b(?:{p})") for p in patterns.VALUATION_PATTERNS]
_STRONG_EVENT_RX = [re.compile(rf"\b(?:{p})") for p in patterns.STRONG_EVENT_PATTERNS]


def is_price_noise(text: str) -> bool:
    """True if the text is dominated by share-price / valuation chatter and does
    not report a real corporate event."""
    low = (text or "").lower()
    hits = sum(1 for rx in _PRICE_NOISE_RX if rx.search(low))
    hits += sum(1 for rx in _VALUATION_RX if rx.search(low))
    if hits >= 2 and not any(rx.search(low) for rx in _STRONG_EVENT_RX):
        return True
    return False


def is_admin_filler(text: str) -> bool:
    """Routine RNS boilerplate / index-membership admin — no investment value."""
    low = (text or "").lower()
    return any(re.search(p, low) for p in patterns.ADMIN_FILLER_PATTERNS)


def is_promotional(text: str) -> bool:
    """True for consumer marketing / promo PR (offers, discounts, sales) — not
    material to whether the stock is worth holding."""
    low = (text or "").lower()
    if any(pat in low for pat in patterns.PROMO_PATTERNS):
        return True
    if any(re.search(rx, low) for rx in patterns.PROMO_REGEXES):
        return True
    return False


def is_low_quality(text: str) -> bool:
    return (is_no_news(text) or is_price_noise(text) or is_promotional(text)
            or is_admin_filler(text))


def filter_articles(articles: list[models.Article], company: models.Company, cfg: settings.Config) -> list[models.Article]:
    kept = []
    for a in articles:
        if is_blocklisted(a) or not is_relevant(a, company):
            continue
        if a.kind != "filing" and is_excluded(f"{a.headline} {a.snippet}", company):
            state._REPORT.count("dropped: exclude term")
            continue
        if not is_recent(a, cfg):
            continue
        if a.kind != "filing" and is_low_quality(f"{a.headline}  {a.snippet}"):
            continue
        kept.append(a)
    return kept
