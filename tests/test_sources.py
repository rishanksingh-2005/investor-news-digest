"""Discovery sources (step 1), all against canned responses — no network."""
import json


# --- Google News RSS ---------------------------------------------------------

def test_google_news_rss_parsing(nd, cfg, fake_urlopen, fixture_text):
    fake_urlopen({"news.google.com/rss/search": [fixture_text("google_news.xml")]})
    items = nd._google_news_rss("Acme Widgets when:14d", cfg, "Google News")

    assert len(items) == 3                                # the empty-title item is skipped
    first = items[0]
    assert first.headline == "Acme Widgets raises full-year guidance after record orders"  # " - Reuters" stripped
    assert first.source == "Reuters"
    assert first.origin == "Google News"
    assert first.published.startswith("2026-11-16T08:30:00")
    assert "Acme Widgets raises full-year guidance" in first.snippet and "<a" not in first.snippet
    assert items[2].source == "" and items[2].published == ""


def test_google_news_rss_respects_item_cap(nd, cfg, fake_urlopen, fixture_text):
    cfg.max_items_per_source = 1
    fake_urlopen({"news.google.com": [fixture_text("google_news.xml")]})
    assert len(nd._google_news_rss("q", cfg, "Google News")) == 1


def test_google_news_rss_failure_returns_empty(nd, cfg, fake_urlopen, capsys):
    fake_urlopen({"news.google.com": [503]})
    assert nd._google_news_rss("q", cfg, "Google News") == []
    assert "feed failed" in capsys.readouterr().out


def test_discover_queries_use_company_name_and_site_filters(nd, cfg, fake_urlopen, fixture_text):
    calls = fake_urlopen({"news.google.com": [fixture_text("google_news.xml")]})
    co = nd.Company(name="Acme Widgets Plc", exchange="XLON")
    nd.discover_trusted(co, cfg)
    nd.discover_regulated(co, cfg)
    assert "site%3Areuters.com" in calls[0]
    assert "site%3Ainvestegate.co.uk" in calls[1]            # UK regulated source
    assert "%22Acme%20Widgets%20Plc%22" in calls[1]


def test_exchange_sources_fallbacks(nd):
    assert "investegate.co.uk" in nd.exchange_sources("xlon")
    assert nd.exchange_sources("XNYS") == nd.US_SOURCES
    assert nd.exchange_sources("") == nd.DEFAULT_REGULATED_SOURCES


def test_discover_honours_source_toggles(nd, cfg, monkeypatch, make_article):
    called = []
    for name in ("discover_google_news", "discover_trusted", "discover_regulated",
                 "discover_sec_edgar", "discover_finnhub"):
        monkeypatch.setattr(nd, name, lambda c, cfg, n=name: called.append(n) or [make_article()])
    cfg.use_trusted_sites = False
    cfg.use_finnhub = False
    out = nd.discover(nd.Company(name="Acme"), cfg)
    assert called == ["discover_google_news", "discover_regulated", "discover_sec_edgar"]
    assert len(out) == 3


# --- SEC EDGAR -------------------------------------------------------------------

def _sec_payload(fixture_text):
    return fixture_text("sec_search.json")


def test_sec_edgar_skips_non_us(nd, cfg, fake_urlopen):
    calls = fake_urlopen({})
    assert nd.discover_sec_edgar(nd.Company(name="Jet2 Plc", exchange="XLON"), cfg) == []
    assert calls == []


def test_sec_edgar_one_entry_per_filing_own_filer_only(nd, cfg, fake_urlopen, fixture_text):
    fake_urlopen({"efts.sec.gov": [_sec_payload(fixture_text)]})
    out = nd.discover_sec_edgar(nd.Company(name="Xpel, Inc.", exchange="XNAS"), cfg)

    assert [a.headline for a in out] == ["SEC 8-K filing — XPEL, Inc.", "SEC 10-Q filing — XPEL, Inc."]
    # the main 8-K document is preferred over its EX-99.1 exhibit
    assert out[0].url == ("https://www.sec.gov/Archives/edgar/data/1767258/"
                          "000176725826000096/xpel-20261104.htm")
    assert out[0].final_url == out[0].url
    assert all(a.kind == "filing" and a.source == "SEC EDGAR" for a in out)
    # the main 8-K document has no useful description: the items it reports are named instead
    assert out[0].snippet == "Results of operations (Item 2.02)"
    assert out[1].snippet == "QUARTERLY REPORT"
    assert out[1].published == "2026-11-06"
    # the mutual fund that merely holds the stock is filtered out
    assert not any("WASATCH" in a.headline for a in out)


