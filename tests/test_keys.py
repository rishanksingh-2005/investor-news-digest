"""Bring-your-own API keys: Finnhub, SEC contact, LLM keys, and --doctor."""
import json

import pytest


# --- key hygiene ----------------------------------------------------------------------

@pytest.mark.parametrize("raw,clean", [
    ("abc123", "abc123"), ("  abc123 \n", "abc123"), ('"abc123"', "abc123"), ("'abc123'", "abc123"),
    ("", ""), ("your_key_here", ""), ("YOUR-API-KEY", ""), ("<paste key>", ""), ("changeme", ""),
])
def test_env_key_cleans_paste_mistakes(nd, monkeypatch, raw, clean):
    monkeypatch.setenv("FINNHUB_API_KEY", raw)
    assert nd.env_key("FINNHUB_API_KEY") == clean


@pytest.mark.parametrize("raw,sym", [
    ("AAPL", "AAPL"), ("nasdaq:aapl", "AAPL"), ("BRK.B", "BRK.B"), ("#VALUE!", ""), ("", ""), ("not a ticker", ""),
])
def test_clean_ticker(nd, raw, sym):
    assert nd._clean_ticker(raw) == sym


# --- Finnhub with the user's own key ----------------------------------------------------

def _capture(nd, monkeypatch, responses):
    """Fake urlopen that records full Request objects, replying from `responses` in order."""
    import io
    import urllib.error
    seen = []

    class R(io.BytesIO):
        status = 200

    def urlopen(req, timeout=None):
        seen.append(req)
        item = responses.pop(0)
        if isinstance(item, int):
            raise urllib.error.HTTPError(req.full_url, item, f"HTTP {item}", {}, None)
        return R(json.dumps(item).encode())

    monkeypatch.setattr(nd.urllib.request, "urlopen", urlopen)
    return seen


def test_finnhub_key_goes_in_header_never_in_url(nd, cfg, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "secret-key-123")
    seen = _capture(nd, monkeypatch, [{"result": [{"symbol": "XPEL", "description": "XPEL INC"}]}, []])
    nd.discover_finnhub(nd.Company(name="Xpel, Inc.", exchange="XNAS"), cfg)
    assert len(seen) == 2
    for req in seen:
        assert "secret-key-123" not in req.full_url and "token=" not in req.full_url
        assert req.get_header("X-finnhub-token") == "secret-key-123"


