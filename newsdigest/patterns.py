"""Source lists, keyword lists and text patterns used by the filters and cleaners."""

from __future__ import annotations

import re


# Trusted outlets we actively seek out and prefer (also used as a site: filter).
TRUSTED_SITES = [
    "reuters.com", "ft.com", "bloomberg.com", "wsj.com", "cnbc.com",
    "businesswire.com", "globenewswire.com", "prnewswire.com",
    "investegate.co.uk", "proactiveinvestors.co.uk", "sharecast.com",
]

# Authentic / official regulated-news source per exchange (the RNS-equivalent for
# each market). Queried through Google News with a site: filter, so the wire /
# aggregator domains resolve; pure regulator portals are listed too but only
# return results when Google indexes them (harmless otherwise). Extend as needed.
EXCHANGE_SOURCES = {
    "XLON": ["londonstockexchange.com", "investegate.co.uk"],           # UK — RNS
    "XPAR": ["actusnews.com", "globenewswire.com"],                      # France — AMF/Actusnews
    "XAMS": ["globenewswire.com", "actusnews.com"],                      # Netherlands — Euronext
    "XLIS": ["actusnews.com", "globenewswire.com"],                      # Portugal — Euronext
    "XSTO": ["mfn.se", "news.cision.com"],                               # Sweden — Nasdaq Nordic
    "XMIL": ["emarketstorage.it", "1info.it"],                           # Italy — CONSOB SDIR
    "XTAE": ["maya.tase.co.il"],                                         # Israel — TASE MAYA
    "XFRA": ["eqs-news.com"],                                            # Germany — EQS/DGAP
    "XSWX": ["eqs-news.com", "globenewswire.com"],                       # Switzerland — SIX
    "XWAR": ["gpw.pl", "bankier.pl"],                                    # Poland — GPW ESPI
    "XTSE": ["sedarplus.ca", "newswire.ca", "globenewswire.com", "newsfilecorp.com"],  # Canada
    "XTSX": ["sedarplus.ca", "newswire.ca", "globenewswire.com", "newsfilecorp.com"],  # Canada TSXV
    "XASX": ["asx.com.au", "globenewswire.com"],                         # Australia — ASX
    "OTCM": ["otcmarkets.com", "globenewswire.com", "prnewswire.com"],   # US OTC
}
US_EXCHANGES = {"XNYS", "XNAS", "XASE", "ARCX", "BATS"}
# Finnhub company-news only covers North America on the free tier.
NORTH_AM_EXCHANGES = {"XNYS", "XNAS", "XASE", "ARCX", "BATS", "XTSE", "XTSX", "OTCM"}
US_SOURCES = ["businesswire.com", "globenewswire.com", "prnewswire.com"]  # SEC EDGAR handled separately
DEFAULT_REGULATED_SOURCES = ["businesswire.com", "globenewswire.com", "prnewswire.com"]


def exchange_sources(exchange: str) -> list:
    ex = (exchange or "").upper()
    if ex in EXCHANGE_SOURCES:
        return EXCHANGE_SOURCES[ex]
    if ex in US_EXCHANGES:
        return US_SOURCES
    return DEFAULT_REGULATED_SOURCES

# Junk / promotional / tabloid sources we never want in a financial brief.
SOURCE_BLOCKLIST = [
    "marketbeat", "simplywall", "fool.com", "motley fool", "zacks", "tipranks",
    "stocktitan", "insider monkey", "insidermonkey", "benzinga", "seeking alpha",
    "seekingalpha", "barchart", "investorplace", "gurufocus", "wallstreetzen",
    "stockstotrade", "newser", "msn.com", "youtube", "facebook", "reddit",
    "twitter", "x.com", "tiktok",
    # tabloid / lifestyle
    "dailyrecord", "dailymail", "mirror.co.uk", "thesun", "express.co.uk",
    "metro.co.uk", "dailystar", "ok.co.uk", "hellomagazine",
    # low-authority aggregators / auto-generated / off-topic feeds
    "ad hoc news", "ad-hoc-news", "adhocnews", "wealth awesome", "wealthawesome",
    "indexbox", "traders union", "tradersunion", "minichart", "quiver quantitative",
    "quiverquant", "twelfthmagpie", "asktraders", "bez kabli", "bezkabli", "wallmine",
    "streetinsider", "india infoline", "indian awaaz", "wisesheets",
    # off-topic sports coverage (matches the Groupama-FDJ cycling team, not FDJ United)
    "procycling", "cyclinguptodate", "brujulabike", "cyclingnews", "velonews",
    "outsideonline", "tvbeurope",
]

