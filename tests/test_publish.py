"""Page budget and the PDF / DOCX writers (step 6)."""
import zipfile


def _company(nd, make_article, name="Acme & Sons Plc", tier=1, n=2, start_importance=1):
    co = nd.Company(name=name, currency="GBp", exchange="XLON", tier=tier)
    for i in range(n):
        a = make_article(headline=f"{name} story {i}", url=f"https://www.reuters.com/{name[:4]}/{i}",
                         source="Reuters", published="2026-11-16T08:30:00+00:00",
                         bullet=f"{name} point {i} with <angle> & ampersand.",
                         importance=start_importance + i)
        co.articles.append(a)
    return co


# --- page budget -----------------------------------------------------------------

def test_page_budget_noop_under_cap(nd, cfg, make_article):
    cos = [_company(nd, make_article, "A", tier=2), _company(nd, make_article, "B", tier=3)]
    out = nd.apply_page_budget(cos, cfg)
    assert [len(c.articles) for c in out] == [2, 2]


def test_page_budget_ranks_by_tier_then_importance(nd, cfg, make_article):
    cfg.max_total_items = 3
    t1 = _company(nd, make_article, "T1", tier=1, n=2, start_importance=0)     # importance 0,1
    t2 = _company(nd, make_article, "T2", tier=2, n=2, start_importance=5)     # importance 5,6
    t3 = _company(nd, make_article, "T3", tier=3, n=1, start_importance=9)
    out = nd.apply_page_budget([t1, t2, t3], cfg)

    by_name = {c.name: c for c in out}
    assert sorted(a.importance for a in by_name["T1"].articles) == [0, 1]   # tier beats importance
    assert [a.importance for a in by_name["T2"].articles] == [6]            # best tier-2 item next
    assert "T3" not in by_name                                             # emptied companies dropped


def test_page_budget_every_tier1_company_keeps_its_best_item(nd, cfg, make_article):
    cfg.max_total_items = 1
    a = _company(nd, make_article, "Alpha", tier=1, n=2, start_importance=3)   # 3,4
    b = _company(nd, make_article, "Beta", tier=1, n=2, start_importance=0)    # 0,1
    out = nd.apply_page_budget([a, b], cfg)
    # the cap is 1, but each tier-1 company is protected one (its best) item
    assert {c.name: [x.importance for x in c.articles] for c in out} == {"Alpha": [4], "Beta": [1]}


def test_page_budget_drops_companies_without_news(nd, cfg, make_article):
    empty = nd.Company(name="Quiet Co", tier=2)
    out = nd.apply_page_budget([empty, _company(nd, make_article, "Busy", tier=2)], cfg)
    assert [c.name for c in out] == ["Busy"]


# --- writers -------------------------------------------------------------------------

def test_build_pdf_writes_valid_pdf(nd, cfg, make_article, tmp_path):
    co = _company(nd, make_article)
    co.articles[0].heading = "Results & Outlook"
    co.articles[0].extra_sources = [{"source": "FT", "url": "https://www.ft.com/x?a=1&b=2"}]
    path = nd.build_pdf([co, nd.Company(name="Empty Co", tier=2)], cfg, str(tmp_path / "d.pdf"))
    data = open(path, "rb").read()
    assert data.startswith(b"%PDF") and len(data) > 1500


def test_build_docx_contains_bullets_and_hyperlinks(nd, cfg, make_article, tmp_path):
    co = _company(nd, make_article)
    co.articles[0].heading = "Results"
    co.articles[0].extra_sources = [{"source": "FT", "url": "https://www.ft.com/x"}]
    path = nd.build_docx([co], cfg, str(tmp_path / "d.docx"))

    with zipfile.ZipFile(path) as z:
        doc = z.read("word/document.xml").decode("utf-8")
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
    assert "Acme &amp; Sons Plc point 0 with &lt;angle&gt; &amp; ampersand." in doc
    assert "Results" in doc and "Read full article" in doc and "FT »" in doc
    assert 'Target="https://www.reuters.com/Acme/0"' in rels
    assert 'Target="https://www.ft.com/x"' in rels


def test_labels(nd, cfg):
    cfg.tiers = (3, 1, 2)
    assert nd._tiers_label(cfg) == "1, 2 & 3"
    cfg.tiers = (1, 2)
    assert nd._tiers_label(cfg) == "1 & 2"
    cfg.tiers = (2,)
    assert nd._tiers_label(cfg) == "2"
    assert nd._fmt_date("2026-11-16T08:30:00Z") == "16 Nov 2026"
    assert nd._fmt_date("2026-11-16") == "16 Nov 2026"
    assert nd._fmt_date("") == ""
