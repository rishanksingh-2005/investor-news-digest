"""Dedupe (step 4) and topic grouping / merging (organise)."""
import json

import pytest


def test_dedupe_collapses_same_url_and_near_identical_titles(nd, cfg, make_article):
    a = make_article(headline="Acme Widgets wins £40m contract", url="https://www.reuters.com/x?utm_source=a")
    same_url = make_article(headline="Totally different words here", url="https://reuters.com/x#comments")
    same_title = make_article(headline="Acme Widgets wins a £40m contract", url="https://www.cnbc.com/y")
    distinct = make_article(headline="Acme Widgets appoints new chief financial officer", url="https://ft.com/z")
    assert nd.dedupe([a, same_url, same_title, distinct], cfg) == [a, distinct]


def test_same_event_and_heuristic_groups(nd, cfg, make_article):
    co = nd.Company(name="Acme Widgets Plc")
    a = make_article(bullet="Acme Widgets raised full-year revenue guidance to £1.2bn after record orders.")
    b = make_article(bullet="Acme Widgets lifted its full-year revenue guidance to £1.2bn on record orders.")
    c = make_article(headline="Acme names new CFO", bullet="Acme appointed Jo Smith as chief financial officer.")
    groups = nd._heuristic_groups([a, b, c], co, cfg)
    as_sets = sorted([sorted(id(x) for x in g) for g in groups])
    assert as_sets == sorted([sorted([id(a), id(b)]), [id(c)]])


def test_organise_falls_back_to_heuristic_and_keeps_filings(nd, cfg, make_article):
    co = nd.Company(name="Acme Widgets Plc")
    a = make_article(source="Reuters", url="https://www.reuters.com/a",
                     bullet="Acme Widgets raised full-year revenue guidance to £1.2bn after record orders.")
    b = make_article(source="Blog", url="https://someblog.example/b",
                     bullet="Acme Widgets lifted its full-year revenue guidance to £1.2bn on record orders, said the blog.")
    f1 = make_article(headline="SEC 8-K filing — Acme", kind="filing", url="https://www.sec.gov/1")
    f2 = make_article(headline="SEC 10-Q filing — Acme", kind="filing", url="https://www.sec.gov/2")

    out = nd.organise([a, b, f1, f2], co, cfg)   # offline provider -> heuristic path

    news = [x for x in out if x.kind != "filing"]
    assert len(news) == 1
    rep = news[0]
    assert rep is a                                    # trusted source wins over the longer bullet
    assert [e["url"] for e in rep.extra_sources] == ["https://someblog.example/b"]
    assert out[-2:] == [f1, f2]                        # filings pass straight through, at the end
    assert (f1.heading, f2.heading) == ("SEC filings", "")


def test_organise_single_article_skips_llm(nd, cfg, make_article, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("LLM should not be called for a single article")
    monkeypatch.setattr(nd, "_llm_organise", boom)
    co = nd.Company(name="Acme Widgets Plc")
    a = make_article(bullet="Acme Widgets won a £40m contract.")
    assert nd.organise([a], co, cfg) == [a]


def test_organise_uses_llm_grouping_when_valid(nd, cfg, make_article, monkeypatch):
    co = nd.Company(name="Acme Widgets Plc")
    a = make_article(url="https://www.reuters.com/a", bullet="Acme won a £40m defence contract.")
    b = make_article(url="https://example.com/b", headline="Acme names Jo Smith finance chief",
                     bullet="Acme named a new CFO.")
    reply = json.dumps([
        {"heading": "Contracts", "members": [1], "summary": "acme won a £40m defence contract"},
        {"heading": "Leadership", "members": [2], "summary": "Acme appointed Jo Smith as CFO"},
    ])
    monkeypatch.setattr(nd, "_llm_complete", lambda prompt, cfg, max_tokens=160, **kw: "Sure!\n" + reply)
    out = nd.organise([a, b], co, cfg)
    assert [(x.heading, x.bullet) for x in out] == [
        ("Contracts", "acme won a £40m defence contract."),
        ("Leadership", "Acme appointed Jo Smith as CFO."),
    ]


def test_llm_organise_repairs_a_grouping_that_forgets_a_bullet(nd, cfg, make_article, monkeypatch):
    co = nd.Company(name="Acme Widgets Plc")
    arts = [make_article(bullet="one."), make_article(bullet="two.")]
    monkeypatch.setattr(nd, "_llm_complete",
                        lambda *a, **k: '{"groups": [{"heading": "x", "members": [1], "summary": "s"}]}')
    groups = nd._llm_organise(arts, co, cfg)
    assert [(h, s, [arts.index(m) for m in ms]) for h, s, ms in groups] == [("x", "s", [0]), ("", "", [1])]


@pytest.mark.parametrize("answer,expected", [
    # repeated / out-of-range numbers dropped; that group's summary dropped with them
    ('{"groups": [{"heading": "A", "members": [1, 2], "summary": "AB"},'
     ' {"heading": "B", "members": [2, 9], "summary": "B9"}, {"heading": "C", "members": [3], "summary": "C"}]}',
     [("A", "AB", [1, 2]), ("C", "C", [3])]),
    # bare list (older format, or a provider without schema support) with prose around it
    ('Sure! [{"heading": "A", "members": ["1", 2, 3], "summary": "all"}] Hope that helps.',
     [("A", "all", [1, 2, 3])]),
])
def test_parse_groups_repairs_small_slips(nd, answer, expected):
    assert nd._parse_groups(answer, 3) == expected


@pytest.mark.parametrize("answer", [
    "no json", "[[1, 2, 3]]", '{"groups": "x"}', '{"groups": [{"heading": "A"}]}',
    '{"groups": [{"heading": "A", "members": [1], "summary": ""}]}',   # places 1 of 3
])
def test_parse_groups_rejects_unusable_answers(nd, answer):
    with pytest.raises(ValueError):
        nd._parse_groups(answer, 3)


def test_merged_summary_with_invented_figure_is_not_used(nd, cfg, make_article, monkeypatch):
    co = nd.Company(name="Acme Widgets Plc")
    a = make_article(url="https://www.reuters.com/a", bullet="Acme won a £40m defence contract.")
    b = make_article(url="https://example.com/b", bullet="Acme's defence contract is worth £40m.")
    reply = '{"groups": [{"heading": "Contracts", "members": [1, 2], "summary": "Acme won a £400m deal."}]}'
    monkeypatch.setattr(nd, "_llm_complete", lambda *x, **k: reply)
    out = nd.organise([a, b], co, cfg)
    assert len(out) == 1 and "£400m" not in out[0].bullet and "£40m" in out[0].bullet
    assert nd._REPORT.counts["summaries: replaced by number check"] == 1