# Material-news keywords. A story is kept only if it contains one of these.
RELEVANCE_KEYWORDS = [
    "earning", "revenue", "profit", "loss", "acquisition", "acquire", "merger",
    "merge", "takeover", "dividend", "guidance", "forecast", "results",
    "trading update", "half-year", "full-year", "interim", "contract", "order",
    "deal", "partnership", "ceo", "cfo", "chief executive", "appoint", "resign",
    "buyback", "ipo", "placing", "fundraise", "stake", "disposal", "ebitda",
    "margin", "outlook", "upgrade", "downgrade", "rating", "regulatory", "fine",
    "lawsuit", "expansion", "launch", "investment",
]

# Keywords are matched at the START of a word ("acquire" -> acquired, acquires),
# never inside one. These short ones must match the WHOLE word (plus plural /
# past-tense endings), because as prefixes they still hit the wrong words:
# "order" -> "orderly", "fine" -> "finance", "stake" -> "stakeholder",
# "deal" -> "dealer", "ipo" -> "ipod".
_WHOLE_WORD_KEYWORDS = {"order", "fine", "ipo", "ceo", "cfo", "deal", "stake", "loss"}


def _keyword_rx(kw: str) -> str:
    # spaces and hyphens are interchangeable ("half-year" == "half year"; _norm
    # turns hyphens into spaces, so a literal "half-year" never used to match)
    body = r"[\s-]+".join(re.escape(w) for w in re.split(r"[\s-]+", kw.strip()))
    return rf"\b{body}(?:s|es|d|ed)?\b" if kw in _WHOLE_WORD_KEYWORDS else rf"\b{body}"


RELEVANCE_RX = re.compile("|".join(_keyword_rx(k) for k in RELEVANCE_KEYWORDS), re.I)

# Share-price / market-data noise. If a story's lead is mostly this AND reports no
# real corporate event, it is dropped (it doesn't clear the quality bar).
PRICE_NOISE_PATTERNS = [
    r"52[\s-]?week", r"market cap", r"market close", r"closed at [£$]?[\d]",
    r"share price", r"stock price", r"one[\s-]?month return", r"trading at [£$]",
    r"\bp/e\b", r"price[\s-]?to[\s-]?earnings", r"per (diluted )?share",
    r"\beps\b", r"earnings per share", r"no trades", r"52 weeks",
    r"shares? (rose|gained|fell|dropped|slipped|jumped|climbed|were up|were down|closed|are trading)",
    r"stock (rose|gained|fell|jumped|climbed|slipped)",
    r"\d+(\.\d+)?%\s*(return|gain|loss)", r"expected eps",
]

# Hard corporate events that "rescue" an otherwise price-heavy story.
EVENT_KEYWORDS_HARD = [
    "acquisition", "acquire", "acquires", "acquiring", "merger", "takeover",
    "launch", "contract", "appoint", "resign", "dividend", "buyback", "ipo",
    "placing", "disposal", "partnership", "trading update", "fundraise", "stake",
    "joint venture", "spin-off", "regulatory", "lawsuit", "fine", "expansion",
    "invests", "investment", "guidance",
]

