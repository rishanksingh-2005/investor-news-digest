"""Aliases / exclude / ticker matching, article page dates, figure-based duplicate
merging, LLM resilience, GDELT, and the labelled evaluation set."""
import importlib.util
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# --- aliases, exclude terms, tickers ----------------------------------------------------

def test_watchlist_reads_aliases_and_exclude(nd, cfg, tmp_path):
    p = tmp_path / "w.csv"
    p.write_text("Company,Aliases,Exclude,Tier\n"
                 "Rolls-Royce Holdings plc,Rolls-Royce; RR,Rolls-Royce Motor Cars | Phantom,1\n"
                 "\"Xpel, Inc.\",\"Xpel, Inc.\",,1\n", encoding="utf-8")
    cfg.watchlist = str(p)
    rr, xpel = nd.load_companies(cfg)
    assert rr.aliases == ["Rolls-Royce", "RR"] and rr.exclude == ["Rolls-Royce Motor Cars", "Phantom"]
    assert xpel.aliases == []          # an alias equal to the name is dropped


def test_json_watchlist_accepts_lists_for_aliases(nd, cfg, tmp_path):
    p = tmp_path / "w.json"
    p.write_text(json.dumps([{"company": "Jet2 Plc", "aliases": ["Jet2", "Jet2.com"], "tier": 1}]), encoding="utf-8")
    cfg.watchlist = str(p)
    assert nd.load_companies(cfg)[0].aliases == ["Jet2", "Jet2.com"]


def test_alias_makes_a_short_name_relevant(nd, make_article):
    co = nd.Company(name="Watches Of Switzerland Group Plc")
    a = make_article(headline="WOSG raises revenue outlook", snippet="Guidance raised after US sales rose.")
    assert not nd.strongly_relevant(a, co)
    co.aliases = ["WOSG"]
    assert nd.strongly_relevant(a, co)


def test_exclude_term_drops_a_story(nd, cfg, make_article):
    co = nd.Company(name="Rolls-Royce Holdings plc", aliases=["Rolls-Royce"], exclude=["Rolls-Royce Motor Cars"])
    cfg.days_back = cfg.recency_grace_days = 36500
    keep = make_article(headline="Rolls-Royce raises profit guidance", snippet="Profit outlook raised.")
    drop = make_article(headline="Rolls-Royce Motor Cars reports record orders", snippet="Orders rose.",
                        url="https://example.com/cars")
    assert nd.filter_articles([keep, drop], co, cfg) == [keep]
    assert nd._REPORT.counts["dropped: exclude term"] == 1


@pytest.mark.parametrize("text,hit", [
    ("PLOW beats Q3 earnings estimates", True), ("Shares of $PLOW rose", True),
    ("(NYSE: PLOW) reports results", True), ("Snow plow drivers wanted", False), ("PLOWING ahead", False),
])
def test_ticker_mentioned(nd, text, hit):
    assert nd.ticker_mentioned(text, nd.Company(name="Douglas Dynamics, Inc.", ticker="PLOW")) is hit


@pytest.mark.parametrize("ticker", ["RR.", "BA", "JET2", ""])
def test_short_or_numeric_tickers_are_not_used(nd, ticker):
    assert not nd.ticker_mentioned(f"{ticker} results", nd.Company(name="X", ticker=ticker))


def test_common_word_names_need_the_whole_phrase(nd, make_article):
    co = nd.Company(name="North West Electricity Plc")
    assert nd.strongly_relevant(make_article(headline="North West Electricity agrees takeover"), co)
    assert not nd.strongly_relevant(make_article(
        headline="Storms bring power cuts across the north west", snippet="Electricity was restored."), co)
    assert nd._name_phrase("The Watches Of Switzerland Group Plc") == "watches of switzerland"


def test_alias_queries_are_ored(nd):
    assert nd._name_query(nd.Company(name="Jet2 Plc")) == '"Jet2 Plc"'
    assert nd._name_query(nd.Company(name="Jet2 Plc"), quoted=False) == "Jet2 Plc"
    assert nd._name_query(nd.Company(name="Jet2 Plc", aliases=["Jet2"])) == '("Jet2 Plc" OR "Jet2")'


# --- the article's own publication date -------------------------------------------------

@pytest.mark.parametrize("html_head,expected", [
    ('<meta property="article:published_time" content="2026-11-10T08:00:00Z">', "2026-11-10T08:00:00+00:00"),
    ('<meta itemprop="datePublished" content="2026-11-09">', "2026-11-09T00:00:00+00:00"),
    ('<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-11-08T10:15:00.000+01:00"}'
     '</script>', "2026-11-08T10:15:00+01:00"),
    ('<time datetime="2026-11-07T12:00:00Z">7 Nov</time>', "2026-11-07T12:00:00+00:00"),
    ('<meta property="article:published_time" content="2999-01-01T00:00:00Z">', ""),   # future: ignored
    ("<title>No date here</title>", ""),
])
def test_page_published(nd, html_head, expected):
    assert nd.page_published(f"<html><head>{html_head}</head><body></body></html>") == expected


