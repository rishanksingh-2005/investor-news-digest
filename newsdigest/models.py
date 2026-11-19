"""Data models: Article and Company."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import patterns


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
    page_published: str = ""  # publication date the article page itself states (ISO), if any
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
    aliases: list = field(default_factory=list)   # other names it's reported under ("Jet2")
    exclude: list = field(default_factory=list)   # terms that mean a story is NOT about it
    articles: list = field(default_factory=list)
    note: str = ""

    @property
    def is_us(self) -> bool:
        return self.exchange.upper() in patterns.US_EXCHANGES or (not self.exchange and self.country == "US")

    @property
    def is_north_am(self) -> bool:
        return (self.exchange.upper() in patterns.NORTH_AM_EXCHANGES
                or (not self.exchange and self.country in {"US", "CA"}))

    @property
    def is_uk(self) -> bool:
        return self.exchange.upper() == "XLON" or (not self.exchange and self.country == "UK")