# Valuation / analyst chatter — counts toward "price noise" (dropped unless a real
# corporate event is present).
VALUATION_PATTERNS = [
    r"p/e ratio", r"\bp/e\b", r"price[\s-]?to[\s-]?sales", r"price[\s-]?to[\s-]?earnings",
    r"enterprise value", r"fair value", r"\bvaluation\b", r"forward price",
    r"implied volatility", r"price target", r"intrinsic value", r"undervalued",
    r"overestimated", r"profit margin", r"trades (above|below|at)", r"traded at",
    r"valued at", r"market capitali[sz]ation", r"analysts?'? (revised|forecast|estimate)",
    r"enterprise value to", r"quarter-over-quarter", r"year-over-year", r"ev/revenue",
    r"return since", r"stock price (estimate|target)", r"target price", r"ev/ebitda",
    r"price[\s-]?to[\s-]?book", r"dividend yield", r"consensus (price )?target",
    r"(buy|sell|hold|overweight|underweight|outperform|underperform) rating",
    r"reiterated", r"initiated coverage", r"analyst rating", r"raised .* target",
]

# Only a *real* corporate event rescues an otherwise price/valuation-heavy story.
STRONG_EVENT_PATTERNS = [
    r"acquired", r"to acquire", r"acquir(es|ing)", r"acquisition of", r"acquisition by",
    r"completed the acquisition", r"announce[sd]", r"launch(ed|es)", r"unveil(ed|s)",
    r"awarded", r"won a", r"secured?", r"signed", r"appoint(ed|s)", r"resign(ed|s)",
    r"\bpriced\b", r"refinanc", r"raise[sd] (its |full-year |2026 |the )?guidance",
    r"issuer bid", r"buyback programme", r"rights issue", r"special dividend",
    r"dividend of", r"higher \d{4}", r"results on", r"trading update", r"profit warning",
    r"convertible", r"credit facility", r"contract (win|renewal|with|for)",
    r"partnership with", r"joint venture", r"expand(s|ed|ing) into",
    r"\breported\b", r"\bresults\b", r"posts? (record|revenue|profit|a )",
]

# Routine regulatory boilerplate / index-membership admin — low investment value.
ADMIN_FILLER_PATTERNS = [
    r"total voting rights", r"voting rights (and|figure)", r"issued share capital",
    r"treasury shares", r"transaction in own shares", r"\bin own shares\b",
    r"block listing", r"\bpdmr\b", r"market abuse regulation", r"holdings? in company",
    r"\btr-1\b", r"form 8\.3", r"major holdings", r"(ordinary )?shares in issue",
    r"purchased [\d,]+ (ordinary )?shares", r"acquired [\d,]+ shares",
    r"director .{0,30}(sold|bought|purchased|acquired)", r"internal trades?",
    r"added to the russell", r"dropped from the russell", r"russell \d{3,4}",
    r"added to the .{0,30} index", r"removed from the .{0,30} index",
    r"msci esg", r"index reconstitution",
]


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------
# Web boilerplate lines to strip from scraped article bodies (matched on SHORT
# lines only, so real paragraphs are never removed).
BODY_JUNK_LINE_PATTERNS = [
    r"^advertisement$", r"\badvertisement\b", r"we use cookies", r"accept (all )?cookies",
    r"cookie (policy|settings|preferences|notice)", r"^sign up", r"subscribe",
    r"newsletter", r"enter your email", r"read more", r"related (articles|stories|reads)",
    r"^more:", r"see also", r"also read", r"share (this|on)", r"click to share",
    r"\b(tweet|whatsapp|linkedin|telegram|copy link)\b", r"\d+\s?min read",
    r"reading time", r"^by [a-z]+(\s[a-z.'-]+){1,3}$", r"^published", r"^updated",
    r"last updated", r"all rights reserved", r"^©", r"getty images", r"^image:",
    r"^photo:", r"^credit:", r"follow us", r"download the app", r"terms of (use|service)",
    r"privacy policy", r"^most read", r"^trending", r"^sponsored", r"^promoted",
]
# Inline phrases removed anywhere in the body.
BODY_JUNK_INLINE_PATTERNS = [
    r"\badvertisement\b", r"\bsponsored content\b", r"\bpaid post\b", r"\bclick here\b",
]