@pytest.mark.parametrize("value,expected", [
    ("20261117T093000Z", datetime(2026, 11, 17, 9, 30, tzinfo=timezone.utc)),
    ("2026-11-17T09:30:00.123456+00:00", datetime(2026, 11, 17, 9, 30, 0, 123456, tzinfo=timezone.utc)),
    ("2026-11-17T09:30:00.5Z", datetime(2026, 11, 17, 9, 30, tzinfo=timezone.utc)),
    ("Mon, 17 Nov 2026 09:30:00 GMT", datetime(2026, 11, 17, 9, 30, tzinfo=timezone.utc)),
])
def test_parse_dt_formats(nd, value, expected):
    assert nd._parse_dt(value).replace(microsecond=expected.microsecond) == expected


def test_page_date_overrides_a_misleading_feed_date(nd, cfg, make_article):
    now = datetime.now(timezone.utc)
    a = make_article(published=now.isoformat(), page_published=(now - timedelta(days=60)).isoformat())
    assert nd.apply_page_date(a) is True
    assert not nd.is_recent(a, cfg)          # the "new" story is really two months old
    b = make_article(published=now.isoformat(), page_published=(now - timedelta(hours=5)).isoformat())
    assert nd.apply_page_date(b) is False and b.published == now.isoformat()


def test_page_date_is_cached_with_the_article(nd, cache):
    cache.put_article("k", "https://x", "body", True, "2026-11-10T08:00:00+00:00")
    assert cache.get_article("k", 999, 999)["published"] == "2026-11-10T08:00:00+00:00"


def test_old_cache_files_get_the_new_column(nd, tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE articles (key TEXT PRIMARY KEY, final_url TEXT, body TEXT, "
               "authentic INTEGER, fetched_at REAL)")
    db.execute("INSERT INTO articles VALUES ('k', 'u', 'b', 1, 9e12)")
    db.commit()
    db.close()
    c = nd.Cache(str(path))
    assert c.get_article("k", 9e12, 9e12)["published"] == ""
    c.close()


# --- figure-based duplicate merging -----------------------------------------------------

def _pair(nd, a_head, a_bullet, b_head, b_bullet):
    return (nd.Article(headline=a_head, url="https://e.com/a", bullet=a_bullet),
            nd.Article(headline=b_head, url="https://e.com/b", bullet=b_bullet))


def test_same_figures_mean_same_event(nd, cfg):
    co = nd.Company(name="Watches Of Switzerland Group Plc")
    a, b = _pair(nd, "Watches of Switzerland raises outlook",
                 "Raised FY revenue growth guidance to 7-10% after US sales rose 22%.",
                 "WOSG lifts guidance", "US revenue jumps 22 percent; now sees growth of up to 10%.")
    assert nd._same_event(a, b, co, cfg)


def test_one_shared_figure_is_not_enough(nd, cfg):
    co = nd.Company(name="Rolls-Royce Holdings plc")
    a, b = _pair(nd, "Rolls-Royce wins Emirates engine deal", "Rolls-Royce won a £1bn engine contract from Emirates.",
                 "Rolls-Royce SMR picked", "Rolls-Royce won a £1bn reactor contract in the Czech Republic.")
    assert not nd._same_event(a, b, co, cfg)


def test_years_and_small_counts_are_not_key_figures(nd):
    assert nd._key_figures("In 2026 it opened 3 sites and grew 12% to £40m.") == {(0.12 * 100, True), (4e7, False)}


# --- LLM resilience ---------------------------------------------------------------------

def test_timeout_is_retried_once_with_a_longer_timeout(nd, cfg, monkeypatch):
    seen = []

    def call(prompt, c, max_tokens=160, schema=None):
        seen.append(c.llm_timeout)
        if len(seen) == 1:
            raise TimeoutError("slow")
        return "ok"

    monkeypatch.setattr(nd, "_llm_call", call)
    assert nd._llm_complete("p", cfg) == "ok"
    assert seen == [cfg.llm_timeout, cfg.llm_timeout * 2]


def test_circuit_breaker_stops_calling_a_dead_llm(nd, cfg, monkeypatch, capsys):
    calls = []

    def down(*a, **k):
        calls.append(1)
        raise ConnectionError("refused")

    monkeypatch.setattr(nd, "_llm_call", down)
    for _ in range(6):
        with pytest.raises(Exception):
            nd._llm_complete("p", cfg)
    assert len(calls) == cfg.llm_max_failures          # then it stops trying
    assert nd._LLM_STATE.tripped
    assert "switching it off" in capsys.readouterr().out
    assert any("LLM switched off" in w for w in nd._REPORT.all_warnings())


def test_success_resets_the_failure_count(nd, cfg, monkeypatch):
    answers = iter([ConnectionError(), ConnectionError(), "ok", ConnectionError(), ConnectionError(), "ok"])

    def flaky(*a, **k):
        x = next(answers)
        if isinstance(x, Exception):
            raise x
        return x

    monkeypatch.setattr(nd, "_llm_call", flaky)
    for _ in range(6):
        try:
            nd._llm_complete("p", cfg)
        except ConnectionError:
            pass
    assert not nd._LLM_STATE.tripped


def test_non_network_errors_do_not_trip_the_breaker(nd, cfg):
    for _ in range(5):
        with pytest.raises(RuntimeError):
            nd._llm_complete("p", cfg)        # "offline-test" provider: config error, not an outage
    assert not nd._LLM_STATE.tripped


def test_warm_up_only_for_ollama(monkeypatch):
    # conftest stubs warm_up_llm; load a fresh copy of the real function here
    spec = importlib.util.spec_from_file_location("nd_warm", ROOT / "news digest.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nd_warm"] = mod
    spec.loader.exec_module(mod)
    sent = []
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout=None: sent.append(req) or io.BytesIO(b"{}"))
    cfg = mod.Config()
    cfg.llm_provider = "anthropic"
    assert mod.warm_up_llm(cfg) is None
    cfg.llm_provider = "ollama"
    mod.warm_up_llm(cfg).join(5)
    assert sent[0].full_url == "http://localhost:11434/api/generate"
    assert json.loads(sent[0].data) == {"model": cfg.ollama_model, "keep_alive": cfg.ollama_keep_alive}


