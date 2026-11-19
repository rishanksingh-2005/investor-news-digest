"""Accuracy and run-health features: number check, whole-word keywords,
constrained JSON for grouping, and the run report / exit codes / log file."""
import json
import os
import types

import pytest


# --- number check ---------------------------------------------------------------------

SOURCE = ("Acme Widgets Plc said on Monday it had won a £40.3m defence contract, lifting its order "
          "book by 18% to £1,234 million. Revenue for 2026 rose 7.5% to $512,000. Three new sites open.")


@pytest.mark.parametrize("bullet", [
    "Acme won a £40.3m defence contract.",          # exact
    "Acme won a £40m contract.",                    # rounded at the bullet's precision
    "The order book rose 18% to £1.2bn.",           # same value, different unit
    "Order book is now £1,234 million.",
    "Revenue rose 7.5% in 2026 to $512,000.",
    "Acme is opening 3 new sites.",                 # small integers are spelled out in sources
    "No figures here at all.",
])
def test_supported_figures_pass(nd, bullet):
    assert nd.unsupported_figures(bullet, SOURCE) == []


@pytest.mark.parametrize("bullet,bad", [
    ("Acme won a £400m defence contract.", ["400"]),     # garbled amount
    ("The order book rose 81%.", ["81%"]),               # swapped digits
    ("Revenue rose 7.5% to $612,000.", ["612000"]),
    ("Acme won 40 contracts and 18 orders.", ["40"]),    # 18 exists only as a percentage... and 40 only as £40.3m
    ("Profit rose 12%.", ["12%"]),
])
def test_invented_figures_are_caught(nd, bullet, bad):
    found = nd.unsupported_figures(bullet, SOURCE)
    assert found[: len(bad)] == bad


def test_percentage_needs_a_percentage_in_the_source(nd):
    assert nd.unsupported_figures("Sales up 18%.", "The company has 18 stores.") == ["18%"]


@pytest.mark.parametrize("source", [
    "Revenue grew 22 percent in the US", "Revenue grew 22 per cent in the US",
    "Revenue grew 22pct in the US", "Revenue grew 22% in the US",
])
def test_percent_written_out_supports_a_percent_sign(nd, source):
    """Seen live: a correct '22%' bullet was flagged because the article said '22 percent'."""
    assert nd.unsupported_figures("US sales grew 22%.", source) == []


def test_bullet_with_invented_figure_falls_back_and_is_not_cached(nd, cfg, cache, make_article, monkeypatch):
    nd._CACHE = cache
    body = ("Acme Widgets Plc said on Monday it had won a £40m defence contract, its largest ever, "
            "lifting its order book by 18%. ") * 4
    a = make_article(body=body, authentic=True)
    co = nd.Company(name="Acme Widgets Plc")
    monkeypatch.setattr(nd, "_llm_complete", lambda *x, **k: "Acme won a £400m defence contract.")
    out = nd.summarise_bullet(a, co, cfg)
    assert "£400m" not in out and "£40m" in out
    assert cache.get_bullet(nd._orig_key(a), cfg.llm_provider, nd._model_name(cfg)) is None
    assert nd._REPORT.counts["bullets: replaced by number check"] == 1


def test_number_check_can_be_turned_off(nd, cfg, make_article, monkeypatch):
    cfg.verify_figures = False
    a = make_article(body="Acme Widgets Plc won a £40m defence contract today. " * 5, authentic=True)
    monkeypatch.setattr(nd, "_llm_complete", lambda *x, **k: "Acme won a £400m deal.")
    assert nd.summarise_bullet(a, nd.Company(name="Acme Widgets Plc"), cfg) == "Acme won a £400m deal."


# --- whole-word keyword matching --------------------------------------------------------

@pytest.mark.parametrize("text,relevant", [
    ("Acme Widgets wins new order from the army", True),
    ("Acme Widgets staff stuck at the border", False),            # "order" inside "border"
    ("Acme Widgets hit with regulatory fine", True),
    ("Acme Widgets finance director on holiday", False),          # "fine" inside "finance"
    ("Acme Widgets stakeholder day this week", False),            # "stake" inside "stakeholder"
    ("Acme Widgets takes 20% stake in rival", True),
    ("Acme Widgets publishes half-year figures", True),           # hyphenated keyword now matches
    ("Acme Widgets full year trading", True),
    ("Acme Widgets dealer network tour", False),                  # "deal" inside "dealer"
])
def test_relevance_keywords_match_whole_words(nd, make_article, text, relevant):
    a = make_article(headline=text, snippet="", body="")
    assert nd.is_relevant(a, nd.Company(name="Acme Widgets Plc")) is relevant


def test_company_token_must_start_a_word(nd, make_article):
    co = nd.Company(name="Gamma Communications Plc")
    assert nd.strongly_relevant(make_article(headline="Gamma Communications buys rival"), co)
    assert nd.strongly_relevant(make_article(headline="Gamma's results beat forecasts"), co)
    assert not nd.strongly_relevant(make_article(headline="Omegagamma results beat forecasts"), co)


@pytest.mark.parametrize("text,more_than_zero", [
    ("Acme won an order worth millions", True),
    ("Acme staff stuck at the border", False),
    ("Acme was fined by the regulator", True),
    ("Acme's finance team grows", False),
])
def test_importance_uses_word_boundaries(nd, text, more_than_zero):
    assert (nd.importance_score(text) > 0) is more_than_zero