def test_finnhub_uses_watchlist_ticker_without_lookup(nd, cfg, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    news = [{"headline": "Shopify news", "url": "https://example.com/s", "datetime": 1763280000}]
    seen = _capture(nd, monkeypatch, [news])
    out = nd.discover_finnhub(nd.Company(name="Shopify Inc.", exchange="XNYS", ticker="SHOP"), cfg)
    assert len(seen) == 1 and "/company-news?symbol=SHOP&" in seen[0].full_url
    assert out[0].headline == "Shopify news"


def test_finnhub_403_skips_only_that_company(nd, cfg, monkeypatch, capsys):
    """A valid free key gets 403 for symbols outside its plan: don't disable Finnhub."""
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    seen = _capture(nd, monkeypatch, [403, [{"headline": "XPEL news", "url": "https://e.com/x"}]])
    assert nd.discover_finnhub(nd.Company(name="Shopify", exchange="XTSE", ticker="SHOP.TO"), cfg) == []
    assert nd._FINNHUB_DISABLED is False
    out = nd.discover_finnhub(nd.Company(name="Xpel, Inc.", exchange="XNAS", ticker="XPEL"), cfg)
    assert len(seen) == 2 and out[0].headline == "XPEL news"
    assert "isn't covered by this key's plan" in capsys.readouterr().out


def test_finnhub_rate_limit_is_retried(nd, cfg, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    seen = _capture(nd, monkeypatch, [429, [{"headline": "XPEL news", "url": "https://e.com/x"}]])
    out = nd.discover_finnhub(nd.Company(name="Xpel, Inc.", exchange="XNAS", ticker="XPEL"), cfg)
    assert len(seen) == 2 and len(out) == 1


def test_finnhub_error_payload_is_reported(nd, cfg, monkeypatch, capsys):
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    _capture(nd, monkeypatch, [{"error": "Invalid symbol"}])
    assert nd.discover_finnhub(nd.Company(name="X", exchange="XNAS", ticker="ZZZZ"), cfg) == []
    assert "Invalid symbol" in capsys.readouterr().out


def test_placeholder_finnhub_key_counts_as_missing(nd, cfg, monkeypatch, capsys):
    monkeypatch.setenv("FINNHUB_API_KEY", "your_finnhub_key")
    _capture(nd, monkeypatch, [])
    assert nd.discover_finnhub(nd.Company(name="X", exchange="XNAS"), cfg) == []
    assert "finnhub.io/register" in capsys.readouterr().out


# --- SEC contact ----------------------------------------------------------------------

def test_sec_user_agent(nd, monkeypatch, capsys):
    monkeypatch.setenv("SEC_USER_AGENT", '"Jane Smith jane@example.com"')
    assert nd.sec_user_agent() == "Jane Smith jane@example.com"
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("value", ["", "Jane Smith"])
def test_sec_user_agent_missing_email_warns_once(nd, monkeypatch, capsys, value):
    monkeypatch.setenv("SEC_USER_AGENT", value)
    first = nd.sec_user_agent()
    nd.sec_user_agent()
    assert first == (value or nd._SEC_DEFAULT_UA)
    assert capsys.readouterr().out.count("SEC_USER_AGENT") == 1


# --- LLM keys -------------------------------------------------------------------------

@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_missing_llm_key_raises_cleanly(nd, cfg, provider):
    if (provider == "openai" and nd.OpenAI is None) or (provider == "anthropic" and nd.anthropic is None):
        pytest.skip(f"{provider} SDK not installed")
    cfg.llm_provider = provider
    with pytest.raises(RuntimeError, match="not set"):
        nd._llm_complete("hi", cfg)


# --- --doctor -------------------------------------------------------------------------

def _doctor(nd, cfg, monkeypatch, tmp_path, probes, ollama_models=("llama3.2:3b",)):
    import io
    cfg.watchlist = str(tmp_path / "w.csv")
    (tmp_path / "w.csv").write_text("Company\nAcme\n", encoding="utf-8")
    calls = []

    def probe(url, headers, timeout=15):
        calls.append((url, headers))
        for needle, code in probes.items():
            if needle in url:
                return code
        raise AssertionError(f"unexpected probe {url}")

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        assert url.endswith("/models"), url
        return R(json.dumps({"data": [{"id": m} for m in ollama_models]}).encode())

    monkeypatch.setattr(nd, "_probe", probe)
    monkeypatch.setattr(nd.urllib.request, "urlopen", urlopen)
    cfg.llm_provider = "ollama"
    return calls


def test_doctor_all_good_and_never_prints_secrets(nd, cfg, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FINNHUB_API_KEY", "fh-secret-999")
    monkeypatch.setenv("SEC_USER_AGENT", "Jane Smith jane@example.com")
    calls = _doctor(nd, cfg, monkeypatch, tmp_path, {"finnhub.io": 200, "efts.sec.gov": 200})
    nd.doctor(cfg)
    out = capsys.readouterr().out
    assert "fh-secret-999" not in out
    assert "Finnhub              key valid" in out and "EDGAR reachable" in out and "1 companies" in out
    fh = next(h for u, h in calls if "finnhub" in u)
    assert fh["X-Finnhub-Token"] == "fh-secret-999"


def test_doctor_flags_rejected_finnhub_key_and_missing_sec_contact(nd, cfg, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FINNHUB_API_KEY", "bad")
    _doctor(nd, cfg, monkeypatch, tmp_path, {"finnhub.io": 401})
    code = nd.doctor(cfg)
    out = capsys.readouterr().out
    assert "key rejected (401)" in out and "SEC_USER_AGENT not set" in out
    assert code == 1


def test_doctor_without_optional_keys_does_not_fail_on_them(nd, cfg, monkeypatch, tmp_path, capsys):
    calls = _doctor(nd, cfg, monkeypatch, tmp_path, {})
    nd.doctor(cfg)
    out = capsys.readouterr().out
    assert "no key (optional)" in out
    assert calls == []   # nothing to test without keys
    assert "[FAIL] Finnhub" not in out


def test_doctor_reports_missing_ollama_model(nd, cfg, monkeypatch, tmp_path, capsys):
    _doctor(nd, cfg, monkeypatch, tmp_path, {}, ollama_models=("mistral",))
    assert nd.doctor(cfg) == 1
    assert "ollama pull llama3.2:3b" in capsys.readouterr().out


# --- Finnhub symbol search ------------------------------------------------------------

@pytest.mark.parametrize("name,queries", [
    ("Douglas Dynamics, Inc.", ["douglas dynamics", "douglas"]),
    ("Seaport Entertainment Group Inc.", ["seaport"]),      # 21 chars is over the limit
    ("Xpel, Inc.", ["xpel"]),
])
def test_finnhub_queries_fit_the_length_limit(nd, name, queries):
    qs = nd._finnhub_queries(name)
    assert qs == queries and all(len(q) <= nd._FINNHUB_MAX_QUERY for q in qs)


def test_finnhub_match_rejects_a_different_company(nd):
    results = [{"symbol": "GLIBA", "description": "GCI LIBERTY INC-A"},
               {"symbol": "LBTYA", "description": "LIBERTY GLOBAL LTD-A"}]
    assert nd._finnhub_match("Liberty Capital Corporation", results) == ""
    assert nd._finnhub_match("Douglas Dynamics, Inc.", [
        {"symbol": "DOU.DE", "description": "DOUGLAS AG"},
        {"symbol": "PLOW", "description": "DOUGLAS DYNAMICS INC"}]) == "PLOW"


def test_finnhub_lookup_falls_back_to_shorter_query_on_422(nd, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    seen = _capture(nd, monkeypatch, [422, {"result": [{"symbol": "PLOW", "description": "DOUGLAS DYNAMICS INC"}]}])
    assert nd._finnhub_lookup_symbol("Douglas Dynamics, Inc.", "k") == "PLOW"
    assert "q=douglas+dynamics" in seen[0].full_url and "q=douglas" in seen[1].full_url
