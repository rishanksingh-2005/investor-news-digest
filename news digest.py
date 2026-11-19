"""
Tier Company News Digest  --  v3
================================
A cleaner, faster rewrite of the multi-source weekly news digest.

Pipeline (one company at a time):
    1. DISCOVER   pull candidate stories from several reliable sources
                  (Google News, trusted outlets, the London Stock Exchange,
                   SEC EDGAR filings, and optionally Finnhub).
    2. EXTRACT    open each story in a stripped-down Chrome (no images, video,
                  ads or trackers) and pull the clean article text via Selenium.
    3. FILTER     drop junk / tabloid sources and keep only material news
                  (earnings, M&A, guidance, leadership, regulatory, ...).
    4. DEDUPE     collapse the same story reported by many outlets.
    5. SUMMARISE  turn each kept story into a one-line bullet via a local Ollama
                  model (with an offline fallback so it never hangs).
    6. PUBLISH    write a presentable PDF *and* DOCX, grouped by company, with a
                  clickable link to every source.

Usage:
    python "news digest.py"                  # uses the defaults below
    python "news digest.py" --tier 1 --days 7 --provider ollama
    python "news digest.py" --headless       # run Chrome invisibly

Everything is configured in the CONFIG dataclass near the top.
"""

from __future__ import annotations

# ----------------------------------------------------------------------------
# Imports (everything up front)
# ----------------------------------------------------------------------------
import os
import re
import time
import csv
import hashlib
import html
import io
import json
import argparse
import difflib
import queue
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
import email.utils
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Third-party (install via requirements.txt)
import openpyxl
from bs4 import BeautifulSoup
import trafilatura
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import WebDriverException

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, KeepTogether,
)

# DOCX
import docx
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.shared import Pt, RGBColor, Inches
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

# LLM clients (optional — only the chosen provider is required at run time)
try:
    from openai import OpenAI            # also used for Ollama (OpenAI-compatible API)
except ImportError:
    OpenAI = None
try:
    import anthropic
except ImportError:
    anthropic = None

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))


# ============================================================================
# CONFIG
# ============================================================================
@dataclass
class Config:
    # Input / output
    # Watchlist: .xlsx / .csv / .tsv / .json / .txt (see load_companies). Default:
    # $WATCHLIST, else watchlist.* or "Company List.xlsx" next to this script.
    watchlist: str = field(default_factory=lambda: default_watchlist())
    sheet_name: str | None = None         # Excel sheet; None = first sheet with a name column
    column_map: dict = field(default_factory=dict)   # {"name": "Issuer", "tier": "Priority"}
    default_tier: int = 1                 # tier for companies when the file has no tier column
    companies: list = field(default_factory=list)    # names given with --company (no file)
    output_dir: str = HERE
    tiers: tuple = (1, 2, 3)              # which tiers to include (combined digest)

    # Recency / volume
    days_back: int = 14                   # discovery window (quality-first, not a hard 7-day cut)
    recency_grace_days: int = 21          # soft recency: keep strong stories up to this age
    max_items_per_source: int = 8
    max_articles_per_company: int = 2     # cap per company (page budget)
    max_article_chars: int = 7000
    max_total_items: int = 40             # global cap across the digest (~8 pages; tune this)
    min_importance: int = 1               # drop items carrying no material signal

    # Sources on/off
    use_google_news: bool = True
    use_trusted_sites: bool = True
    use_regulated: bool = True           # per-exchange official/regulated market feeds
    use_sec_edgar: bool = True           # US SEC filings (US-listed names only)
    use_finnhub: bool = True             # Finnhub company-news; needs FINNHUB_API_KEY (North America only on free tier)

    # Selenium
    headless: bool = False
    page_load_timeout: int = 20
    resolve_wait: int = 8
    polite_delay: float = 1.0

    # Speed: work is done in phases, each spread over a small thread pool.
    discovery_workers: int = 3           # companies searched at once (Google rate-limits above ~3)
    http_workers: int = 8                # plain-HTTP article fetches at once
    browser_workers: int = 3             # Chrome windows at once (only for links that need one)
    llm_workers: int = 2                 # summaries in flight (Ollama queues extras anyway)
    http_fast_path: bool = True          # try plain HTTP before Chrome for non-Google links

    # Cache (SQLite). Makes reruns fast and a crashed run resumable: feeds,
    # extracted articles and LLM bullets are reused instead of fetched again.
    use_cache: bool = True
    cache_path: str = os.path.join(HERE, "digest_cache.sqlite")
    feed_cache_minutes: int = 60         # reuse a search result for this long
    article_cache_days: int = 30         # reuse an extracted article for this long
    failed_article_retry_hours: int = 24 # retry unreadable / paywalled pages after this
    new_only: bool = False               # skip stories already published in an earlier digest
    check_only: bool = False             # --check: report how the watchlist reads, then exit
    init_watchlist: str | None = None    # --init-watchlist FILE: write a starter file, then exit
    doctor: bool = False                 # --doctor: check setup and test API keys, then exit

    # Dedup / merge thresholds (higher = stricter, fewer things treated as dup)
    title_similarity: float = 0.82       # stage 1: near-identical headline dedupe
    merge_text_ratio: float = 0.58       # stage 2: same-event if bullet text this similar
    merge_token_overlap: float = 0.42    # stage 2: OR shared-token (Jaccard) overlap
    merge_distinctive: int = 3           # stage 2: OR this many shared distinctive tokens

    # LLM
    llm_provider: str = "ollama"         # "ollama" | "openai" | "anthropic" | "bart"
    ollama_model: str = "llama3.2:3b"
    ollama_base_url: str = "http://localhost:11434/v1"
    openai_model: str = "gpt-4o-mini"
    anthropic_model: str = "claude-sonnet-4-6"
    llm_timeout: int = 90

    # Local BART summariser (facebook/bart-large-cnn in the NLP folder). Only used
    # when llm_provider == "bart"; it writes the per-article bullet. It can't follow
    # instructions or emit JSON, so topic grouping falls back to the heuristic.
    # If the NLP folder is missing (e.g. a fresh clone — the weights are not in git)
    # the model is downloaded from the Hugging Face hub instead.
    bart_model_dir: str = os.path.join(HERE, "NLP")
    bart_hub_id: str = "facebook/bart-large-cnn"
    bart_min_tokens: int = 18
    bart_max_tokens: int = 60
    bart_max_input_words: int = 800

    # Finnhub ticker map (name -> symbol)
    finnhub_tickers: dict = field(default_factory=dict)


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


# ============================================================================
# Data models
# ============================================================================
@dataclass
class Article:
    headline: str
    url: str
    source: str = ""        # outlet name
    origin: str = ""        # which discovery channel surfaced it
    kind: str = "news"      # "news" | "filing"
    published: str = ""     # ISO datetime
    final_url: str = ""     # resolved publisher URL
    snippet: str = ""       # short feed description (fallback text)
    body: str = ""          # extracted full text (read from the article's <div>s)
    authentic: bool = False # True only if a real, readable article body was found
    bullet: str = ""        # one-line summary for the digest
    extra_sources: list = field(default_factory=list)  # merged duplicate sources
    heading: str = ""       # topic sub-heading this point sits under
    importance: int = 0     # material-signal score, used for ranking / page budget


@dataclass
class Company:
    name: str
    currency: str = ""
    exchange: str = ""      # ISO 10383 MIC (XLON, XNAS, ...), normalised by the loader
    tier: int = 0
    ticker: str = ""
    country: str = ""       # "US", "UK", ... (used when the exchange is unknown)
    articles: list = field(default_factory=list)
    note: str = ""

    @property
    def is_us(self) -> bool:
        return self.exchange.upper() in US_EXCHANGES or (not self.exchange and self.country == "US")

    @property
    def is_north_am(self) -> bool:
        return (self.exchange.upper() in NORTH_AM_EXCHANGES
                or (not self.exchange and self.country in {"US", "CA"}))

    @property
    def is_uk(self) -> bool:
        return self.exchange.upper() == "XLON" or (not self.exchange and self.country == "UK")


# ============================================================================
# Watchlist loader: Excel, CSV/TSV, JSON or a plain list of names.
#
# Any customer's own spreadsheet works as long as it has a column with the
# company name; every other column is optional and recognised by common
# header names (see WATCHLIST_COLUMNS) or mapped explicitly with --map.
# ============================================================================
class WatchlistError(ValueError):
    pass


# canonical field -> accepted header names (compared lower-case, with spaces,
# underscores, dots and dashes removed)
WATCHLIST_COLUMNS = {
    "name": ["name", "company", "companyname", "company name", "issuer", "issuername", "securityname",
             "security", "entity", "entityname", "organisation", "organization", "longname", "shortname"],
    "ticker": ["ticker", "symbol", "tickersymbol", "microsoftexcelticker", "bloombergticker", "epic", "code"],
    "exchange": ["exchange", "mic", "exchangemic", "market", "listing", "venue", "primaryexchange"],
    "currency": ["currency", "ccy", "tradingcurrency"],
    "country": ["country", "countryofrisk", "domicile", "region", "countrycode"],
    "tier": ["tier", "priority", "group", "bucket", "level", "rank"],
}

# Exchange names people actually type -> MIC.
EXCHANGE_ALIASES = {
    "NASDAQ": "XNAS", "NASDAQGS": "XNAS", "NASDAQGM": "XNAS", "NASDAQCM": "XNAS", "NMS": "XNAS",
    "NYSE": "XNYS", "NEWYORKSTOCKEXCHANGE": "XNYS", "NYSEAMERICAN": "XASE", "AMEX": "XASE",
    "NYSEARCA": "ARCX", "CBOE": "BATS", "OTC": "OTCM", "OTCQX": "OTCM", "OTCQB": "OTCM", "PINK": "OTCM",
    "LSE": "XLON", "LON": "XLON", "LONDON": "XLON", "AIM": "XLON", "LONDONSTOCKEXCHANGE": "XLON",
    "TSX": "XTSE", "TORONTO": "XTSE", "TSXV": "XTSX", "TSXVENTURE": "XTSX",
    "ASX": "XASX", "EURONEXTPARIS": "XPAR", "EPA": "XPAR", "PARIS": "XPAR",
    "EURONEXTAMSTERDAM": "XAMS", "AMS": "XAMS", "EURONEXTLISBON": "XLIS",
    "STOCKHOLM": "XSTO", "NASDAQSTOCKHOLM": "XSTO", "BORSAITALIANA": "XMIL", "MILAN": "XMIL",
    "TASE": "XTAE", "TELAVIV": "XTAE", "XETRA": "XFRA", "FRANKFURT": "XFRA", "FRA": "XFRA",
    "SIX": "XSWX", "SWX": "XSWX", "WSE": "XWAR", "GPW": "XWAR", "WARSAW": "XWAR",
}

