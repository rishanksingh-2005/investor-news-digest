"""Settings: the Config dataclass, the project home folder and .env loading."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from dotenv import load_dotenv

from . import watchlists


def _home() -> str:
    """Where user files live (watchlist, .env, cache, reports, logs): $NEWS_DIGEST_HOME,
    else the source checkout (the folder holding "news digest.py"), else the folder
    the command is run from (an installed `newsdigest`)."""
    env = os.environ.get("NEWS_DIGEST_HOME", "").strip()
    if env:
        return os.path.abspath(env)
    checkout = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(checkout, "news digest.py")):
        return checkout
    return os.getcwd()


HERE = _home()


def command() -> str:
    """How the user started us, for messages that suggest a next command."""
    prog = os.path.basename(sys.argv[0] or "")
    return 'python "news digest.py"' if prog == "news digest.py" else "newsdigest"
load_dotenv(os.path.join(HERE, ".env"))


# ============================================================================
# CONFIG
# ============================================================================
@dataclass
class Config:
    # Input / output
    # Watchlist: .xlsx / .csv / .tsv / .json / .txt (see load_companies). Default:
    # $WATCHLIST, else watchlist.* or "Company List.xlsx" in the project folder.
    watchlist: str = field(default_factory=lambda: watchlists.default_watchlist())
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
    use_gdelt: bool = False              # GDELT global news index (free, no key; --gdelt)
    gdelt_min_interval: float = 6.0      # seconds between GDELT requests (GDELT asks for >= 5)

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
    config_path: str | None = None       # the config.yaml that was applied, if any
    init_config: str | None = None       # --init-config FILE: write a starter config, then exit

    # Email delivery (optional): send the PDF + DOCX after each run. The mail
    # password goes in .env as SMTP_PASSWORD, never in this file.
    email_to: list = field(default_factory=list)   # recipients; empty = don't email
    email_from: str = ""                 # sender address (default: smtp_user)
    email_subject: str = "News digest {date}"      # placeholders: {date} {tiers} {companies} {items}
    email_attach_docx: bool = True       # attach the Word file as well as the PDF
    email_when_empty: bool = False       # also email a short note when nothing was published
    smtp_host: str = ""                  # e.g. smtp.gmail.com, smtp.office365.com
    smtp_port: int = 587                 # 587 with starttls, 465 with ssl
    smtp_security: str = "starttls"      # "starttls" | "ssl" | "none"
    smtp_user: str = ""                  # login name (usually your email address)

    # Accuracy / run health
    verify_figures: bool = True          # replace AI bullets whose numbers aren't in the article
    log_dir: str | None = None           # run logs; None = "<output_dir>/logs", "" = no log file

    # Dedup / merge thresholds (higher = stricter, fewer things treated as dup)
    title_similarity: float = 0.82       # stage 1: near-identical headline dedupe
    merge_text_ratio: float = 0.7        # stage 2: same-event if bullet text this similar (0.58 merged
                                         # "won a £1bn engine contract" with "won a £1bn reactor contract")
    merge_token_overlap: float = 0.42    # stage 2: OR shared-token (Jaccard) overlap
    merge_distinctive: int = 3           # stage 2: OR this many shared distinctive tokens
    merge_headline_overlap: float = 0.6  # stage 2: OR headlines share this much (Jaccard)

    # LLM
    llm_provider: str = "ollama"         # "ollama" | "openai" | "anthropic" | "bart"
    ollama_model: str = "llama3.2:3b"
    ollama_base_url: str = "http://localhost:11434/v1"
    openai_model: str = "gpt-4o-mini"
    anthropic_model: str = "claude-sonnet-4-6"
    llm_timeout: int = 90
    llm_retry_on_timeout: bool = True    # one retry with twice the timeout
    llm_max_failures: int = 3            # consecutive timeouts/connection errors before the
                                         # LLM is switched off for the rest of the run
    ollama_keep_alive: str = "30m"       # keep the model loaded between calls (warm-up)

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
