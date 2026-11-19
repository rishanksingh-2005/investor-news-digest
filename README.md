# Tier Company News Digest

<!-- After pushing to GitHub, add the CI badge (replace OWNER/REPO):
[![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/ci.yml)
-->

An investor-focused news digest for **your own watchlist** of listed companies.
Point it at your company list (Excel, CSV, JSON or a plain text file, with
whatever column names you already use). For every company it finds the latest stories from several sources,
reads each article, throws away the noise, merges duplicate coverage, summarises
each story into one bullet with an LLM, and publishes a clean **PDF and Word
(.docx)** report grouped by company, with every point linking back to its source.

Companies can be split into priority **tiers**; by default Tiers 1, 2 and 3 go
into a single combined digest.

```bash
python "news digest.py" --watchlist my_companies.xlsx --check   # see how your file is read
python "news digest.py" --watchlist my_companies.xlsx           # build the digest
```

---

## How it works

| Step | What happens | Tech |
|---|---|---|
| 0. Load | Read your watchlist (any of .xlsx/.csv/.tsv/.json/.txt; columns auto-detected) | `openpyxl`, `csv`, `json` |
| 1. Discover | Find candidate stories per company | Google News RSS, SEC EDGAR, Finnhub |
| 2. Extract | Resolve each story to the publisher and read the article body: cache first, then plain HTTP, then Chrome | `urllib` / Selenium (Chrome) + BeautifulSoup, `trafilatura` as fallback |
| 3. Filter | Drop blocklisted, off-topic, stale, promotional, price-chatter and admin-filler stories | regex rules |
| 4. Dedupe | Collapse the same story reported by many outlets | `difflib` |
| 5. Summarise | One investor bullet per story, then group a company's points by topic | Ollama (default), OpenAI, Anthropic or local BART |
| 6. Publish | Build the PDF and DOCX with clickable source links | `reportlab`, `python-docx` |

Only step 5 uses AI. Everything else is plain code and public feeds.

**Discovery sources**

- **Google News RSS** for every company: a plain search, plus a `site:` search
  over trusted outlets (Reuters, FT, Bloomberg, WSJ, CNBC, the PR wires, …).
- **Official / regulated feeds per exchange**, also via Google News with `site:`
  filters: RNS/Investegate (UK), SEDAR+/Newswire (Canada), Actusnews (France),
  MFN/Cision (Sweden), EQS (Germany/Switzerland), MAYA (Israel), ASX (Australia),
  US PR wires, and more. See `EXCHANGE_SOURCES` in the script.
- **SEC EDGAR**: the company's own recent filings (8-K, 10-Q, …), for US listings only.
- **Finnhub**: company news for North American listings. Optional, and needs a
  free API key; it's skipped automatically without one.

**Quality gates.** A story only reaches the report if it comes from a
non-blocklisted source, names the company in its headline or lead, carries a
material signal (earnings, M&A, guidance, contracts, leadership, regulatory…), is
not share-price/valuation chatter, consumer promo or routine RNS admin, and has a
real readable article body (not a paywall or consent stub). Companies with nothing
material are left out of the report.

**Page budget.** At most `max_articles_per_company` points per company and
`max_total_items` across the whole digest (about 8 pages), ranked by importance.
Every Tier-1 company keeps its best item.

If the chosen LLM is unreachable, the run never hangs: it falls back to an
offline summary built from the article's own lead sentences.

**Accuracy safeguards**

- **Number check.** Every figure in an AI-written bullet (amounts, percentages,
  counts) must appear in the source article, allowing for rounding and units
  (`£40.3m` supports `£40m`, `£1,234 million` supports `£1.2bn`, `22 percent`
  supports `22%`). A bullet with a figure that isn't in the article is replaced by
  the article's own sentences, and is never cached. Merged topic summaries get the
  same check against their member stories. In live testing this caught a bullet
  claiming "revenue climbed 7.9%" when the article contains no such figure. Turn
  it off with `verify_figures = False`.
- **Whole-word matching.** Keywords and company names match from the start of a
  word, so "order" no longer matches "border", "fine" no longer matches "finance",
  and "stake" no longer matches "stakeholder". Hyphenated keywords such as
  "half-year" match either spelling.
- **Constrained JSON.** Topic grouping asks the model for a fixed JSON format
  (Ollama and OpenAI support this). Small slips are repaired instead of the
  whole answer being thrown away: a forgotten story becomes its own point, and a
  repeated one is dropped.

**Run report and exit codes**

Each run ends with a health report: calls per source, stories read, how each
bullet was made, and an overall status. The report is also written to a log
file in `logs/`:

```
Run report
  Finnhub                            4 ok
  Google News                        7 ok
  SEC EDGAR                          4 ok
  bullets: AI (cached)               6
  bullets: replaced by number check  1
  Status: OK
```

The exit code tells a scheduler how the run went:

| Code | Meaning |
|---|---|
| `0` | OK. Published, or nothing material this window. |
| `1` | Failed. Watchlist problem, crash, or no stories from any source (usually a Google consent page, rate limit or no network). |
| `2` | Degraded. Published, but a source failed more often than it worked, Chrome didn't start, or the LLM was unreachable for most bullets. |

### Speed and caching

The run happens in phases, and each phase works through all companies in
parallel on a small thread pool. Discovery searches several companies at once.
Extraction uses the cheapest route that works:

1. **Cache.** A story already resolved and read in an earlier run is reused straight
   from `digest_cache.sqlite`.
2. **Plain HTTP.** Non-Google links (Finnhub, cached publisher URLs) are fetched
   directly, with no browser, 8 at a time.
3. **Chrome.** Only Google News redirect links, and pages HTTP couldn't read, open
   in Chrome, in several windows at once (`--browsers N`).

LLM summaries and topic groupings are cached per input and model, so a rerun
never pays for the same LLM call twice. If the LLM was unreachable, nothing is
cached and the next run tries again. Search results are reused for an hour.
Unreadable or paywalled pages are retried after a day.

Measured on Tier 1 (7 companies, 29 articles, local `llama3.2:3b`, headless):

| Run | Time |
|---|---|
| Previous version (sequential, no cache) | 664 s |
| This version, empty cache | 265 s (2.5× faster) |
| Same window again, cache filled | 10 s |

So reruns are near-instant, and a crashed or interrupted run can pick up where it
stopped: just run it again. `--new-only` uses the same cache to leave out stories that already
appeared in an earlier digest, which is handy for a daily run. `--no-cache`
ignores the cache and fetches everything fresh.

---

## Requirements

- **Python 3.10+**
- **Google Chrome.** Selenium downloads the matching driver automatically.
- **A summariser**, one of:
  - **Ollama** (default, free, local): install from [ollama.com](https://ollama.com),
    then `ollama pull llama3.2:3b`.
  - **OpenAI** or **Anthropic** API key (hosted).
  - **BART** (offline, no instructions/grouping): `facebook/bart-large-cnn`.

## Setup

**Windows (PowerShell)**

```powershell
cd "path\to\project"
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env        # then fill in only the keys you need
```

**macOS / Linux**

```bash
cd "path/to/project"
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Optional local BART summariser (~2 GB of extra packages):

```bash
pip install -r requirements-bart.txt
```

It uses the model in `NLP/` if that folder is present, and otherwise downloads
`facebook/bart-large-cnn` from Hugging Face on first use.

> A virtual environment is tied to the exact Python that created it. If you
> upgrade or reinstall Python, delete `.venv` and create it again. Typical symptoms
> of a stale venv: `No module named 'lxml.etree'`, `cannot import name '_regex'`.

### Your watchlist

Use the company list you already have. Supported files:

| Format | What it should look like |
|---|---|
| `.xlsx` / `.xlsm` | Any sheet with a header row. The first sheet that has a company-name column is used (or pick one with `--sheet`). The header doesn't have to be on row 1. |
| `.csv` / `.tsv` | Comma, semicolon, tab or pipe separated, detected automatically. A single column with no header works as a plain list of names. |
| `.json` | `[{"company": "...", "tier": 1}, ...]`, `{"companies": [...]}`, or just `["Name", "Name"]`. |
| `.txt` | One company per line; `#` starts a comment. |

Only a **company name** column is required. Everything else is optional, and
columns are recognised by common header names (case, spaces and punctuation
don't matter):

| Field | Recognised headers (examples) | Used for |
|---|---|---|
| name | Name, Company, Company Name, Issuer, Security Name, Entity | The search term. |
| tier | Tier, Priority, Group, Bucket | Selecting tiers (`1`, `1.0`, `Tier 1`, `T1`, `P1` all work). No tier column: every company is Tier 1. |
| exchange | Exchange, MIC, Market, Listing, Venue | Picks the official news sources. MIC codes (`XLON`) or common names (`NASDAQ`, `NYSE`, `LSE`, `AIM`, `TSX`, `ASX`, `Euronext Paris`, …). |
| country | Country, Domicile, Region | Used when the exchange is missing (`US` turns on SEC filings, `US`/`CA` Finnhub). |
| ticker | Ticker, Symbol, EPIC | Kept with the company. |
| currency | Currency, CCY | Shown in the report. |

Other columns are ignored. Spreadsheet error values such as `#VALUE!` and `#N/A`
count as empty, and duplicate company names are dropped.

If your headers are unusual, map them yourself:

```bash
python "news digest.py" -w export.csv --map "name=Long Description,tier=Bucket" --check
```

**Getting started:**

```bash
python "news digest.py" --init-watchlist watchlist.xlsx   # starter file (.xlsx, .csv or .json)
python "news digest.py" -w watchlist.xlsx --check         # columns found, companies per tier, skipped rows
python "news digest.py" -c "Rolls-Royce Holdings" -c "BAE Systems"   # no file at all
```

Where the watchlist comes from, in order: `--watchlist FILE`; the `WATCHLIST`
environment variable (handy in `.env`); `watchlist.xlsx`/`.csv`/`.json`/`.txt` or
`Company List.xlsx` next to the script; and finally the bundled
`examples/watchlist.example.csv`, so a fresh clone runs out of the box.

Real watchlists are client data, so `.gitignore` keeps `watchlist.*`,
`watchlists/` and `Company List.xlsx` out of the repository.

### Environment variables (`.env`)

Each user brings their own keys. Copy `.env.example` to `.env` and fill in only
what you need. `.env` is git-ignored, and keys are never printed or put in URLs.

| Variable | Needed for | How to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | `--provider anthropic` | [console.anthropic.com](https://console.anthropic.com) |
| `OPENAI_API_KEY` | `--provider openai` | [platform.openai.com](https://platform.openai.com/api-keys) |
| `FINNHUB_API_KEY` | Finnhub news source (optional) | Free at [finnhub.io/register](https://finnhub.io/register); the key is on your dashboard. |
| `SEC_USER_AGENT` | SEC EDGAR filings (US listings) | No sign-up. The SEC requires your name and email: `SEC_USER_AGENT=Jane Smith jane@example.com` |
| `WATCHLIST` | Default watchlist file, if you don't pass `--watchlist` | |

None are needed for the default Ollama run: without a Finnhub key, Finnhub is
skipped; without an SEC contact, EDGAR is still tried with a generic one, which
the SEC may throttle.

Notes on Finnhub:
- The free plan covers **US listings only**. Other markets return HTTP 403, so
  just that company is skipped and Finnhub carries on for the rest.
- The ticker comes from your watchlist's ticker column if it has one, otherwise
  Finnhub's symbol search.
- The free plan allows 60 calls a minute; a rate-limit reply is retried once.

**Check everything in one go:**

```bash
python "news digest.py" --doctor
```

```
  [OK  ] Python               3.14.6
  [OK  ] Chrome               found
  [OK  ] Watchlist            112 companies in watchlist.xlsx
  [OK  ] Ollama               running, llama3.2:3b available
  [OK  ] Finnhub              key valid
  [OK  ] SEC EDGAR            contact set, EDGAR reachable
```

It makes one cheap test call per key that is set (Finnhub, OpenAI, Anthropic,
SEC) and tells you exactly what to fix.

---

## Run

Make sure Ollama is running (`ollama list` should show `llama3.2:3b`), activate
the venv, then:

```bash
python "news digest.py"
```

A Chrome window opens so you can watch, or dismiss a consent popup once. When the
run finishes, these files appear in the project folder:

```
news digest YYYY-MM-DD.pdf
news digest YYYY-MM-DD.docx
```

A large watchlist (100+ companies) takes a while on the first run. Use
`--tiers 1,2` for a faster pass; later runs reuse the cache.

### Command-line flags

| Flag | Effect |
|---|---|
| `--watchlist FILE`, `-w` | Your company list (.xlsx, .csv, .tsv, .json, .txt). |
| `--sheet NAME` | Excel sheet to read (default: first sheet with a company-name column). |
| `--map "field=Header,..."` | Map your own column headers (fields: name, tier, exchange, country, ticker, currency). |
| `--company NAME`, `-c` | Run for this company without a file (repeatable). |
| `--check` | Show how the watchlist is read (columns, tiers, skipped rows) and exit. |
| `--init-watchlist FILE` | Write a starter watchlist and exit. |
| `--doctor` | Check the setup and test your API keys, then exit. |
| `--tiers 1,2,3` | Comma-separated tiers to include (default: all three). |
| `--tier 2` | A single tier only. |
| `--days 14` | Discovery window in days (default 14). |
| `--provider ollama\|openai\|anthropic\|bart` | Choose the summariser. |
| `--model NAME` | Override the model for that provider (for `bart`: a model folder). |
| `--headless` | Run Chrome invisibly. |
| `--no-trusted` | Skip the trusted-outlets search. |
| `--no-sec` | Skip SEC EDGAR filings. |
| `--no-finnhub` | Skip Finnhub. |
| `--new-only` | Leave out stories already published in an earlier digest. |
| `--no-cache` | Don't read or write the cache; fetch everything fresh. |
| `--browsers N` | Chrome windows to run in parallel (default 3). |

Examples:

```bash
python "news digest.py" --tiers 1,2 --days 7 --headless
python "news digest.py" --tier 1 --provider anthropic
```

### Configuration

Everything else lives in the `Config` dataclass at the top of `news digest.py`:

| Setting | Default | What it does |
|---|---|---|
| `watchlist` | see above | Default watchlist file. |
| `default_tier` | 1 | Tier given to every company when the file has no tier column. |
| `days_back` | 14 | Discovery window (days). |
| `recency_grace_days` | 21 | Stories older than this are dropped. |
| `max_items_per_source` | 8 | Candidates pulled from each source. |
| `max_articles_per_company` | 2 | Points kept per company. |
| `max_total_items` | 40 | Global cap across the digest (~8 pages). |
| `min_importance` | 1 | Minimum material-signal score to keep a story. |
| `use_*` | on | Toggle each discovery source. |
| `llm_provider` | `ollama` | Default summariser. |
| `ollama_model` / `openai_model` / `anthropic_model` | `llama3.2:3b` / `gpt-4o-mini` / `claude-sonnet-4-6` | Model per provider. |
| `title_similarity`, `merge_*` | — | Dedupe / merge strictness. |
| `polite_delay` | 1.0 s | Pause between requests, so sites aren't hammered. |
| `discovery_workers` | 3 | Companies searched at once (Google rate-limits much above this). |
| `http_workers` / `browser_workers` / `llm_workers` | 8 / 3 / 2 | Parallel HTTP fetches / Chrome windows / LLM calls. |
| `http_fast_path` | on | Try plain HTTP before Chrome for non-Google links. |
| `use_cache`, `cache_path` | on, `digest_cache.sqlite` | The SQLite run cache. |
| `feed_cache_minutes` / `article_cache_days` / `failed_article_retry_hours` | 60 / 30 / 24 | How long each kind of cached item is reused. |
| `verify_figures` | on | Number check on AI bullets and summaries. |
| `log_dir` | `<output_dir>/logs` | Where run logs go (`""` turns logging off). |

Source blocklist, relevance keywords and noise patterns are module-level lists
just below `Config` (`SOURCE_BLOCKLIST`, `RELEVANCE_KEYWORDS`, …).

---

## Scheduling (optional)

**Windows (Task Scheduler)**, daily at 07:00. Use full paths, and keep Ollama set
to start at login:

```powershell
schtasks /Create /SC DAILY /ST 07:00 /TN "News Digest" /TR "\"C:\path\to\project\.venv\Scripts\python.exe\" \"C:\path\to\project\news digest.py\" --headless"
```

**macOS / Linux (cron)**, daily at 07:00:

```
0 7 * * * cd "/path/to/project" && .venv/bin/python "news digest.py" --headless >> schedule.log 2>&1
```

The machine must be awake at that time. A cloud scheduler can't reach a local
Ollama, so there use `--provider openai` or `--provider anthropic`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| 0 articles for everyone | Google showed a consent wall. Run once without `--headless`, accept it, then rerun. |
| Bullets look like raw article sentences, plus `LLM bullet failed — using fallback` | The LLM wasn't reachable. Start Ollama (`ollama list`) or check your API key. |
| `[Finnhub] FINNHUB_API_KEY was rejected (HTTP 401)` | The key is wrong or was regenerated. Copy it again from your Finnhub dashboard into `.env`, then run `--doctor`. Or leave it empty / use `--no-finnhub`. |
| `[Finnhub] ... isn't covered by this key's plan (HTTP 403)` | Normal on the free plan for non-US listings; that company is skipped. |
| `[SEC EDGAR] failed: HTTP 403`, or the `SEC_USER_AGENT` warning | Set `SEC_USER_AGENT=Your Name you@domain.com` in `.env`. |
| A key in `.env` seems ignored | A variable already set in your system environment takes priority over `.env`. Remove the old one, or open a new terminal. |
| ChromeDriver errors | Update Chrome; Selenium 4 matches the driver automatically. |
| `can't open file … news digest.py` | Wrong folder, or the name isn't quoted. It has a space: `"news digest.py"`. |
| A company you expected is missing | It was filtered (price chatter / promo / admin filler), or nothing readable passed in the window. Try `--days 21`. |
| `pip` not recognised (Windows) | Use `python -m pip …` with the venv active. |
| Results look stale, or a fixed page still shows as unreadable | Run once with `--no-cache`, or delete `digest_cache.sqlite`. |
| `feed failed: HTTP Error 429` | Google is rate-limiting. Lower `discovery_workers` to 1–2. |
| PC struggles during a run | Lower `--browsers` (each Chrome window uses ~200–400 MB). |
| `couldn't find a company-name column` | Rename the column to `Company`/`Name`, or use `--map name="Your Header"`. Check with `--check`. |
| `No companies found for the selected tiers` | Your tier values don't match `--tiers`. `--check` lists the tiers it found. |
| `.xls` not supported | Open it in Excel and save as `.xlsx` or `.csv`. |
| `number check: … not in the article` | The AI wrote a figure the article doesn't contain, so the article's own sentences were used instead. Working as intended. |
| A scheduled run "fails" with exit code 2 | It published, but something was degraded. The `!` lines at the end of the log say what. |

---

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest          # ~235 tests, a few seconds
ruff check .              # lint
```

The test suite runs **fully offline**: no Chrome, no LLM, no API keys and no
network. Feed and API calls are replaced with canned responses from
`tests/fixtures/`, and any unexpected network call fails the test. It covers
every pipeline stage: watchlist loading in every format (header detection,
column mapping, tier parsing, exchange names), Google News / SEC EDGAR / Finnhub
parsing (including retries and bad-key handling), article extraction, every
filter, dedupe and topic merging, the LLM fallbacks, the cache (expiry, reuse,
`--new-only`), the cache → HTTP → Chrome routing, the page budget, the PDF and DOCX
writers, the CLI, and a complete offline `run()` end to end.

**CI.** GitHub Actions (`.github/workflows/ci.yml`) runs `ruff` and the tests on
every push and pull request: Ubuntu on Python 3.10, 3.13 and 3.14, plus Windows.

## Project layout

```
news digest.py          the whole pipeline (single script)
examples/               sample watchlist (watchlist.example.csv)
watchlist.xlsx / .csv   your own watchlist (not in git)
requirements.txt        core dependencies
requirements-bart.txt   optional local BART summariser
requirements-dev.txt    test + lint tools
pyproject.toml          pytest and ruff settings
tests/                  offline test suite (+ fixtures/)
.github/workflows/      CI
.env.example            template for API keys (copy to .env)
NLP/                    optional local BART model (not in git)
digest_cache.sqlite     run cache, created on first run (not in git)
logs/                   one log per run, with the run report (not in git)
LICENSE                 MIT
```

## Limitations

- Google News RSS is unofficial and can rate-limit or change format.
- Paywalled outlets (FT, WSJ, Bloomberg) often fail the readable-body check, so
  the same story is usually taken from a free outlet instead.
- Relevance and noise filters are keyword rules: they're fast and transparent,
  but they aren't perfect.
- The number check verifies figures, not meaning. A bullet can quote only real
  numbers and still misstate what they refer to.
- With the default 3B local model, topic grouping is always valid JSON but not
  always good judgement: it can put unrelated points (results and a CFO change,
  say) under one heading. A larger model (`--model llama3.1:8b`, or
  `--provider openai`/`anthropic`) groups noticeably better.

*Summaries are AI-assisted. Verify against the linked source before making any
investment decision. Not investment advice.*

## License

[MIT](LICENSE)