COUNTRY_ALIASES = {
    "US": "US", "USA": "US", "UNITEDSTATES": "US", "UNITEDSTATESOFAMERICA": "US",
    "UK": "UK", "GB": "UK", "GBR": "UK", "UNITEDKINGDOM": "UK", "GREATBRITAIN": "UK", "ENGLAND": "UK",
    "CA": "CA", "CAN": "CA", "CANADA": "CA",
}

# Values spreadsheets put in cells that mean "nothing here".
_EMPTY_CELL = {"", "#VALUE!", "#N/A", "#NAME?", "#REF!", "#DIV/0!", "N/A", "NA", "NONE", "NULL", "-"}


def _norm_header(h) -> str:
    return re.sub(r"[\s_.\-/()]+", "", str(h or "")).lower()


def _cell(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if s.upper() in _EMPTY_CELL else s


def normalise_exchange(v) -> str:
    s = _cell(v)
    key = re.sub(r"[\s_.\-:]+", "", s).upper()
    if not key:
        return ""
    return EXCHANGE_ALIASES.get(key, key if re.fullmatch(r"[A-Z]{4}", key) else s)


def normalise_country(v) -> str:
    key = re.sub(r"[\s_.\-]+", "", _cell(v)).upper()
    return COUNTRY_ALIASES.get(key, key)


def parse_tier(v):
    """1, 1.0, "1", "Tier 1", "T1", "P2" -> int; anything else -> None."""
    s = _cell(v)
    if not s:
        return None
    m = re.fullmatch(r"(?:tier|t|priority|p|group|g)?\s*(\d+)(?:\.0+)?", s, re.I)
    return int(m.group(1)) if m else None


def parse_column_map(spec: str) -> dict:
    """'name=Issuer, tier=Priority' -> {'name': 'Issuer', 'tier': 'Priority'}."""
    out = {}
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise WatchlistError(f"--map entry {part.strip()!r} must look like field=Column Header")
        k, v = (x.strip() for x in part.split("=", 1))
        if k.lower() not in WATCHLIST_COLUMNS:
            raise WatchlistError(f"--map field {k!r} unknown; use one of {', '.join(WATCHLIST_COLUMNS)}")
        out[k.lower()] = v
    return out


def _match_columns(header: list, column_map: dict) -> dict:
    """Header cells -> {field: column index}. Explicit --map entries win."""
    normed = [_norm_header(h) for h in header]
    found = {}
    for fld, aliases in WATCHLIST_COLUMNS.items():
        if fld in column_map:
            want = _norm_header(column_map[fld])
            if want not in normed:
                raise WatchlistError(f"column {column_map[fld]!r} (mapped to {fld}) not found; "
                                     f"columns are: {', '.join(str(h) for h in header if h)}")
            found[fld] = normed.index(want)
            continue
        for alias in aliases:
            a = _norm_header(alias)
            if a in normed and normed.index(a) not in found.values():
                found[fld] = normed.index(a)
                break
    return found


def _find_header(rows: list, column_map: dict):
    """The header is the first of the top 15 rows that has a recognisable name column."""
    error = None
    for i, row in enumerate(rows[:15]):
        if not any(_cell(c) for c in row):
            continue
        try:
            cols = _match_columns(list(row), column_map)
        except WatchlistError as e:   # a --map column missing from this row
            error = error or e
            continue
        if "name" in cols:
            return i, cols
    if error:
        raise error
    return None, {}


def _read_rows(path: str, sheet: str | None) -> list[list]:
    """Raw rows from any supported file, header row included where there is one."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        try:
            if sheet:
                if sheet not in wb.sheetnames:
                    raise WatchlistError(f"sheet {sheet!r} not found; sheets are: {', '.join(wb.sheetnames)}")
                sheets = [wb[sheet]]
            else:   # first sheet that has a name column, active sheet first
                sheets = [wb.active] + [ws for ws in wb.worksheets if ws is not wb.active]
            for ws in sheets:
                rows = [list(r) for r in ws.iter_rows(values_only=True)]
                if len(sheets) == 1 or _find_header(rows, {})[0] is not None:
                    return rows
            return [list(r) for r in sheets[0].iter_rows(values_only=True)]
        finally:
            wb.close()
    if ext == ".xls":
        raise WatchlistError("old .xls files aren't supported: open it in Excel and Save As .xlsx or .csv")
    if ext == ".json":
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("companies") or data.get("watchlist") or []
        if not isinstance(data, list):
            raise WatchlistError("JSON watchlist must be a list, or an object with a 'companies' list")
        if all(isinstance(x, str) for x in data):
            return [["name"]] + [[x] for x in data]
        keys = list(dict.fromkeys(k for x in data if isinstance(x, dict) for k in x))
        return [keys] + [[x.get(k) for k in keys] for x in data if isinstance(x, dict)]
    with open(path, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    if not text.strip():
        return []
    if ext == ".txt":   # one company per line; '#' starts a comment
        names = [ln.split("#", 1)[0].strip() for ln in text.splitlines()]
        return [["name"]] + [[n] for n in names if n]
    # CSV / TSV: sniff the delimiter; a single column with no header is a list of names.
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if ext == ".tsv" else csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect)]
    if rows and all(len(r) <= 1 for r in rows) and _find_header(rows, {})[0] is None:
        rows = [["name"]] + rows
    return rows


def default_watchlist() -> str:
    """WATCHLIST env var, else the first watchlist file next to the script."""
    env = os.getenv("WATCHLIST", "").strip()
    if env:
        return env
    for fn in ("watchlist.xlsx", "watchlist.csv", "watchlist.json", "watchlist.txt", "Company List.xlsx"):
        if os.path.exists(os.path.join(HERE, fn)):
            return os.path.join(HERE, fn)
    return os.path.join(HERE, "examples", "watchlist.example.csv")


def read_watchlist(cfg: Config) -> tuple[list[Company], dict]:
    """Every company in the watchlist (all tiers) plus a report of what was detected."""
    path = cfg.watchlist
    if not os.path.exists(path):
        raise WatchlistError(f"watchlist not found: {path}")
    rows = _read_rows(path, cfg.sheet_name)
    column_map = parse_column_map(cfg.column_map) if isinstance(cfg.column_map, str) else dict(cfg.column_map)
    h, cols = _find_header(rows, column_map)
    if h is None:
        first = next((r for r in rows if any(_cell(c) for c in r)), [])
        raise WatchlistError(
            "couldn't find a company-name column. Name a column 'Company' or 'Name', or map yours with "
            f"--map name=\"Your Header\". First row is: {', '.join(str(c) for c in first if c is not None)}")
    report = {"columns": {f: str(rows[h][i]) for f, i in cols.items()}, "skipped": [], "duplicates": 0}

    def get(r, fld):
        i = cols.get(fld)
        return r[i] if i is not None and i < len(r) else None

    out, seen = [], set()
    for n, r in enumerate(rows[h + 1:], start=h + 2):
        name = _cell(get(r, "name"))
        if not name:
            continue
        if "tier" in cols:
            tier = parse_tier(get(r, "tier"))
            if tier is None:
                raw = _cell(get(r, "tier"))
                report["skipped"].append(f"row {n} ({name}): " + (f"tier {raw!r} is not a number"
                                                                   if raw else "no tier"))
                continue
        else:
            tier = cfg.default_tier
        if name.lower() in seen:
            report["duplicates"] += 1
            continue
        seen.add(name.lower())
        out.append(Company(
            name=name, tier=tier,
            currency=_cell(get(r, "currency")),
            exchange=normalise_exchange(get(r, "exchange")),
            ticker=_cell(get(r, "ticker")),
            country=normalise_country(get(r, "country")),
        ))
    return out, report


def load_companies(cfg: Config) -> list[Company]:
    if cfg.companies:   # --company "Name" on the command line: no file needed
        return [Company(name=n.strip(), tier=cfg.default_tier) for n in cfg.companies if n.strip()]
    companies, report = read_watchlist(cfg)
    for msg in report["skipped"][:5]:
        print(f"   skipped {msg}")
    if len(report["skipped"]) > 5:
        print(f"   ... and {len(report['skipped']) - 5} more rows without a valid tier")
    return [c for c in companies if c.tier in cfg.tiers]


def check_watchlist(cfg: Config) -> int:
    """--check: show how the watchlist was read, without fetching anything."""
    try:
        companies, report = read_watchlist(cfg)
    except (WatchlistError, OSError, json.JSONDecodeError) as e:
        print(f"Watchlist problem: {e}")
        return 1
    print(f"Watchlist: {cfg.watchlist}")
    for fld in WATCHLIST_COLUMNS:
        src = report["columns"].get(fld)
        print(f"  {fld:<9}<- {src!r}" if src else
              f"  {fld:<9}   (not found{'; every company is tier ' + str(cfg.default_tier) if fld == 'tier' else ''})")
    tiers: dict = {}
    for c in companies:
        tiers.setdefault(c.tier, []).append(c)
    print(f"\n{len(companies)} companies")
    for t in sorted(tiers):
        names = [c.name for c in tiers[t]]
        more = f" ... +{len(names) - 6} more" if len(names) > 6 else ""
        print(f"  Tier {t}: {len(names):>4}  {', '.join(names[:6])}{more}")
    unknown = sorted({c.exchange for c in companies if c.exchange and c.exchange.upper() not in
                      EXCHANGE_SOURCES and c.exchange.upper() not in NORTH_AM_EXCHANGES})
    if unknown:
        print(f"\n  Exchanges without a dedicated source (generic wires used): {', '.join(unknown)}")
    if report["duplicates"]:
        print(f"  {report['duplicates']} duplicate names ignored")
    for msg in report["skipped"]:
        print(f"  skipped {msg}")
    print(f"\nSelected tiers {_tiers_label(cfg)}: {sum(c.tier in cfg.tiers for c in companies)} companies")
    return 0


WATCHLIST_TEMPLATE = [
    ["Company", "Ticker", "Exchange", "Country", "Currency", "Tier"],
    ["Apple Inc.", "AAPL", "XNAS", "US", "USD", 1],
    ["Unilever PLC", "ULVR", "XLON", "UK", "GBp", 2],
    ["Shopify Inc.", "SHOP", "XTSE", "CA", "CAD", 3],
]


def write_watchlist_template(path: str) -> None:
    if os.path.exists(path):
        raise WatchlistError(f"{path} already exists; not overwriting it")
    if path.lower().endswith((".xlsx", ".xlsm")):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Watchlist"
        for row in WATCHLIST_TEMPLATE:
            ws.append(row)
        wb.save(path)
    elif path.lower().endswith(".json"):
        keys = [k.lower() for k in WATCHLIST_TEMPLATE[0]]
        with open(path, "w", encoding="utf-8") as f:
            json.dump([dict(zip(keys, r)) for r in WATCHLIST_TEMPLATE[1:]], f, indent=2)
    else:
        with open(path, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(WATCHLIST_TEMPLATE)


# ============================================================================
# Selenium driver  (text-only: images / media / ads / trackers blocked)
# ============================================================================
def build_driver(cfg: Config):
    opts = Options()
    if cfg.headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1280,1000")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--mute-audio")
    opts.add_argument("--blink-settings=imagesEnabled=false")   # no images
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    # Block ad/tracker/video hosts at the network layer -> faster, text-only.
    host_rules = ",".join(f"MAP {h} 127.0.0.1" for h in AD_HOSTS)
    opts.add_argument(f"--host-rules={host_rules}")
    # Disable images, plugins, popups, autoplay, notifications via content prefs.
    opts.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2,
        "profile.managed_default_content_settings.plugins": 2,
        "profile.managed_default_content_settings.popups": 2,
        "profile.managed_default_content_settings.media_stream": 2,
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.autoplay": 2,
    })
    # "eager" = stop waiting once the DOM is ready (don't wait on trackers).
    opts.page_load_strategy = "eager"

    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(cfg.page_load_timeout)
    try:
        driver.set_script_timeout(15)
    except Exception:
        pass
    return driver


# ============================================================================
# Cache (SQLite)
# ============================================================================
class Cache:
    """A small SQLite cache, safe to share between worker threads.

    feeds      raw discovery responses (short TTL): reruns don't re-query feeds
    articles   resolved URL + extracted body per story: Chrome never re-opens it
    bullets    LLM bullet per (story, provider, model): no repeat LLM calls
    published  stories that made it into a digest: lets ``--new-only`` skip them

    ``Cache(None)`` is a disabled cache: every lookup misses, writes do nothing.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS feeds     (url TEXT PRIMARY KEY, body BLOB, fetched_at REAL);
    CREATE TABLE IF NOT EXISTS articles  (key TEXT PRIMARY KEY, final_url TEXT, body TEXT,
                                          authentic INTEGER, fetched_at REAL);
    CREATE TABLE IF NOT EXISTS bullets   (key TEXT, provider TEXT, model TEXT, bullet TEXT,
                                          created_at REAL, PRIMARY KEY (key, provider, model));
    CREATE TABLE IF NOT EXISTS published (key TEXT PRIMARY KEY, company TEXT, digest_date TEXT,
                                          published_at REAL);
    """

    def __init__(self, path: str | None):
        self.path = path
        self._lock = threading.Lock()
        self._db = None
        if path:
            self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
            with self._lock:
                self._db.executescript(self.SCHEMA)
                self._prune()

    @property
    def enabled(self) -> bool:
        return self._db is not None

    def _q(self, sql: str, args=(), one=False):
        if self._db is None:
            return None
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = cur.fetchone() if one else cur.fetchall()
            self._db.commit()
            return rows

    def _prune(self):
        now = time.time()
        self._db.execute("DELETE FROM feeds WHERE fetched_at < ?", (now - 2 * 86400,))
        self._db.execute("DELETE FROM articles WHERE fetched_at < ?", (now - 120 * 86400,))
        self._db.execute("DELETE FROM bullets WHERE created_at < ?", (now - 120 * 86400,))
        self._db.execute("DELETE FROM published WHERE published_at < ?", (now - 365 * 86400,))
        self._db.commit()

    # feeds --------------------------------------------------------------------
    def get_feed(self, url: str, max_age_s: float) -> bytes | None:
        row = self._q("SELECT body, fetched_at FROM feeds WHERE url = ?", (url,), one=True)
        if row and time.time() - row[1] <= max_age_s:
            return row[0]
        return None

    def put_feed(self, url: str, body: bytes) -> None:
        self._q("INSERT OR REPLACE INTO feeds VALUES (?, ?, ?)", (url, body, time.time()))

    # articles -------------------------------------------------------------------
    def get_article(self, key: str, max_age_s: float, failed_max_age_s: float) -> dict | None:
        if not key:
            return None
        row = self._q("SELECT final_url, body, authentic, fetched_at FROM articles WHERE key = ?",
                      (key,), one=True)
        if not row:
            return None
        age = time.time() - row[3]
        if age > (max_age_s if row[2] else failed_max_age_s):
            return None
        return {"final_url": row[0], "body": row[1], "authentic": bool(row[2])}

    def put_article(self, key: str, final_url: str, body: str, authentic: bool) -> None:
        if key:
            self._q("INSERT OR REPLACE INTO articles VALUES (?, ?, ?, ?, ?)",
                    (key, final_url, body, int(authentic), time.time()))

    # bullets --------------------------------------------------------------------
    def get_bullet(self, key: str, provider: str, model: str) -> str | None:
        row = self._q("SELECT bullet FROM bullets WHERE key = ? AND provider = ? AND model = ?",
                      (key, provider, model), one=True)
        return row[0] if row else None

    def put_bullet(self, key: str, provider: str, model: str, bullet: str) -> None:
        if key and bullet:
            self._q("INSERT OR REPLACE INTO bullets VALUES (?, ?, ?, ?, ?)",
                    (key, provider, model, bullet, time.time()))

    # published --------------------------------------------------------------------
    def was_published(self, *keys: str) -> bool:
        keys = [k for k in keys if k]
        if not keys or self._db is None:
            return False
        marks = ",".join("?" * len(keys))
        return bool(self._q(f"SELECT 1 FROM published WHERE key IN ({marks}) LIMIT 1", keys, one=True))

    def mark_published(self, keys, company: str, digest_date: str) -> None:
        for k in keys:
            if k:
                self._q("INSERT OR REPLACE INTO published VALUES (?, ?, ?, ?)",
                        (k, company, digest_date, time.time()))

    def close(self) -> None:
        if self._db is not None:
            with self._lock:
                self._db.close()
            self._db = None


# The cache in use for the current run. run() swaps in a real one; everything
# else (tests, one-off calls) gets a disabled cache.
_CACHE = Cache(None)


def _model_name(cfg: "Config") -> str:
    p = cfg.llm_provider.lower()
    return {"ollama": cfg.ollama_model, "openai": cfg.openai_model,
            "anthropic": cfg.anthropic_model, "bart": cfg.bart_model_dir}.get(p, p)


def _urlopen_retry(req, timeout: float = 20, attempts: int = 2, backoff: float = 3.0) -> bytes:
    """GET with one polite retry on rate-limit / server errors (429, 5xx)."""
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout).read()
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < attempts - 1:
                time.sleep(backoff * (attempt + 1))
                continue
            raise


def _driver_alive(driver) -> bool:
    try:
        _ = driver.current_url
        return True
    except Exception:
        return False


def _safe(fn, default=""):
    try:
        return fn()
    except Exception:
        return default


# ============================================================================
# STEP 1 — Discovery
# ============================================================================
def _google_news_rss(query: str, cfg: Config, origin: str, source_override=None) -> list[Article]:
    """Fetch + parse a Google News RSS search. Reliable (plain XML, no JS)."""
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query) + "&hl=en-US&gl=US&ceid=US:en")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    })
    try:
        raw = _CACHE.get_feed(url, cfg.feed_cache_minutes * 60)
        if raw is None:
            raw = _urlopen_retry(req, timeout=20)
            root = ET.fromstring(raw)
            if root.tag != "rss":
                # e.g. a consent / rate-limit page that happens to parse as XML
                raise ValueError(f"not an RSS feed (root <{root.tag}>)")
            _CACHE.put_feed(url, raw)           # only real feeds are cached
        else:
            root = ET.fromstring(raw)
    except Exception as e:
        print(f"      [{origin}] feed failed: {e}")
        return []

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
        out.append(Article(headline=headline, url=link, source=source_override or source,
                            origin=origin, published=published, snippet=snippet))
        if len(out) >= cfg.max_items_per_source:
            break
    return out


def discover_google_news(company: Company, cfg: Config) -> list[Article]:
    return _google_news_rss(f'{company.name} when:{cfg.days_back}d', cfg, "Google News")


def discover_trusted(company: Company, cfg: Config) -> list[Article]:
    """Seek the same news specifically from a basket of trusted outlets."""
    sites = " OR ".join(f"site:{s}" for s in TRUSTED_SITES)
    query = f'"{company.name}" ({sites}) when:{cfg.days_back}d'
    return _google_news_rss(query, cfg, "Trusted outlets")


def discover_regulated(company: Company, cfg: Config) -> list[Article]:
    """Authentic / official market news for the company's exchange, pulled via the
    Google News feed with a site: filter over that market's regulated-news domains
    (RNS/Investegate for UK, SEDAR+/Cision for Canada, Actusnews for France,
    MFN/Cision for Sweden, EQS for Germany, MAYA for Israel, ASX for Australia,
    US PR wires, etc.). Works for any tier / exchange."""
    sites = exchange_sources(company.exchange)
    if not sites:
        return []
    site_filter = " OR ".join(f"site:{s}" for s in sites)
    query = f'"{company.name}" ({site_filter}) when:{cfg.days_back}d'
    return _google_news_rss(query, cfg, origin=f"Official / regulated ({company.exchange})")


_SEC_DEFAULT_UA = "NewsDigest research contact@example.com"
_SEC_UA_WARNED = False


def sec_user_agent(warn: bool = True) -> str:
    """SEC EDGAR requires automated clients to identify themselves with a name and
    a contact email (sec.gov/os/accessing-edgar-data), e.g.
    SEC_USER_AGENT=Jane Smith jane@example.com. Each user sets their own in .env.
    Without one, a generic placeholder is sent, which the SEC may throttle or block."""
    global _SEC_UA_WARNED
    ua = os.environ.get("SEC_USER_AGENT", "").strip().strip("\"'").strip()
    if ua and re.search(r"\S+@\S+\.\S+", ua):
        return ua
    if warn and not _SEC_UA_WARNED:
        _SEC_UA_WARNED = True
        why = "has no contact email" if ua else "isn't set"
        print(f"      [SEC EDGAR] SEC_USER_AGENT {why} — the SEC asks for 'Your Name you@domain.com' "
              f"in .env; requests may be refused without it")
    return ua if ua else _SEC_DEFAULT_UA


def discover_sec_edgar(company: Company, cfg: Config) -> list[Article]:
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
        raw = _CACHE.get_feed(api, cfg.feed_cache_minutes * 60)
        if raw is None:
            # EDGAR full-text search throws occasional 5xx — _urlopen_retry retries once
            raw = _urlopen_retry(req, timeout=20, backoff=2.0)
            data = json.loads(raw.decode("utf-8", "replace"))
            _CACHE.put_feed(api, raw)
        else:
            data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        print(f"      [SEC EDGAR] failed: {e}")
        return []

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
    toks = _company_tokens(company.name)
    distinctive = [t for t in toks if len(t) >= 4] or toks

    out = []
    for acc, (src, fn, form) in by_filing.items():
        if len(out) >= cfg.max_items_per_source:
            break
        names = src.get("display_names") or []
        if names and not any(t in _norm(n) for n in names for t in distinctive):
            continue
        who = re.sub(r"(\s*\([^)]*\))+\s*$", "", names[0]).strip() if names else company.name
        ciks = src.get("ciks") or []
        cik = str(ciks[0] if ciks else "").lstrip("0")
        link = ""
        if cik:
            link = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{fn or ''}"
        desc = (src.get("file_description") or "").strip()
        out.append(Article(headline=f"SEC {form} filing — {who}", url=link, final_url=link,
                           source="SEC EDGAR", origin="SEC EDGAR", kind="filing",
                           published=src.get("file_date", ""), snippet=desc))
    return out


_FINNHUB_SYMBOL_CACHE: dict = {}
_FINNHUB_DISABLED = False   # set once per run: no key, or the key was rejected
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
        return json.loads(_urlopen_retry(req, timeout=20, backoff=5.0).decode("utf-8", "replace"))
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
    toks = _company_tokens(name)
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
    want = set(_company_tokens(name))
    for r in results:
        sym = (r.get("symbol") or "").strip()
        desc = set(_norm(r.get("description") or "").split())
        if sym and "." not in sym and want <= desc:
            return sym
    return ""


def _finnhub_lookup_symbol(name: str, key: str) -> str:
    """Resolve a company name to a Finnhub symbol via the /search endpoint, only
    accepting a result whose registered name matches. Cached per run."""
    if name in _FINNHUB_SYMBOL_CACHE:
        return _FINNHUB_SYMBOL_CACHE[name]
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
    _FINNHUB_SYMBOL_CACHE[name] = sym
    return sym


def discover_finnhub(company: Company, cfg: Config) -> list[Article]:
    """Finnhub company-news REST API, with the user's own FINNHUB_API_KEY (free at
    finnhub.io/register). The free plan covers North America only, so other
    listings are skipped. Ticker: Config.finnhub_tickers, else the watchlist's
    ticker column, else looked up by name."""
    global _FINNHUB_DISABLED
    if _FINNHUB_DISABLED:
        return []
    key = env_key("FINNHUB_API_KEY")
    if not key:
        # say it once, not once per company
        print("      [Finnhub] no FINNHUB_API_KEY set — skipping Finnhub (optional; get a free key "
              "at finnhub.io/register and put it in .env)")
        _FINNHUB_DISABLED = True
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
            return []
        data = _finnhub_get("company-news", {"symbol": symbol, "from": start, "to": end}, key)
    except _FinnhubAuthError:
        print("      [Finnhub] FINNHUB_API_KEY was rejected (HTTP 401) — check the key in .env "
              "(python \"news digest.py\" --doctor tests it); skipping Finnhub for this run")
        _FINNHUB_DISABLED = True
        return []
    except _FinnhubNoAccess:
        print(f"      [Finnhub] '{company.name}' isn't covered by this key's plan (HTTP 403) — skipping")
        return []
    except Exception as e:
        print(f"      [Finnhub] failed: {e}")
        return []
    if not isinstance(data, list):   # error payloads come back as {"error": "..."}
        if isinstance(data, dict) and data.get("error"):
            print(f"      [Finnhub] {company.name}: {data['error']}")
        return []
    out = []
    for it in (data or [])[: cfg.max_items_per_source]:
        try:
            published = datetime.fromtimestamp(int(it.get("datetime", 0)), tz=timezone.utc).isoformat()
        except Exception:
            published = ""
        out.append(Article(headline=(it.get("headline") or "").strip(), url=it.get("url", ""),
                           source=it.get("source", "Finnhub"), origin="Finnhub",
                           published=published, snippet=it.get("summary", "") or ""))
    return out


def discover(company: Company, cfg: Config) -> list[Article]:
    steps = [
        (cfg.use_google_news, discover_google_news),
        (cfg.use_trusted_sites, discover_trusted),
        (cfg.use_regulated, discover_regulated),
        (cfg.use_sec_edgar, discover_sec_edgar),
        (cfg.use_finnhub, discover_finnhub),
    ]
    found = []
    for i, (enabled, fn) in enumerate(s for s in steps if s[0]):
        if i:
            # ~3 feed hits per company x 112 companies: pace them so Google News
            # doesn't start returning 429 / 503 halfway through the run.
            time.sleep(cfg.polite_delay)
        found += fn(company, cfg)
    return found


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
        for sel in ARTICLE_DIV_SELECTORS:
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


def _is_authentic(text: str) -> bool:
    """Authentic = a real, readable article body — not a paywall / consent /
    login stub or a near-empty shell. Such pages are left out (not summarised)."""
    if not text or len(text) < 400:
        return False
    low = text.lower()
    if sum(low.count(m) for m in JUNK_MARKERS) >= 2:
        return False
    if len(re.findall(r"[.!?]", text)) < 4:   # needs several sentences of prose
        return False
    return True


def extract_body(driver, article: Article, cfg: Config) -> Article:
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
        _safe(lambda: driver.execute_script("window.stop()"))

    deadline = time.time() + cfg.resolve_wait
    while time.time() < deadline:
        cur = _safe(lambda: driver.current_url)
        if cur and "news.google.com" not in cur and cur.startswith("http"):
            break
        time.sleep(0.4)

    article.final_url = _safe(lambda: driver.current_url) or article.url
    _safe(lambda: driver.execute_script("window.stop()"))
    page = _safe(lambda: driver.page_source)
    if not page:
        return article

    article.body = _clean_body(_extract_article_text(page, article.final_url))[: cfg.max_article_chars]
    article.authentic = _is_authentic(article.body)
    return article


_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _orig_key(a: Article) -> str:
    """Cache key for a story: its discovery URL (stable across reruns)."""
    u = (a.url or "").lower()
    return re.sub(r"^https?://(www\.)?", "", u).split("#")[0].rstrip("/")


def _needs_browser(a: Article) -> bool:
    """Google News links are JavaScript redirects — only a real browser can
    follow them to the publisher. Everything else can be tried over plain HTTP."""
    target = a.final_url or a.url
    return "news.google.com" in urllib.parse.urlparse(target).netloc


def extract_http(article: Article, cfg: Config) -> bool:
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
    body = _clean_body(_extract_article_text(page, final))[: cfg.max_article_chars]
    if not _is_authentic(body):
        return False
    article.final_url, article.body, article.authentic = final, body, True
    return True


_DRIVER_LOCK = threading.Lock()   # start Chromes one at a time (driver download is not thread-safe)


def _extract_with_browsers(articles: list[Article], cfg: Config) -> None:
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
                if driver is None or not _driver_alive(driver):
                    if driver is not None:
                        _safe(driver.quit)
                    try:
                        with _DRIVER_LOCK:
                            driver = build_driver(cfg)
                    except Exception as e:
                        print(f"   Chrome failed to start ({e.__class__.__name__}: {e}) — "
                              f"skipping stories that need a browser")
                        failed_start.set()
                        return
                try:
                    extract_body(driver, a, cfg)
                except WebDriverException:
                    _safe(driver.quit)
                    driver = None
                time.sleep(cfg.polite_delay)
        finally:
            if driver is not None:
                _safe(driver.quit)

    n = max(1, min(cfg.browser_workers, len(articles)))
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def extract_all(articles: list[Article], cfg: Config) -> dict:
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
        hit = _CACHE.get_article(_orig_key(a), cfg.article_cache_days * 86400,
                                 cfg.failed_article_retry_hours * 3600)
        if hit is not None:
            a.final_url = hit["final_url"] or a.final_url
            a.body, a.authentic = hit["body"] or "", hit["authentic"]
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
        _CACHE.put_article(_orig_key(a), a.final_url, a.body if a.authentic else "", a.authentic)
        if not a.authentic:
            stats["unreadable"] += 1
    return stats


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


def is_blocklisted(a: Article) -> bool:
    # Match both as-is and with spaces removed, so a feed name like
    # "Simply Wall St" is caught by the "simplywall" entry.
    hay = f"{a.source} {a.final_url or a.url}".lower()
    squashed = re.sub(r"\s+", "", hay)
    return any(bad in hay or bad.replace(" ", "") in squashed for bad in SOURCE_BLOCKLIST)


def is_relevant(a: Article, company: Company) -> bool:
    if a.kind == "filing":
        return True
    lead = _norm(f"{a.headline} {a.snippet}")
    full = _norm(f"{a.headline} {a.snippet} {a.body}")
    if not full.strip():
        return False
    if not any(kw in full for kw in RELEVANCE_KEYWORDS):
        return False
    toks = _company_tokens(company.name)
    distinctive = [t for t in toks if len(t) >= 5] or toks
    # the distinctive company name should appear in the headline/lead, not just
    # buried deep in the body (kills off-topic articles that merely mention it).
    if not any(t in lead for t in distinctive):
        if sum(1 for t in set(toks) if t in full) < 2:
            return False
    return True


# Generic geographic / directional / descriptor words that are >=5 chars but are
# NOT distinctive on their own — a name built only from these (e.g. "North West")
# must be matched as a full phrase, or common articles that merely contain "north"
# leak in as false positives.
GENERIC_NAME_WORDS = {
    "north", "south", "east", "west", "northern", "southern", "eastern",
    "western", "central", "world", "global", "united", "national",
    "international", "american", "british", "european", "general", "standard",
    "first", "great", "royal", "modern", "premier", "prime", "value", "smart",
    "direct", "future", "capital", "century", "pacific", "metro", "greater",
}


def strongly_relevant(a: Article, company: Company) -> bool:
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
    lead = _norm(f"{a.headline} {a.snippet}")
    if not lead.strip():
        return False
    toks = _company_tokens(company.name)
    distinctive = [t for t in toks if len(t) >= 5 and t not in GENERIC_NAME_WORDS]
    if distinctive:
        return any(t in lead for t in distinctive)
    phrase = " ".join(toks).strip()
    return bool(phrase) and phrase in lead


def _parse_dt(iso: str):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        try:
            return email.utils.parsedate_to_datetime(iso)
        except Exception:
            return None


def is_recent(a: Article, cfg: Config) -> bool:
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
    if any(pat in low for pat in NO_NEWS_PATTERNS):
        return True
    return any(rx.search(low) for rx in NO_NEWS_REGEXES)


# Material-signal scoring: how "important" a story is. Used to gate out fluff and
# to rank items when trimming to the page budget.
_IMP_MONEY = re.compile(r"[£$€]\s?\d")
_IMP_PCT = re.compile(r"\d+(?:\.\d+)?\s?%")
_IMP_EVENTS = [
    r"acquisition|acquires?|acquired|acquiring|merger|takeover|buyback|repurchase",
    r"dividend|guidance|outlook|profit warning",
    r"contract|order|awarded|secured|mandate|tender",
    r"\beps\b|revenue|profit|ebitda|margin|earnings|net income|\bloss\b|pre-?tax",
    r"ceo|cfo|chief executive|chair|appoint|resign|steps? down|stepping down|leaving|succeed",
    r"investigation|lawsuit|probe|\bfine\b|regulatory|delist|sanction",
    r"placing|fundraise|rights issue|convertible|senior notes|\bstake\b|disposal|equity raise",
    r"launch|expansion|expand|partnership|joint venture|\bresults\b|acquire",
]


def importance_score(text: str) -> int:
    low = (text or "").lower()
    s = 0
    if _IMP_MONEY.search(text or ""):
        s += 2
    if _IMP_PCT.search(text or ""):
        s += 1
    for pat in _IMP_EVENTS:
        if re.search(pat, low):
            s += 1
    return s


def is_material(text: str, cfg: Config) -> bool:
    """Keep only stories carrying a real corporate signal; drop fluff / no-news."""
    if is_no_news(text):
        return False
    return importance_score(text) >= cfg.min_importance


def is_price_noise(text: str) -> bool:
    """True if the text is dominated by share-price / valuation chatter and does
    not report a real corporate event."""
    low = (text or "").lower()
    hits = sum(1 for pat in PRICE_NOISE_PATTERNS if re.search(pat, low))
    hits += sum(1 for pat in VALUATION_PATTERNS if re.search(pat, low))
    if hits >= 2 and not any(re.search(p, low) for p in STRONG_EVENT_PATTERNS):
        return True
    return False


def is_admin_filler(text: str) -> bool:
    """Routine RNS boilerplate / index-membership admin — no investment value."""
    low = (text or "").lower()
    return any(re.search(p, low) for p in ADMIN_FILLER_PATTERNS)


def is_promotional(text: str) -> bool:
    """True for consumer marketing / promo PR (offers, discounts, sales) — not
    material to whether the stock is worth holding."""
    low = (text or "").lower()
    if any(pat in low for pat in PROMO_PATTERNS):
        return True
    if any(re.search(rx, low) for rx in PROMO_REGEXES):
        return True
    return False


def is_low_quality(text: str) -> bool:
    return (is_no_news(text) or is_price_noise(text) or is_promotional(text)
            or is_admin_filler(text))


def filter_articles(articles: list[Article], company: Company, cfg: Config) -> list[Article]:
    kept = []
    for a in articles:
        if is_blocklisted(a) or not is_relevant(a, company):
            continue
        if not is_recent(a, cfg):
            continue
        if a.kind != "filing" and is_low_quality(f"{a.headline}  {a.snippet}"):
            continue
        kept.append(a)
    return kept


# ============================================================================
# STEP 4 — Dedupe
# ============================================================================
def _title_key(a: Article) -> str:
    t = _norm(a.headline)
    t = re.sub(r"\b(the|a|an|to|of|for|and|in|on|at|as|is|are|with|by)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _url_key(a: Article) -> str:
    u = (a.final_url or a.url or "").lower()
    u = re.sub(r"^https?://(www\.)?", "", u).split("?")[0].split("#")[0].rstrip("/")
    return u


def dedupe(articles: list[Article], cfg: Config) -> list[Article]:
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


def _is_trusted(a: Article) -> bool:
    u = (a.final_url or a.url or "").lower()
    return any(site in u for site in TRUSTED_SITES)


def _bullet_tokens(a: Article, company: Company) -> set:
    ctoks = set(_company_tokens(company.name))
    stop = {"plc", "group", "the", "and", "its", "has", "will", "to", "of", "in",
            "for", "on", "with", "as", "is", "by", "at", "this", "that", "from",
            "be", "are", "new", "an", "a", "it", "was", "were", "amid", "over"}
    toks = set(_norm(f"{a.headline} {a.bullet}").split())
    return {t for t in toks if len(t) > 2 and t not in ctoks and t not in stop}


def _distinctive(tokens: set) -> set:
    return {t for t in tokens if len(t) >= 7 or any(ch.isdigit() for ch in t)}


def _same_event(a: Article, b: Article, company: Company, cfg: Config) -> bool:
    if difflib.SequenceMatcher(None, (a.bullet or "").lower(),
                               (b.bullet or "").lower()).ratio() >= cfg.merge_text_ratio:
        return True
    ta, tb = _bullet_tokens(a, company), _bullet_tokens(b, company)
    if ta and tb:
        if len(ta & tb) / len(ta | tb) >= cfg.merge_token_overlap:
            return True
        if len(_distinctive(ta) & _distinctive(tb)) >= cfg.merge_distinctive:
            return True
    return False


def _merge_reps(reps: list, company: Company, cfg: Config) -> list:
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


def _heuristic_groups(articles: list[Article], company: Company, cfg: Config) -> list[list[Article]]:
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


def _llm_group(articles: list[Article], company: Company, cfg: Config) -> list[list[Article]]:
    listing = "\n".join(f"{i + 1}. {a.bullet}" for i, a in enumerate(articles))
    prompt = (
        f'These numbered news bullets are about "{company.name}". Group together any '
        f"bullets that report the SAME underlying event or announcement, even if worded "
        f"differently or seemingly contradictory. Reply with ONLY a JSON array of arrays "
        f"of bullet numbers, each number used exactly once. Example: [[1,3],[2],[4,5]].\n\n"
        f"{listing}"
    )
    txt = _llm_complete(prompt, cfg)
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        raise ValueError("no JSON in response")
    groups = json.loads(m.group(0))
    flat = sorted(int(n) for g in groups for n in g)
    if flat != list(range(1, len(articles) + 1)):
        raise ValueError("grouping does not cover every bullet exactly once")
    return [[articles[int(n) - 1] for n in g] for g in groups]


def _pick_representative(group: list[Article]):
    if not group:
        return None
    pool = [a for a in group if _is_trusted(a)] or group
    return max(pool, key=lambda a: len(a.bullet or ""))


def _derive_heading(members: list[Article], company: Company) -> str:
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


def _llm_organise(articles: list[Article], company: Company, cfg: Config):
    """Ask the model to group bullets by topic, label each group, and merge each
    group into a single clear point. Returns [(heading, summary, members), ...]."""
    listing = "\n".join(f"{i + 1}. {a.bullet}" for i, a in enumerate(articles))
    prompt = (
        f'Organise this week\'s news bullets about "{company.name}" for an investor '
        f"deciding whether the stock is worth holding. Group bullets that concern the "
        f"SAME topic or event. For each group give: a short 2-4 word \"heading\" (topic "
        f"label), the member bullet numbers, and one \"summary\" (one or two sentences, "
        f"max ~45 words) that merges the members into a single clear point. Reply with "
        f"ONLY JSON like [{{\"heading\": \"...\", \"members\": [1,2], \"summary\": \"...\"}}]. "
        f"Every bullet number must appear exactly once.\n\n{listing}"
    )
    # Same bullets -> same prompt -> reuse the answer (bullets are cached too, so a
    # re-run with nothing new skips the LLM entirely). An unusable answer is
    # remembered as well, so the same input goes straight to the heuristic next
    # time instead of paying for the same bad reply again. Network/API errors
    # raise out of _llm_complete before anything is stored, so those are retried.
    provider, model = cfg.llm_provider.lower(), _model_name(cfg)
    key = "organise:" + hashlib.sha1(prompt.encode("utf-8")).hexdigest()
    cached = _CACHE.get_bullet(key, provider, model)
    if cached == _UNUSABLE:
        raise ValueError("model gave an unusable grouping for these bullets before")
    txt = cached or _llm_complete(prompt, cfg, max_tokens=500)
    try:
        m = re.search(r"\[.*\]", txt, re.S)
        if not m:
            raise ValueError("no JSON in response")
        data = json.loads(m.group(0))
        flat = sorted(int(n) for g in data for n in g["members"])
        if flat != list(range(1, len(articles) + 1)):
            raise ValueError("grouping does not cover every bullet exactly once")
    except (ValueError, TypeError, KeyError) as e:   # JSONDecodeError is a ValueError
        if not cached:
            _CACHE.put_bullet(key, provider, model, _UNUSABLE)
        raise ValueError(f"unusable grouping: {e}") from e
    if not cached:
        _CACHE.put_bullet(key, provider, model, txt)
    groups = []
    for g in data:
        members = [articles[int(n) - 1] for n in g["members"]]
        groups.append((str(g.get("heading", "")).strip(),
                       str(g.get("summary", "")).strip(), members))
    return groups


def _heuristic_organise(articles: list[Article], company: Company, cfg: Config):
    """Deterministic fallback: cluster by text similarity, keep the best bullet."""
    groups = []
    for cluster in _heuristic_groups(articles, company, cfg):
        rep = _pick_representative(cluster)
        groups.append((_derive_heading(cluster, company), rep.bullet, cluster))
    return groups


def organise(articles: list[Article], company: Company, cfg: Config) -> list[Article]:
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
            groups = _heuristic_organise(news, company, cfg)

    out = []
    for heading, summary, members in groups:
        if not members:
            continue
        rep = _pick_representative(members)
        rep.heading = heading or ""
        if summary:
            rep.bullet = _tidy_bullet(summary)
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


# ============================================================================
# STEP 5 — Summarise to a bullet (Ollama, with offline fallback)
# ============================================================================
def _fallback_bullet(a: Article) -> str:
    content = a.body if (a.body and len(a.body) >= 120) else a.snippet
    if a.kind == "filing":
        form = re.sub(r"^SEC\s+|\s+filing\b.*$", "", a.headline).strip() or "Regulatory"
        desc = (a.snippet or "").strip()
        if desc and desc.upper() != form.upper():
            return _tidy_bullet(f"{form} filed with the SEC: {desc.capitalize()}")
        return f"{form} filing published on SEC EDGAR."
    content = (content or "").strip()
    if len(content) < 40:
        # A fragment (e.g. a bare source name in the feed description) makes a
        # worse bullet than the headline itself.
        return _tidy_bullet(a.headline or content)
    sents = re.split(r"(?<=[.!?])\s+", content)
    lead = " ".join(sents[:2]).strip()
    return _tidy_bullet(lead or a.headline)


_BART_PIPE = None


def _load_bart(cfg: Config):
    """Lazily load the local BART pipeline. transformers/torch are imported here
    (not at module top) so the rest of the tool runs without them."""
    global _BART_PIPE
    if _BART_PIPE is not None:
        return _BART_PIPE
    from transformers import pipeline, AutoTokenizer, AutoModelForSeq2SeqLM
    src = cfg.bart_model_dir
    if not os.path.isfile(os.path.join(src, "config.json")):
        print(f"      BART: no local model at {src} — using {cfg.bart_hub_id} from the hub")
        src = cfg.bart_hub_id
    tok = AutoTokenizer.from_pretrained(src)
    model = AutoModelForSeq2SeqLM.from_pretrained(src)
    _BART_PIPE = pipeline("summarization", model=model, tokenizer=tok)
    return _BART_PIPE


def _bart_summarise(text: str, cfg: Config) -> str:
    """Summarise article text into one short bullet with local BART."""
    pipe = _load_bart(cfg)
    clipped = " ".join((text or "").split()[: cfg.bart_max_input_words])
    out = pipe(clipped, max_length=cfg.bart_max_tokens, min_length=cfg.bart_min_tokens,
               do_sample=False, truncation=True, no_repeat_ngram_size=3)
    return _tidy_bullet(out[0]["summary_text"])


def _llm_complete(prompt: str, cfg: Config, max_tokens: int = 160) -> str:
    """Single LLM call with a hard timeout. Raises on failure (caller falls back)."""
    p = cfg.llm_provider.lower()
    if p == "bart":
        raise RuntimeError("bart provider is summarise-only; use _bart_summarise")
    if p == "ollama":
        if OpenAI is None:
            raise RuntimeError("openai package not installed")
        client = OpenAI(base_url=cfg.ollama_base_url, api_key="ollama",
                        timeout=cfg.llm_timeout, max_retries=0)
        r = client.chat.completions.create(model=cfg.ollama_model, max_tokens=max_tokens,
                                           timeout=cfg.llm_timeout,
                                           messages=[{"role": "user", "content": prompt}])
        return r.choices[0].message.content.strip()
    if p == "openai":
        if OpenAI is None:
            raise RuntimeError("openai package not installed")
        if not env_key("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set")
        client = OpenAI(api_key=env_key("OPENAI_API_KEY"),
                        timeout=cfg.llm_timeout, max_retries=0)
        r = client.chat.completions.create(model=cfg.openai_model, max_tokens=max_tokens,
                                           timeout=cfg.llm_timeout,
                                           messages=[{"role": "user", "content": prompt}])
        return r.choices[0].message.content.strip()
    if p == "anthropic":
        if anthropic is None:
            raise RuntimeError("anthropic package not installed")
        if not env_key("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        client = anthropic.Anthropic(api_key=env_key("ANTHROPIC_API_KEY"),
                                     timeout=cfg.llm_timeout, max_retries=0)
        r = client.messages.create(model=cfg.anthropic_model, max_tokens=max_tokens,
                                   messages=[{"role": "user", "content": prompt}])
        return r.content[0].text.strip()
    raise RuntimeError(f"unknown provider '{p}'")


def summarise_bullet(a: Article, company: Company, cfg: Config) -> str:
    if a.kind == "filing":
        return _fallback_bullet(a)
    content = a.body if (a.body and len(a.body) >= 120) else a.snippet
    if not content or len(content) < 60:
        return _fallback_bullet(a)
    provider, model, key = cfg.llm_provider.lower(), _model_name(cfg), _orig_key(a)
    cached = _CACHE.get_bullet(key, provider, model)
    if cached:
        return cached
    if provider == "bart":
        try:
            text = _bart_summarise(content[: cfg.max_article_chars], cfg)
        except Exception as e:
            print(f"      BART bullet failed ({e.__class__.__name__}) — using fallback")
            return _fallback_bullet(a)
        _CACHE.put_bullet(key, provider, model, text)
        return text or _fallback_bullet(a)
    prompt = (
        f'Summarise this news about "{company.name}" as ONE concise bullet point '
        f"(one or two sentences, max ~40 words) for an investor deciding whether the "
        f"stock is worth holding, and for competitive intelligence. State only what is "
        f"materially relevant: earnings, revenue, margins, M&A, contracts/orders, "
        f"capacity or market expansion, guidance, leadership, regulatory or competitive "
        f"positioning. Ignore marketing, discounts, promotions and consumer offers. No "
        f"preamble, no 'the article', no links, no bullet character.\n\n"
        f"HEADLINE: {a.headline}\n\n{content[: cfg.max_article_chars]}"
    )
    try:
        text = _tidy_bullet(_llm_complete(prompt, cfg))
    except Exception as e:
        print(f"      LLM bullet failed ({e.__class__.__name__}) — using fallback")
        return _fallback_bullet(a)   # fallbacks are never cached, so a later run retries the LLM
    _CACHE.put_bullet(key, provider, model, text)
    return text or _fallback_bullet(a)


# ============================================================================
# STEP 6 — Publish (presentable PDF + DOCX)
# ============================================================================
NAVY = HexColor("#0b3d62")
BLUE = HexColor("#1a73e8")
GREY = HexColor("#5f6368")
DARK = HexColor("#202124")
LINE = HexColor("#e0e0e0")


def _tiers_label(cfg: Config) -> str:
    ts = sorted(set(cfg.tiers))
    if len(ts) == 1:
        return str(ts[0])
    if len(ts) == 2:
        return f"{ts[0]} & {ts[1]}"
    return ", ".join(str(t) for t in ts[:-1]) + f" & {ts[-1]}"


def _tiers_fname(cfg: Config) -> str:
    return "-".join(str(t) for t in sorted(set(cfg.tiers)))


def _window_label(cfg: Config) -> str:
    end = datetime.now()
    start = end - timedelta(days=cfg.days_back)
    return f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"


def _fmt_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d %b %Y")
    except Exception:
        return iso[:10]


def build_pdf(companies: list[Company], cfg: Config, path: str) -> str:
    styles = getSampleStyleSheet()

    def S(name, **kw):
        return ParagraphStyle(name, parent=styles["Normal"], **kw)

    title = ParagraphStyle("title", parent=styles["Title"], fontSize=22, textColor=NAVY,
                           spaceAfter=2, alignment=0)
    sub = S("sub", fontSize=9.5, textColor=GREY, spaceAfter=14)
    co_style = ParagraphStyle("co", parent=styles["Heading2"], fontSize=14, textColor=NAVY,
                              spaceBefore=14, spaceAfter=1)
    meta = S("meta", fontSize=8, textColor=GREY, spaceAfter=6)
    subhead = ParagraphStyle("subhead", parent=styles["Normal"], fontName="Helvetica-Bold",
                             fontSize=10.5, textColor=NAVY, spaceBefore=7, spaceAfter=1)
    bullet = S("bullet", fontSize=10.5, textColor=DARK, leading=15,
               leftIndent=16, firstLineIndent=-8, spaceBefore=2)
    link_s = S("link", fontSize=8, textColor=GREY, leading=12, spaceAfter=4, leftIndent=16)
    note = S("note", fontSize=10, textColor=GREY, leading=14, spaceAfter=4)

    def esc(s):
        return html.escape(s or "")

    doc = SimpleDocTemplate(path, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            title=f"Tier {_tiers_label(cfg)} Weekly News Digest")
    flow = [
        Paragraph(f"Tier {_tiers_label(cfg)} Weekly News Digest", title),
        Paragraph(f"Week of {_window_label(cfg)}&nbsp;&nbsp;|&nbsp;&nbsp;"
                  f"Generated {datetime.now().strftime('%A, %d %B %Y')}&nbsp;&nbsp;|&nbsp;&nbsp;"
                  f"Sources: Google News, trusted outlets, official market feeds", sub),
        HRFlowable(width="100%", thickness=1.2, color=NAVY, spaceAfter=4),
    ]

    for c in companies:
        bits = " · ".join(b for b in [c.exchange, c.currency, (f"Tier {c.tier}" if c.tier else "")] if b)
        header = [Paragraph(esc(c.name), co_style)]
        if bits:
            header.append(Paragraph(esc(bits), meta))
        units = []
        if not c.articles:
            units.append([Paragraph(c.note or "No material news in this window.", note)])
        else:
            for a in c.articles:
                unit = []
                if a.heading:
                    unit.append(Paragraph(esc(a.heading), subhead))
                link = a.final_url or a.url
                tail = " — ".join(x for x in [esc(a.source), _fmt_date(a.published)] if x)
                unit.append(Paragraph(
                    f'<font color="#1a73e8">•</font>&nbsp;&nbsp;{esc(a.bullet)}', bullet))
                sub_bits = []
                if tail:
                    sub_bits.append(tail)
                if link:
                    sub_bits.append(f'<link href="{esc(link)}"><font color="#1a73e8">Read full article »</font></link>')
                for ex in a.extra_sources:
                    exl, exs = ex.get("url"), ex.get("source") or "source"
                    if exl:
                        sub_bits.append(f'<link href="{esc(exl)}"><font color="#1a73e8">{esc(exs)} »</font></link>')
                if sub_bits:
                    unit.append(Paragraph(" &nbsp;·&nbsp; ".join(sub_bits), link_s))
                units.append(unit)
        if units:
            units[0] = header + units[0]
        else:
            units = [header]
        for unit in units:
            flow.append(KeepTogether(unit))
        flow.append(Spacer(1, 4))
        flow.append(HRFlowable(width="100%", thickness=0.5, color=LINE))

    flow.append(Spacer(1, 8))
    flow.append(Paragraph("Generated automatically · summaries are AI-assisted and should be "
                          "verified against the linked source before any decision.",
                          S("foot", fontSize=7.5, textColor=GREY)))
    doc.build(flow)
    return path


def _w_el(tag: str, val: str | None = None):
    """Create a WordprocessingML element, optionally with a w:val attribute."""
    el = OxmlElement(tag)
    if val is not None:
        el.set(qn("w:val"), val)
    return el


def _docx_hyperlink(paragraph, url, text, color="1a73e8"):
    """Insert a real clickable hyperlink run into a python-docx paragraph."""
    part = paragraph.part
    r_id = part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    for el in (_w_el("w:color", color), _w_el("w:u", "single"), _w_el("w:sz", "16")):
        rPr.append(el)
    new_run.append(rPr)
    t = OxmlElement("w:t")
    t.text = text
    new_run.append(t)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)
    return hyperlink


_DOCX_GREY = RGBColor(0x5f, 0x63, 0x68)
_DOCX_NAVY = RGBColor(0x0b, 0x3d, 0x62)


def _style_run(run, size: float, color=None, bold: bool = False, italic: bool = False):
    run.font.size = Pt(size)
    if color is not None:
        run.font.color.rgb = color
    if bold:
        run.bold = True
    if italic:
        run.italic = True
    return run


def build_docx(companies: list[Company], cfg: Config, path: str) -> str:
    d = docx.Document()
    d.add_heading(f"Tier {_tiers_label(cfg)} Weekly News Digest", level=0)
    sub = d.add_paragraph()
    _style_run(sub.add_run(f"Week of {_window_label(cfg)}   |   Generated "
                           f"{datetime.now().strftime('%A, %d %B %Y')}   |   "
                           f"Google News · trusted outlets · official market feeds"),
               9, _DOCX_GREY, italic=True)

    for c in companies:
        ch = d.add_heading(c.name, level=1)
        ch.paragraph_format.keep_with_next = True
        bits = " · ".join(b for b in [c.exchange, c.currency, (f"Tier {c.tier}" if c.tier else "")] if b)
        if bits:
            m = d.add_paragraph()
            _style_run(m.add_run(bits), 8.5, _DOCX_GREY)
            m.paragraph_format.keep_with_next = True
        if not c.articles:
            p = d.add_paragraph(c.note or "No material news in this window.")
            p.runs[0].italic = True
            p.paragraph_format.keep_together = True
            continue
        for a in c.articles:
            if a.heading:
                hp = d.add_paragraph()
                _style_run(hp.add_run(a.heading), 10.5, _DOCX_NAVY, bold=True)
                hp.paragraph_format.space_before = Pt(6)
                hp.paragraph_format.space_after = Pt(0)
                hp.paragraph_format.keep_with_next = True
                hp.paragraph_format.keep_together = True
            p = d.add_paragraph(style="List Bullet")
            p.add_run(a.bullet)
            p.paragraph_format.keep_together = True
            p.paragraph_format.keep_with_next = True
            sub = d.add_paragraph()
            sub.paragraph_format.left_indent = Inches(0.3)
            sub.paragraph_format.keep_together = True
            tail = " · ".join(x for x in [a.source, _fmt_date(a.published)] if x)
            if tail:
                _style_run(sub.add_run(tail + "  "), 8, _DOCX_GREY)
            link = a.final_url or a.url
            if link:
                _docx_hyperlink(sub, link, "Read full article »")
            for ex in a.extra_sources:
                exl = ex.get("url")
                if exl:
                    _style_run(sub.add_run("  ·  "), 8, _DOCX_GREY)
                    _docx_hyperlink(sub, exl, f"{ex.get('source') or 'source'} »")

    foot = d.add_paragraph()
    fr = foot.add_run("Generated automatically · summaries are AI-assisted and should be "
                      "verified against the linked source before any decision.")
    fr.italic = True
    fr.font.size = Pt(7.5)
    fr.font.color.rgb = RGBColor(0x5f, 0x63, 0x68)
    d.save(path)
    return path


# ============================================================================
# Orchestrator
# ============================================================================
def apply_page_budget(companies: list[Company], cfg: Config) -> list[Company]:
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


def _published_keys(a: Article) -> list[str]:
    keys = [_orig_key(a), _url_key(a)]
    keys += [re.sub(r"^https?://(www\.)?", "", (e.get("url") or "").lower()).split("#")[0].rstrip("/")
             for e in a.extra_sources]
    return [k for k in keys if k]


def run(cfg: Config) -> list[str]:
    """Run the whole digest. Returns the paths written (empty if nothing to publish).

    Work happens in phases, each spread over a small thread pool:
      1. discover + pre-filter every company     (cfg.discovery_workers)
      2. extract every candidate: cache -> HTTP -> Chrome
      3. summarise every kept story               (cfg.llm_workers)
      4. quality gate + topic grouping per company (cfg.llm_workers)
      5. page budget, publish, remember what was published
    """
    global _CACHE
    t_start = time.time()
    timings: dict = {}

    def lap(name: str, since: float) -> float:
        now = time.time()
        timings[name] = now - since
        return now

    if cfg.companies:
        print("Companies from the command line ...")
    else:
        print(f"Loading Tier {_tiers_label(cfg)} companies from {os.path.basename(cfg.watchlist)} ...")
    try:
        companies = load_companies(cfg)
    except (WatchlistError, OSError, json.JSONDecodeError) as e:
        print(f"Watchlist problem: {e}\nRun with --check to see how the file is being read.")
        return []
    print(f"  -> {len(companies)} companies: {', '.join(c.name for c in companies)}\n")
    if not companies:
        print("No companies found for the selected tiers. Run with --check to see what the watchlist holds.")
        return []

    _CACHE = Cache(cfg.cache_path if cfg.use_cache else None)
    if _CACHE.enabled:
        print(f"Cache: {os.path.basename(cfg.cache_path)}"
              + ("  (new-only: skipping stories already published)" if cfg.new_only else ""))
    try:
        # 1. Discover. Cheap pre-filter BEFORE the slow extraction step: drop
        # blocklisted, off-topic, stale and low-quality items (judged on
        # headline + snippet) and dedupe, so we only open articles we might keep.
        t = time.time()
        print(f"[1/5] Discovering news ({cfg.discovery_workers} companies at a time) ...")

        def discover_one(c):
            raw = discover(c, cfg)
            return len(raw), dedupe(filter_articles(raw, c, cfg), cfg)

        found = _pmap(discover_one, companies, cfg.discovery_workers)
        candidates = {}
        for c, (n_raw, cands) in zip(companies, found):
            candidates[id(c)] = cands
            print(f"   {c.name}: discovered {n_raw} -> {len(cands)} candidates")
        t = lap("discover", t)

        # 2. Extract every candidate of every company in one go.
        everything = [a for c in companies for a in candidates[id(c)]]
        print(f"[2/5] Reading {len(everything)} articles ...")
        stats = extract_all(everything, cfg)
        print(f"   {stats['cached']} from cache, {stats['http']} via HTTP, "
              f"{stats['browser']} via Chrome, {stats['unreadable']} unreadable")
        t = lap("extract", t)

        # 3. Pick what to summarise: readable, not blocklisted now that the real
        # publisher URL is known (before extraction it was a news.google.com
        # redirect), optionally not already published, capped per company.
        selected = {}
        for c in companies:
            kept = [a for a in candidates[id(c)]
                    if (a.kind == "filing" or a.authentic) and not is_blocklisted(a)]
            if cfg.new_only:
                kept = [a for a in kept if not _CACHE.was_published(_orig_key(a), _url_key(a))]
            selected[id(c)] = kept[: cfg.max_articles_per_company]
        jobs = [(a, c) for c in companies for a in selected[id(c)]]
        print(f"[3/5] Summarising {len(jobs)} stories ({cfg.llm_provider}, {_model_name(cfg)}) ...")
        bullets = _pmap(lambda ac: summarise_bullet(ac[0], ac[1], cfg), jobs, cfg.llm_workers)
        for (a, _), b in zip(jobs, bullets):
            a.bullet = b
        t = lap("summarise", t)

        # 4. Backstop quality gate, importance score, then topic grouping.
        print("[4/5] Grouping by topic ...")

        def finish(c):
            kept = [a for a in selected[id(c)] if a.kind == "filing"
                    or (not is_low_quality(a.bullet) and is_material(a.bullet, cfg)
                        and strongly_relevant(a, c))]
            for a in kept:
                a.importance = importance_score(a.bullet)
            return organise(kept, c, cfg)

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
            return []

        stamp = datetime.now().strftime("%Y-%m-%d")
        # Output files are always named "news digest <date>" (see README).
        pdf = os.path.join(cfg.output_dir, f"news digest {stamp}.pdf")
        docx_path = os.path.join(cfg.output_dir, f"news digest {stamp}.docx")
        build_pdf(companies, cfg, pdf)
        build_docx(companies, cfg, docx_path)
        for c in companies:
            for a in c.articles:
                _CACHE.mark_published(_published_keys(a), c.name, stamp)
        lap("publish", t)
        print(f"Done in {time.time() - t_start:.0f}s  ("
              + ", ".join(f"{k} {v:.0f}s" for k, v in timings.items()) + ")")
        print(f"  PDF : {pdf}\n  DOCX: {docx_path}")
        return [pdf, docx_path]
    finally:
        _CACHE.close()
        _CACHE = Cache(None)


def parse_args() -> Config:
    cfg = Config()
    p = argparse.ArgumentParser(description="Tier company weekly news digest (v3)")
    w = p.add_argument_group("watchlist (your own company list)")
    w.add_argument("--watchlist", "-w", default=None, metavar="FILE",
                   help=".xlsx, .csv, .tsv, .json or .txt (default: $WATCHLIST, watchlist.* "
                        "or 'Company List.xlsx' next to the script)")
    w.add_argument("--sheet", default=None, help="Excel sheet to read (default: first with a name column)")
    w.add_argument("--map", default="", metavar="FIELD=HEADER,...", dest="column_map",
                   help="map your column headers, e.g. --map \"name=Issuer Name,tier=Priority\"")
    w.add_argument("--company", "-c", action="append", default=[], metavar="NAME",
                   help="run for this company without a file (repeatable)")
    w.add_argument("--check", action="store_true",
                   help="show how the watchlist is read (columns, tiers) and exit")
    w.add_argument("--init-watchlist", default=None, metavar="FILE",
                   help="write a starter watchlist (.xlsx, .csv or .json) and exit")
    p.add_argument("--doctor", action="store_true",
                   help="check the setup (Chrome, LLM, watchlist) and test your API keys, then exit")
    p.add_argument("--tier", type=int, default=None, help="run a single tier only")
    p.add_argument("--tiers", default=None, help="comma-separated tiers, e.g. 1,2 (default)")
    p.add_argument("--days", type=int, default=cfg.days_back, dest="days_back")
    p.add_argument("--provider", default=cfg.llm_provider,
                   choices=["ollama", "openai", "anthropic", "bart"], dest="llm_provider")
    p.add_argument("--model", default=None, help="override the model for the chosen provider")
    p.add_argument("--headless", action="store_true", default=cfg.headless)
    p.add_argument("--no-trusted", action="store_false", dest="use_trusted_sites")
    # --finnhub / --no-finnhub and --sec / --no-sec. Defaults come from Config, so
    # editing Config actually takes effect (previously --finnhub defaulted to off
    # and silently overrode Config.use_finnhub = True).
    p.add_argument("--finnhub", action=argparse.BooleanOptionalAction,
                   default=cfg.use_finnhub, dest="use_finnhub",
                   help="Finnhub news (North America; needs FINNHUB_API_KEY)")
    p.add_argument("--sec", action=argparse.BooleanOptionalAction,
                   default=cfg.use_sec_edgar, dest="use_sec_edgar",
                   help="SEC EDGAR filings (US-listed names)")
    p.add_argument("--no-cache", action="store_false", dest="use_cache", default=cfg.use_cache,
                   help="ignore and don't write the SQLite cache (fetch everything fresh)")
    p.add_argument("--new-only", action="store_true", default=cfg.new_only,
                   help="leave out stories already published in an earlier digest")
    p.add_argument("--browsers", type=int, default=cfg.browser_workers, metavar="N",
                   help=f"Chrome windows to run in parallel (default {cfg.browser_workers})")
    a = p.parse_args()
    if a.watchlist:
        cfg.watchlist = os.path.abspath(a.watchlist)
    cfg.sheet_name = a.sheet
    try:
        cfg.column_map = parse_column_map(a.column_map)
    except WatchlistError as e:
        p.error(str(e))
    cfg.companies = a.company
    cfg.check_only = a.check
    cfg.init_watchlist = a.init_watchlist
    cfg.doctor = a.doctor
    if a.tiers:
        cfg.tiers = tuple(int(x) for x in a.tiers.split(",") if x.strip())
    elif a.tier is not None:
        cfg.tiers = (a.tier,)
    cfg.days_back = a.days_back
    cfg.llm_provider = a.llm_provider
    cfg.headless = a.headless
    cfg.use_trusted_sites = a.use_trusted_sites
    cfg.use_finnhub = a.use_finnhub
    cfg.use_sec_edgar = a.use_sec_edgar
    cfg.use_cache = a.use_cache
    cfg.new_only = a.new_only
    cfg.browser_workers = max(1, a.browsers)
    if a.model:
        if a.llm_provider == "ollama":
            cfg.ollama_model = a.model
        elif a.llm_provider == "openai":
            cfg.openai_model = a.model
        elif a.llm_provider == "bart":
            cfg.bart_model_dir = a.model
        else:
            cfg.anthropic_model = a.model
    return cfg


def _probe(url: str, headers: dict, timeout: float = 15) -> int | str:
    """HTTP status of a GET (200, 401, ...), or a short error string if unreachable."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return e.__class__.__name__


