"""Investor news digest for your own company watchlist.

    newsdigest --watchlist companies.xlsx          # build the digest
    newsdigest --doctor                            # check setup and API keys

The pipeline, one module per stage:

    watchlists   load the company list (any format, auto-detected columns)
    sources      1. discover stories (Google News, regulated feeds, SEC, Finnhub, GDELT)
    extraction   2. read each article (cache -> HTTP -> Chrome)
    filtering    3. keep material, relevant, recent news
    grouping     4. dedupe and group by topic
    summarising  5. one checked bullet per story
    publishing   6. PDF + DOCX
    pipeline     run() ties the phases together; cli is the command line
"""

__version__ = "1.2.0.dev0"

# Load every module once, in dependency order, so `newsdigest.<module>` works.
from . import state  # noqa: F401  (must be first: holds the run-wide state)
from . import (  # noqa: F401,E402
    patterns, models, settings, watchlists, configfile, browser, storage, health, sources, extraction,
    filtering, grouping, summarising, publishing, delivery, scheduling, pipeline, cli,
)
from .settings import Config  # noqa: E402
from .pipeline import run  # noqa: E402
from .cli import main  # noqa: E402

__all__ = ["Config", "run", "main", "__version__"]