# --- constrained JSON for topic grouping ------------------------------------------------

class _FakeClient:
    def __init__(self, reject_schema=False):
        self.calls = []
        self.reject_schema = reject_schema
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        if self.reject_schema and "response_format" in kw:
            raise ValueError("response_format not supported")
        msg = types.SimpleNamespace(content=' {"groups": []} ')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def test_schema_is_sent_as_json_schema_response_format(nd, cfg):
    client = _FakeClient()
    out = nd._openai_chat(client, "m", "p", cfg, 100, nd.GROUPS_SCHEMA)
    rf = client.calls[0]["response_format"]
    assert out == '{"groups": []}'
    assert rf["type"] == "json_schema" and rf["json_schema"]["name"] == "topic_groups"
    assert "title" not in rf["json_schema"]["schema"] and rf["json_schema"]["strict"] is True


def test_provider_without_schema_support_gets_a_plain_call(nd, cfg):
    client = _FakeClient(reject_schema=True)
    nd._openai_chat(client, "m", "p", cfg, 100, nd.GROUPS_SCHEMA)
    assert len(client.calls) == 2 and "response_format" not in client.calls[1]


def test_plain_calls_send_no_schema(nd, cfg):
    client = _FakeClient()
    nd._openai_chat(client, "m", "p", cfg, 100, None)
    assert "response_format" not in client.calls[0]


# --- run report, exit codes, log file ---------------------------------------------------

def test_report_status(nd):
    r = nd.RunReport()
    r.source("Google News", "ok")
    r.source("SEC EDGAR", "ok")
    r.source("SEC EDGAR", "error", "HTTP 500")
    assert r.exit_code() == nd.EXIT_OK                      # occasional error: fine
    r.source("SEC EDGAR", "error", "HTTP 500")
    assert r.exit_code() == nd.EXIT_DEGRADED                # failing most of the time
    assert any("SEC EDGAR failed 2 of 3" in w for w in r.all_warnings())
    r.fatal = "no stories"
    assert r.exit_code() == nd.EXIT_FAILED
    assert "Status: FAILED (no stories)" in "\n".join(r.lines())


def test_report_flags_llm_outage(nd):
    r = nd.RunReport()
    r.count("bullets: AI", 1)
    r.count("bullets: fallback (LLM unreachable)", 3)
    assert r.exit_code() == nd.EXIT_DEGRADED


def test_sources_report_into_the_run_report(nd, cfg, fake_urlopen, fixture_text):
    fake_urlopen({"news.google.com": [fixture_text("google_news.xml"), 503, 503]})
    nd.discover_google_news(nd.Company(name="Acme Widgets Plc"), cfg)
    nd.discover_google_news(nd.Company(name="Beta Plc"), cfg)
    assert nd._REPORT.sources["Google News"]["ok"] == 1
    assert nd._REPORT.sources["Google News"]["error"] == 1


def test_run_with_no_stories_anywhere_fails_and_writes_a_log(nd, cfg, tmp_path, monkeypatch, capsys):
    (tmp_path / "w.csv").write_text("Company\nAcme Plc\n", encoding="utf-8")
    cfg.watchlist, cfg.output_dir = str(tmp_path / "w.csv"), str(tmp_path)
    monkeypatch.setattr(nd, "discover", lambda c, cfg: [])
    assert nd.run(cfg) == []
    assert nd._REPORT.exit_code() == nd.EXIT_FAILED
    out = capsys.readouterr().out
    assert "no stories from any source" in out and "Run report" in out
    logs = os.listdir(tmp_path / "logs")
    assert len(logs) == 1 and logs[0].startswith("news digest ")
    assert "Run report" in (tmp_path / "logs" / logs[0]).read_text(encoding="utf-8")


def test_watchlist_problem_is_a_failed_run(nd, cfg, tmp_path):
    cfg.watchlist, cfg.output_dir, cfg.log_dir = str(tmp_path / "missing.csv"), str(tmp_path), ""
    nd.run(cfg)
    assert nd._REPORT.exit_code() == nd.EXIT_FAILED
    assert not (tmp_path / "logs").exists()   # log_dir="" turns the log off


def test_main_returns_the_run_exit_code(nd, monkeypatch):
    cfg = nd.Config()
    monkeypatch.setattr(nd, "parse_args", lambda: cfg)

    def fake_run(c):
        nd._REPORT.source("SEC EDGAR", "error", "HTTP 500")
        return ["x.pdf"]

    monkeypatch.setattr(nd, "run", fake_run)
    assert nd.main() == nd.EXIT_DEGRADED


def test_grouping_outcomes_are_counted(nd, cfg, make_article, monkeypatch):
    co = nd.Company(name="Acme Widgets Plc")
    arts = [make_article(bullet="Acme won a contract."), make_article(bullet="Acme named a CFO.")]
    monkeypatch.setattr(nd, "_llm_complete", lambda *a, **k: json.dumps(
        {"groups": [{"heading": "A", "members": [1, 2], "summary": "Acme won a contract and named a CFO."}]}))
    nd.organise(arts, co, cfg)
    monkeypatch.setattr(nd, "_llm_complete", lambda *a, **k: "nope")
    nd.organise(arts, co, cfg)
    assert nd._REPORT.counts["topic grouping: AI"] == 1
    assert nd._REPORT.counts["topic grouping: rule-based"] == 1