# Figure extractors (money / percentages / dates).
MONEY_RE = re.compile(r"[£$€]\s?\d[\d,]*(?:\.\d+)?\s?(?:tn|bn|trillion|billion|million|m|k)?\b", re.I)
PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s?%")
DATE_RE = re.compile(
    r"\b(\d{1,2}\s(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s\d{4})?"
    r"|\d{4}-\d{2}-\d{2})\b", re.I)


def _clean_body(text: str) -> str:
    """Strip web boilerplate (cookie/subscribe/share/byline/ad lines) from a
    scraped article body so the summariser sees clean prose."""
    if not text:
        return ""
    kept = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            continue
        low = s.lower()
        if len(s) < 120 and any(re.search(pat, low) for pat in BODY_JUNK_LINE_PATTERNS):
            continue
        kept.append(s)
    body = "\n".join(kept)
    for pat in BODY_JUNK_INLINE_PATTERNS:
        body = re.sub(pat, " ", body, flags=re.I)
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def _tidy_bullet(text: str) -> str:
    """Normalise a finished bullet: collapse whitespace, drop stray leading
    bullet chars / trailing asterisks / bracket artifacts, fix spacing before
    punctuation, and ensure it ends with a full stop."""
    t = (text or "").strip()
    t = re.sub(r"^\s*[-•*·]\s*", "", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([.,;:!?])", r"\1", t)
    t = re.sub(r"([.,;:!?])\1{1,}", r"\1", t)
    t = re.sub(r"\s*\[[^\]]*\]\s*$", "", t)
    t = re.sub(r"\s*\*+\s*$", "", t)
    t = t.strip(" -–—·*")
    if t and t[-1] not in ".!?":
        t += "."
    return t


def extract_facts(text: str) -> dict:
    """Pull the concrete figures out of a piece of text — handy for highlighting
    or standardising the money / % / dates that matter in a brief."""
    t = text or ""
    dates = [m if isinstance(m, str) else m[0] for m in DATE_RE.findall(t)]
    return {
        "money": MONEY_RE.findall(t),
        "percent": PERCENT_RE.findall(t),
        "dates": dates,
    }

# Phrases that indicate the piece contains no actual news.
NO_NEWS_PATTERNS = [
    "no specific news", "no information regarding", "no news", "reports no ",
    "beyond the quote", "no further news", "nothing to report", "no material news",
    "no relevant news", "article excerpt", "no significant news",
    "not mentioned in this", "is not mentioned", "no information provided about",
    "performance is not mentioned", "focuses on broader market", "no reported guidance",
    "no updates on", "not found in the provided", "no specific revenue",
    "there is no information", "not in immediate doubt",
    "has not been mentioned", "does not fit within scope", "could not be found",
    "0% change", "no reported figures", "no relevant information", "no relevant data",
    # LLM hedging phrases that read as "we found nothing" — never let these print.
    "no specific data", "no specific information", "no specific figures",
    "no specific details", "no specific detail", "no specific financial",
    "no concrete", "no quantifiable", "no meaningful", "no additional information",
    "no further detail", "no explicit", "no numerical", "not disclosed",
    "is not provided", "are not provided", "was not provided", "were not provided",
    "no data is provided", "no information is provided", "provides no",
    "provide no", "does not provide", "doesn't provide", "did not provide",
    "does not mention", "doesn't mention", "does not contain", "no financial detail",
    "without providing", "no details are given", "no detail is given",
]

# Regex catch-all for the same "no news / no data" hedging, so novel phrasings
# (e.g. "No specific data is provided in this excerpt") are also suppressed.
NO_NEWS_REGEXES = [
    re.compile(
        r"\bno\s+(?:specific|material|relevant|concrete|quantifiable|meaningful|"
        r"significant|additional|further|explicit|numerical|financial|reported)\s+"
        r"(?:\w+\s+){0,2}(?:data|information|figures?|detail|details|news|update|"
        r"updates|guidance|revenue|earnings)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:data|information|figures?|details?|guidance|revenue|earnings)\s+"
        r"(?:is|are|was|were)\s+not\s+(?:provided|available|disclosed|given|"
        r"mentioned|specified|reported)\b",
        re.I,
    ),
    re.compile(
        r"\bno\s+(?:data|information|figures?|details?)\s+(?:is|are)\s+"
        r"(?:provided|available|disclosed|given|mentioned)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:does not|doesn't|did not|didn't|do not|don't)\s+"
        r"(?:provide|disclose|specify|mention|contain|report|include)\b",
        re.I,
    ),
]

# Consumer marketing / promotional PR. This is about the company's *offers*, not
# its investment worth, so it is dropped. Kept deliberately specific to avoid
# nuking legitimate finance uses (e.g. "shares trading at a discount").
PROMO_PATTERNS = [
    "discount on", "discount code", "% discount", "% off", "money off",
    "save up to", "voucher", "promo code", "promotion code", "giveaway",
    "sweepstake", "prize draw", "special offer", "flash sale", "book now",
    "limited-time offer", "limited time offer", "deal of the day", "free upgrade",
    "cashback", "coupon", "sale now on", "on sale for", "off when you book",
    "dream wedding", "holiday package deal", "early bird offer",
]
PROMO_REGEXES = [
    r"£\d[\d,]*\s*(off|discount)", r"\$\d[\d,]*\s*(off|discount)",
    r"\d+%\s*off",
]

# Ad / tracker / video hosts mapped to a dead address in Chrome (--host-rules),
# so the browser never even connects to them. Speeds up extraction massively and
# leaves the page as essentially text.
AD_HOSTS = [
    "doubleclick.net", "googlesyndication.com", "googletagmanager.com",
    "google-analytics.com", "googleadservices.com", "adservice.google.com",
    "ads.pubmatic.com", "pubmatic.com", "rubiconproject.com", "criteo.com",
    "criteo.net", "scorecardresearch.com", "taboola.com", "outbrain.com",
    "adnxs.com", "amazon-adsystem.com", "adsafeprotected.com", "moatads.com",
    "quantserve.com", "facebook.net", "connect.facebook.net", "hotjar.com",
    "cdn.cookielaw.org", "onetrust.com", "player.vimeo.com",
    "imasdk.googleapis.com", "innovid.com", "teads.tv", "jwpcdn.com",
]

# Article-body containers, in priority order. We look *inside* these <div>/<article>
# blocks and read their paragraphs, instead of scraping the whole page. Covers the
# common patterns across Reuters, FT, Yahoo, Guardian, WordPress, etc.
ARTICLE_DIV_SELECTORS = [
    '[itemprop="articleBody"]',
    "article",
    "div.article-body", "div.article__body", "div.articleBody",
    "div.article-content", "div.article__content", "div.articletext",
    "div.story-body", "div.story__body", "div.storytext", "div.story-content",
    "div.entry-content", "div.post-content", "div.post__content",
    "div.content__article-body", "div.caas-body", "div.c-article-body",
    "div.body-content", "div.ArticleBody", "div.rich-text", "div.RichTextBody",
    "div[data-component='text-block']", "div#article-body", "main",
]

# Phrases that mark a paywall / consent / login stub rather than real content.
JUNK_MARKERS = [
    "enable javascript", "subscribe to continue", "subscribe to read",
    "sign in to read", "create an account", "accept cookies", "cookie policy",
    "you have reached your limit", "for unlimited access", "register to continue",
    "this content is not available in your", "please enable javascript",
]