def doctor(cfg: Config) -> int:
    """--doctor: check this machine's setup and test every key that is set.
    Secrets are never printed. Returns 1 if anything needed for a run is broken."""
    import importlib
    import platform
    import shutil
    results = []   # (status, item, detail)

    def say(status, item, detail=""):
        results.append(status)
        print(f"  [{status:<4}] {item:<20} {detail}")

    print("Setup check\n")
    v = tuple(int(x) for x in platform.python_version_tuple()[:2])
    say("OK" if v >= (3, 10) else "FAIL", "Python", platform.python_version()
        + ("" if v >= (3, 10) else "  (3.10+ needed)"))
    for mod, pkg in [("openpyxl", "openpyxl"), ("bs4", "beautifulsoup4"), ("selenium", "selenium"),
                     ("trafilatura", "trafilatura"), ("reportlab", "reportlab"), ("docx", "python-docx"),
                     ("dotenv", "python-dotenv"), ("openai", "openai")]:
        try:
            importlib.import_module(mod)
        except Exception:
            say("FAIL", pkg, "not installed: pip install -r requirements.txt")
    chrome = (shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")
              or next((p for p in [
                  os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                  os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                  os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"] if os.path.exists(p)), None))
    say("OK" if chrome else "FAIL", "Chrome", "found" if chrome else "not found: install Google Chrome")

    try:
        companies, _ = read_watchlist(cfg) if not cfg.companies else (load_companies(cfg), None)
        say("OK" if companies else "WARN", "Watchlist",
            f"{len(companies)} companies in {os.path.basename(cfg.watchlist) if not cfg.companies else '--company'}")
    except Exception as e:
        say("FAIL", "Watchlist", str(e))

    # Summariser
    p = cfg.llm_provider.lower()
    if p == "ollama":
        base = cfg.ollama_base_url.rstrip("/")
        try:
            with urllib.request.urlopen(base + "/models", timeout=5) as r:
                models = [m.get("id", "") for m in json.loads(r.read()).get("data", [])]
            if cfg.ollama_model in models:
                say("OK", "Ollama", f"running, {cfg.ollama_model} available")
            else:
                say("FAIL", "Ollama", f"running, but {cfg.ollama_model} missing: ollama pull {cfg.ollama_model}")
        except Exception:
            say("FAIL", "Ollama", "not reachable: start Ollama, or use --provider openai/anthropic "
                                  "(the digest falls back to plain extracts without an LLM)")
    elif p == "bart":
        try:
            importlib.import_module("transformers")
            say("OK", "BART", "transformers installed")
        except Exception:
            say("FAIL", "BART", "pip install -r requirements-bart.txt")

    for var, label, url, hdr in [
        ("OPENAI_API_KEY", "OpenAI key", "https://api.openai.com/v1/models",
         lambda k: {"Authorization": f"Bearer {k}"}),
        ("ANTHROPIC_API_KEY", "Anthropic key", "https://api.anthropic.com/v1/models",
         lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"}),
    ]:
        needed = p == var.split("_")[0].lower()
        k = env_key(var)
        if not k:
            if needed:
                say("FAIL", label, f"not set: add {var}= to .env")
            continue
        code = _probe(url, hdr(k))
        say("OK" if code == 200 else "FAIL", label,
            "valid" if code == 200 else ("rejected (401): check the key" if code == 401 else f"test failed: {code}"))

    # Finnhub (optional)
    k = env_key("FINNHUB_API_KEY")
    if not cfg.use_finnhub:
        say("--", "Finnhub", "turned off (--no-finnhub)")
    elif not k:
        say("--", "Finnhub", "no key (optional): free key at finnhub.io/register, then FINNHUB_API_KEY= in .env")
    else:
        code = _probe(f"{FINNHUB_API}/quote?symbol=AAPL", {"X-Finnhub-Token": k})
        if code == 200:
            say("OK", "Finnhub", "key valid")
        elif code == 429:
            say("OK", "Finnhub", "key valid (rate-limited right now: free plan is 60 calls/min)")
        elif code == 401:
            say("FAIL", "Finnhub", "key rejected (401): copy it again from finnhub.io/dashboard, "
                                   "or remove it / use --no-finnhub")
        else:
            say("WARN", "Finnhub", f"test failed: {code}")

    # SEC EDGAR (US listings)
    ua = os.environ.get("SEC_USER_AGENT", "").strip().strip("\"'").strip()
    if not cfg.use_sec_edgar:
        say("--", "SEC EDGAR", "turned off (--no-sec)")
    elif not re.search(r"\S+@\S+\.\S+", ua):
        say("WARN", "SEC EDGAR", "SEC_USER_AGENT " + ("has no email" if ua else "not set")
            + ": add SEC_USER_AGENT=Your Name you@domain.com to .env")
    else:
        day = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
        code = _probe("https://efts.sec.gov/LATEST/search-index?q=%228-K%22&dateRange=custom"
                      f"&startdt={day}&enddt={day}", {"User-Agent": ua, "Accept": "application/json"})
        say("OK" if code == 200 else "WARN", "SEC EDGAR",
            "contact set, EDGAR reachable" if code == 200 else f"contact set, but EDGAR test returned {code}")

    fails, warns = results.count("FAIL"), results.count("WARN")
    print("\n" + ("All good." if not fails and not warns else
                  f"{fails} problem(s), {warns} warning(s). Keys go in .env (copy .env.example)."))
    return 1 if fails else 0


def main() -> int:
    cfg = parse_args()
    if cfg.doctor:
        return doctor(cfg)
    if cfg.init_watchlist:
        try:
            write_watchlist_template(cfg.init_watchlist)
        except WatchlistError as e:
            print(e)
            return 1
        print(f"Wrote {cfg.init_watchlist}. Replace the sample rows with your companies, then run:\n"
              f'  python "news digest.py" --watchlist "{cfg.init_watchlist}" --check')
        return 0
    if cfg.check_only:
        return check_watchlist(cfg)
    run(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
