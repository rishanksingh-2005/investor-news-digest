"""Command line: argument parsing, --doctor, and main()."""

from __future__ import annotations

import os
import re
import json
import argparse
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import configfile, delivery, health, pipeline, settings, sources, state, watchlists


def parse_args(argv: list[str] | None = None) -> settings.Config:
    """Defaults, then config.yaml, then command-line flags (each overrides the last)."""
    cfg = settings.Config()
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None)
    known, _ = pre.parse_known_args(argv)
    try:
        path = configfile.find_config(known.config)
        if path:
            configfile.load_config(path, cfg)
    except (configfile.ConfigError, watchlists.WatchlistError) as e:
        raise SystemExit(f"Config problem: {e}")

    p = argparse.ArgumentParser(description="Investor news digest for your own company watchlist")
    c = p.add_argument_group("settings file")
    c.add_argument("--config", default=None, metavar="FILE",
                   help="settings file (default: $NEWS_DIGEST_CONFIG, or config.yaml in the project folder)")
    c.add_argument("--init-config", default=None, metavar="FILE", nargs="?", const="config.yaml",
                   help="write a starter config.yaml listing every setting, then exit")
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
    p.add_argument("--no-trusted", action="store_false", dest="use_trusted_sites", default=cfg.use_trusted_sites)
    # --finnhub / --no-finnhub and --sec / --no-sec. Defaults come from Config, so
    # editing Config actually takes effect (previously --finnhub defaulted to off
    # and silently overrode Config.use_finnhub = True).
    p.add_argument("--finnhub", action=argparse.BooleanOptionalAction,
                   default=cfg.use_finnhub, dest="use_finnhub",
                   help="Finnhub news (North America; needs FINNHUB_API_KEY)")
    p.add_argument("--gdelt", action=argparse.BooleanOptionalAction,
                   default=cfg.use_gdelt, dest="use_gdelt",
                   help="GDELT global news index (free, no key; ~6 s per company)")
    p.add_argument("--sec", action=argparse.BooleanOptionalAction,
                   default=cfg.use_sec_edgar, dest="use_sec_edgar",
                   help="SEC EDGAR filings (US-listed names)")
    p.add_argument("--no-cache", action="store_false", dest="use_cache", default=cfg.use_cache,
                   help="ignore and don't write the SQLite cache (fetch everything fresh)")
    p.add_argument("--new-only", action="store_true", default=cfg.new_only,
                   help="leave out stories already published in an earlier digest")
    e = p.add_argument_group("email delivery")
    e.add_argument("--email-to", action="append", default=None, metavar="ADDRESS",
                   help="email the digest to this address (repeatable; replaces email_to from config.yaml)")
    e.add_argument("--no-email", action="store_true", help="don't email this run, even if email_to is set")
    p.add_argument("--browsers", type=int, default=cfg.browser_workers, metavar="N",
                   help=f"Chrome windows to run in parallel (default {cfg.browser_workers})")
    a = p.parse_args(argv)
    if a.watchlist:
        cfg.watchlist = os.path.abspath(a.watchlist)
    if a.sheet:
        cfg.sheet_name = a.sheet
    if a.column_map:
        try:
            cfg.column_map = watchlists.parse_column_map(a.column_map)
        except watchlists.WatchlistError as e:
            p.error(str(e))
    cfg.companies = a.company
    cfg.check_only = a.check
    cfg.init_watchlist = a.init_watchlist
    cfg.init_config = a.init_config
    cfg.doctor = a.doctor
    if a.email_to:
        cfg.email_to = [x.strip() for x in a.email_to if x.strip()]
    if a.no_email:
        cfg.email_to = []
    if a.tiers:
        cfg.tiers = tuple(int(x) for x in a.tiers.split(",") if x.strip())
    elif a.tier is not None:
        cfg.tiers = (a.tier,)
    cfg.days_back = a.days_back
    cfg.llm_provider = a.llm_provider
    cfg.headless = a.headless
    cfg.use_trusted_sites = a.use_trusted_sites
    cfg.use_finnhub = a.use_finnhub
    cfg.use_gdelt = a.use_gdelt
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


def doctor(cfg: settings.Config) -> int:
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
        companies, _ = watchlists.read_watchlist(cfg) if not cfg.companies else (watchlists.load_companies(cfg), None)
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
        k = sources.env_key(var)
        if not k:
            if needed:
                say("FAIL", label, f"not set: add {var}= to .env")
            continue
        code = _probe(url, hdr(k))
        say("OK" if code == 200 else "FAIL", label,
            "valid" if code == 200 else ("rejected (401): check the key" if code == 401 else f"test failed: {code}"))

    # Finnhub (optional)
    k = sources.env_key("FINNHUB_API_KEY")
    if not cfg.use_finnhub:
        say("--", "Finnhub", "turned off (--no-finnhub)")
    elif not k:
        say("--", "Finnhub", "no key (optional): free key at finnhub.io/register, then FINNHUB_API_KEY= in .env")
    else:
        code = _probe(f"{sources.FINNHUB_API}/quote?symbol=AAPL", {"X-Finnhub-Token": k})
        if code == 200:
            say("OK", "Finnhub", "key valid")
        elif code == 429:
            say("OK", "Finnhub", "key valid (rate-limited right now: free plan is 60 calls/min)")
        elif code == 401:
            say("FAIL", "Finnhub", "key rejected (401): copy it again from finnhub.io/dashboard, "
                                   "or remove it / use --no-finnhub")
        else:
            say("WARN", "Finnhub", f"test failed: {code}")

    # GDELT (optional, off by default)
    if cfg.use_gdelt:
        code = _probe(sources.GDELT_API + "?query=%22annual%20results%22&mode=artlist&format=json&maxrecords=1"
                      "&timespan=1d", {"User-Agent": "news-digest/1.0"}, timeout=30)
        say("OK" if code == 200 else "WARN", "GDELT",
            "reachable" if code == 200 else
            ("rate-limited right now (429): GDELT allows one request per 5 s per client" if code == 429
             else f"test failed: {code}"))

    # Email delivery (optional): connects and logs in, sends nothing
    status, detail = delivery.check(cfg)
    say(status, "Email", detail)

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


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    if cfg.init_config:
        try:
            configfile.write_starter_config(cfg.init_config)
        except configfile.ConfigError as e:
            print(e)
            return 1
        print(f"Wrote {cfg.init_config}: every setting with its default and an explanation.\n"
              f"Uncomment the lines you want to change. It's used automatically when it's called\n"
              f"config.yaml in the project folder; otherwise pass --config {cfg.init_config}.")
        return 0
    if cfg.config_path:
        print(f"Settings: {cfg.config_path}")
    if cfg.doctor:
        return doctor(cfg)
    if cfg.init_watchlist:
        try:
            watchlists.write_watchlist_template(cfg.init_watchlist)
        except watchlists.WatchlistError as e:
            print(e)
            return 1
        print(f"Wrote {cfg.init_watchlist}. Replace the sample rows with your companies, then run:\n"
              f'  {settings.command()} --watchlist "{cfg.init_watchlist}" --check')
        return 0
    if cfg.check_only:
        return watchlists.check_watchlist(cfg)
    try:
        pipeline.run(cfg)
    except KeyboardInterrupt:
        print("\nStopped. Everything read so far is cached; run again to continue.")
        return health.EXIT_FAILED
    except Exception:
        import traceback
        traceback.print_exc()
        return health.EXIT_FAILED
    return state._REPORT.exit_code()