def test_sec_edgar_retries_once_on_5xx(nd, cfg, fake_urlopen, fixture_text):
    calls = fake_urlopen({"efts.sec.gov": [500, _sec_payload(fixture_text)]})
    out = nd.discover_sec_edgar(nd.Company(name="Xpel, Inc.", exchange="XNAS"), cfg)
    assert len(calls) == 2 and len(out) == 2


def test_sec_edgar_gives_up_on_4xx(nd, cfg, fake_urlopen, capsys):
    calls = fake_urlopen({"efts.sec.gov": [403]})
    assert nd.discover_sec_edgar(nd.Company(name="Xpel, Inc.", exchange="XNAS"), cfg) == []
    assert len(calls) == 1
    assert "SEC EDGAR] failed" in capsys.readouterr().out


def test_sec_edgar_sends_configured_user_agent(nd, cfg, monkeypatch, fixture_text):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Runner test@example.org")
    seen = {}

    class R:
        def __init__(self, b):
            self.b = b

        def read(self):
            return self.b

    def urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return R(_sec_payload(fixture_text).encode())

    monkeypatch.setattr(nd.urllib.request, "urlopen", urlopen)
    nd.discover_sec_edgar(nd.Company(name="Xpel, Inc.", exchange="XNAS"), cfg)
    assert seen["ua"] == "Test Runner test@example.org"


# --- Finnhub -------------------------------------------------------------------------

def test_finnhub_without_key_warns_once_and_disables(nd, cfg, fake_urlopen, capsys):
    calls = fake_urlopen({})
    us = nd.Company(name="Xpel, Inc.", exchange="XNAS")
    assert nd.discover_finnhub(us, cfg) == []
    assert nd.discover_finnhub(us, cfg) == []
    assert calls == []
    assert capsys.readouterr().out.count("no FINNHUB_API_KEY") == 1


def test_finnhub_rejected_key_disables_for_rest_of_run(nd, cfg, fake_urlopen, monkeypatch, capsys):
    monkeypatch.setenv("FINNHUB_API_KEY", "bad")
    calls = fake_urlopen({"finnhub.io": [401]})
    nd.discover_finnhub(nd.Company(name="Xpel, Inc.", exchange="XNAS"), cfg)
    nd.discover_finnhub(nd.Company(name="Douglas Dynamics, Inc.", exchange="XNYS"), cfg)
    assert len(calls) == 1
    assert nd._FINNHUB_DISABLED is True
    assert "FINNHUB_API_KEY was rejected" in capsys.readouterr().out


def test_finnhub_skips_non_north_american(nd, cfg, fake_urlopen, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    calls = fake_urlopen({})
    assert nd.discover_finnhub(nd.Company(name="Jet2 Plc", exchange="XLON"), cfg) == []
    assert calls == []


def test_finnhub_resolves_symbol_and_parses_news(nd, cfg, fake_urlopen, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    search = {"result": [{"symbol": "XPEL.MX", "description": "XPEL INC"},
                         {"symbol": "XPEL", "description": "XPEL INC"}]}
    news = [{"headline": " XPEL beats estimates ", "url": "https://example.com/n", "source": "Benzinga",
             "datetime": 1763280000, "summary": "Revenue rose."}]
    calls = fake_urlopen({"finnhub.io/api/v1/search": [json.dumps(search)],
                          "finnhub.io/api/v1/company-news": [json.dumps(news)]})
    out = nd.discover_finnhub(nd.Company(name="Xpel, Inc.", exchange="XNAS"), cfg)
    assert "symbol=XPEL&" in calls[1]                        # dot-free (North American) symbol chosen
    assert out[0].headline == "XPEL beats estimates"
    assert out[0].origin == "Finnhub" and out[0].snippet == "Revenue rose."
    assert out[0].published.startswith("2025-11-16")


def test_sec_forms_without_a_description_are_named(nd, cfg, fake_urlopen):
    import json
    hit = {"_id": "0001-26-1:primary_doc.xml", "_source": {
        "adsh": "0001-26-1", "ciks": ["0000320193"], "display_names": ["Apple Inc.  (AAPL)  (CIK 0000320193)"],
        "file_type": "144", "form": "144", "root_forms": ["144"], "file_date": "2026-11-16"}}
    fake_urlopen({"efts.sec.gov": [json.dumps({"hits": {"hits": [hit]}}).encode()]})
    out = nd.discover_sec_edgar(nd.Company(name="Apple Inc.", exchange="XNAS"), cfg)
    assert out[0].snippet == "Insider's notice of a planned share sale"
    assert nd._fallback_bullet(out[0]) == "144 filed with the SEC: Insider's notice of a planned share sale."
