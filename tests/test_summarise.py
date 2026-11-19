"""Summarising (step 5): LLM call routing and the offline fallbacks."""
import sys
import types

import pytest


def test_fallback_bullet_uses_first_two_sentences_of_body(nd, make_article):
    body = ("Acme raised guidance to £1.2bn. Orders rose 18% in the half. "
            "The CEO was upbeat. More filler text follows here to pass the length check.")
    a = make_article(body=body)
    assert nd._fallback_bullet(a) == "Acme raised guidance to £1.2bn. Orders rose 18% in the half."


def test_fallback_bullet_uses_snippet_when_body_short(nd, make_article):
    a = make_article(body="short", snippet="Acme won a £40m defence contract from the MoD")
    assert nd._fallback_bullet(a) == "Acme won a £40m defence contract from the MoD."


def test_fallback_bullet_prefers_headline_over_fragment_snippet(nd, make_article):
    a = make_article(headline="Acme wins £40m contract", snippet="Reuters")
    assert nd._fallback_bullet(a) == "Acme wins £40m contract."


def test_fallback_bullet_uses_headline_as_last_resort(nd, make_article):
    assert nd._fallback_bullet(make_article(headline="Acme wins contract")) == "Acme wins contract."


def test_filing_bullets(nd, make_article):
    with_desc = make_article(headline="SEC 10-Q filing — XPEL, Inc.", kind="filing", snippet="QUARTERLY REPORT")
    assert nd._fallback_bullet(with_desc) == "10-Q filed with the SEC: Quarterly report."
    no_desc = make_article(headline="SEC 8-K filing — XPEL, Inc.", kind="filing", snippet="8-K")
    assert nd._fallback_bullet(no_desc) == "8-K filing published on SEC EDGAR."


def test_summarise_bullet_falls_back_when_llm_unreachable(nd, cfg, make_article, capsys):
    a = make_article(body="Acme raised guidance to £1.2bn after a record half. " * 4)
    out = nd.summarise_bullet(a, nd.Company(name="Acme"), cfg)
    assert out.startswith("Acme raised guidance")
    assert "LLM bullet failed" in capsys.readouterr().out


def test_summarise_bullet_tidies_llm_output(nd, cfg, make_article, monkeypatch):
    seen = {}

    def fake(prompt, cfg, max_tokens=160):
        seen["prompt"] = prompt
        return "- Acme raised FY guidance to £1.2bn ,  citing record orders"

    monkeypatch.setattr(nd, "_llm_complete", fake)
    a = make_article(headline="Acme lifts outlook", body="Acme raised guidance to £1.2bn after a record half. " * 4)
    out = nd.summarise_bullet(a, nd.Company(name="Acme Widgets"), cfg)
    assert out == "Acme raised FY guidance to £1.2bn, citing record orders."
    assert 'about "Acme Widgets"' in seen["prompt"] and "HEADLINE: Acme lifts outlook" in seen["prompt"]


def test_summarise_bullet_skips_llm_for_tiny_content(nd, cfg, make_article, monkeypatch):
    monkeypatch.setattr(nd, "_llm_complete", lambda *a, **k: pytest.fail("LLM called"))
    a = make_article(headline="Acme wins contract", snippet="tiny")
    assert nd.summarise_bullet(a, nd.Company(name="Acme"), cfg) == "Acme wins contract."


def test_llm_complete_rejects_unknown_and_bart(nd, cfg):
    with pytest.raises(RuntimeError, match="unknown provider"):
        nd._llm_complete("hi", cfg)
    cfg.llm_provider = "bart"
    with pytest.raises(RuntimeError, match="summarise-only"):
        nd._llm_complete("hi", cfg)


def test_llm_complete_reports_missing_sdk(nd, cfg, monkeypatch):
    monkeypatch.setattr(nd, "anthropic", None)
    cfg.llm_provider = "anthropic"
    with pytest.raises(RuntimeError, match="anthropic package not installed"):
        nd._llm_complete("hi", cfg)


def test_load_bart_falls_back_to_hub_when_folder_missing(nd, cfg, monkeypatch, tmp_path):
    loaded = []
    fake = types.ModuleType("transformers")
    fake.AutoTokenizer = types.SimpleNamespace(from_pretrained=lambda src: loaded.append(("tok", src)))
    fake.AutoModelForSeq2SeqLM = types.SimpleNamespace(from_pretrained=lambda src: loaded.append(("model", src)))
    fake.pipeline = lambda task, model, tokenizer: ("pipe", task)
    monkeypatch.setitem(sys.modules, "transformers", fake)

    cfg.bart_model_dir = str(tmp_path / "missing")
    assert nd._load_bart(cfg) == ("pipe", "summarization")
    assert loaded == [("tok", "facebook/bart-large-cnn"), ("model", "facebook/bart-large-cnn")]


def test_load_bart_prefers_local_folder(nd, cfg, monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text("{}")
    loaded = []
    fake = types.ModuleType("transformers")
    fake.AutoTokenizer = types.SimpleNamespace(from_pretrained=lambda src: loaded.append(src))
    fake.AutoModelForSeq2SeqLM = types.SimpleNamespace(from_pretrained=lambda src: loaded.append(src))
    fake.pipeline = lambda task, model, tokenizer: "pipe"
    monkeypatch.setitem(sys.modules, "transformers", fake)

    cfg.bart_model_dir = str(tmp_path)
    nd._load_bart(cfg)
    assert loaded == [str(tmp_path), str(tmp_path)]