# --- GDELT ------------------------------------------------------------------------------

GDELT_JSON = {"articles": [
    {"url": "https://www.reuters.com/jet2-profit", "title": "Jet2 profit rises", "seendate": "20261117T093000Z",
     "domain": "reuters.com", "language": "English"},
    {"url": "https://www.reuters.com/jet2-profit", "title": "Jet2 profit rises (dup)", "seendate": "20261117T093000Z",
     "domain": "reuters.com"},
    {"url": "https://www.bbc.co.uk/jet2", "title": "Jet2 adds routes", "seendate": "20261116T120000Z",
     "domain": "bbc.co.uk"},
]}


def test_gdelt_parses_articles(nd, cfg, fake_urlopen):
    calls = fake_urlopen({"api.gdeltproject.org": [json.dumps(GDELT_JSON)]})
    out = nd.discover_gdelt(nd.Company(name="Jet2 Plc", aliases=["Jet2"]), cfg)
    assert [a.headline for a in out] == ["Jet2 profit rises", "Jet2 adds routes"]      # duplicate URL dropped
    assert out[0].origin == "GDELT" and out[0].source == "reuters.com"
    assert out[0].published == "2026-11-17T09:30:00+00:00"
    assert not nd._needs_browser(out[0])                                               # read over plain HTTP
    assert "%22Jet2+Plc%22+OR+%22Jet2%22" in calls[0] and "timespan=14d" in calls[0]


def test_gdelt_rate_limit_text_and_429_switch_it_off(nd, cfg, fake_urlopen, capsys):
    calls = fake_urlopen({"api.gdeltproject.org": [
        "Please limit requests to one every 5 seconds or contact us.", 429, json.dumps(GDELT_JSON)]})
    co = nd.Company(name="Jet2 Plc")
    assert nd.discover_gdelt(co, cfg) == [] and nd.discover_gdelt(co, cfg) == []
    assert nd.discover_gdelt(co, cfg) == []                  # switched off: no third request
    assert len(calls) == 2
    assert "rate-limited twice" in capsys.readouterr().out
    assert nd._REPORT.sources["GDELT"]["error"] == 2


def test_gdelt_requests_are_spaced_out(nd, cfg, fake_urlopen, monkeypatch):
    waits = []
    monkeypatch.setattr(nd.time, "sleep", waits.append)
    fake_urlopen({"api.gdeltproject.org": [json.dumps({"articles": []})]})
    nd.discover_gdelt(nd.Company(name="A Plc"), cfg)
    nd.discover_gdelt(nd.Company(name="B Plc"), cfg)
    assert len(waits) == 1 and 5 < waits[0] <= cfg.gdelt_min_interval


def test_gdelt_is_off_by_default_and_has_a_flag(nd, monkeypatch):
    assert nd.Config().use_gdelt is False
    monkeypatch.setattr("sys.argv", ["news digest.py", "--gdelt"])
    assert nd.parse_args().use_gdelt is True


# --- labelled evaluation set ------------------------------------------------------------

def test_rules_meet_the_evaluation_thresholds(nd):
    """The hand-labelled cases in eval/golden.json: a rule change that makes the
    digest's judgement worse fails here (and in CI)."""
    spec = importlib.util.spec_from_file_location("evaluate", ROOT / "eval" / "evaluate.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    golden = json.loads((ROOT / "eval" / "golden.json").read_text(encoding="utf-8"))
    for name, (scores, rows, _) in ev.evaluate(nd, golden).items():
        misses = [r[2] for r in rows if r[0] != r[1]]
        assert scores["accuracy"] >= golden["thresholds"][name], f"{name}: {scores} misses={misses}"
