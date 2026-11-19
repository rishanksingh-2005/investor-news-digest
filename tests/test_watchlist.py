"""Bring-your-own watchlist: any file format, any sensible column names."""
import json
import os

import openpyxl
import pytest


def _xlsx(path, sheets):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    wb.save(path)
    return str(path)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def _load(nd, cfg, path, **kw):
    cfg.watchlist = path
    for k, v in kw.items():
        setattr(cfg, k, v)
    return nd.load_companies(cfg)


def test_csv_with_other_header_names_and_exchange_names(nd, cfg, tmp_path):
    path = _write(tmp_path / "list.csv",
                  "Issuer Name,Symbol,Market,Domicile,CCY,Priority\n"
                  "Apple Inc.,AAPL,NASDAQ,USA,USD,Tier 1\n"
                  "Unilever PLC,ULVR,LSE,United Kingdom,GBp,P2\n"
                  "Shopify Inc.,SHOP,TSX,Canada,CAD,3.0\n")
    cos = _load(nd, cfg, path)
    assert [(c.name, c.ticker, c.exchange, c.country, c.currency, c.tier) for c in cos] == [
        ("Apple Inc.", "AAPL", "XNAS", "US", "USD", 1),
        ("Unilever PLC", "ULVR", "XLON", "UK", "GBp", 2),
        ("Shopify Inc.", "SHOP", "XTSE", "CA", "CAD", 3),
    ]
    assert cos[0].is_us and cos[1].is_uk and cos[2].is_north_am and not cos[2].is_us


def test_semicolon_csv_and_header_not_on_first_row(nd, cfg, tmp_path):
    path = _write(tmp_path / "eu.csv",
                  "Client watchlist export;;\n;;\nCompany;Exchange;Tier\nASML Holding;XAMS;1\n")
    cos = _load(nd, cfg, path)
    assert [(c.name, c.exchange, c.tier) for c in cos] == [("ASML Holding", "XAMS", 1)]


def test_no_tier_column_means_every_company_is_default_tier(nd, cfg, tmp_path):
    path = _write(tmp_path / "names.csv", "Company\nAcme Plc\nBeta Inc\n")
    cos = _load(nd, cfg, path)
    assert [(c.name, c.tier) for c in cos] == [("Acme Plc", 1), ("Beta Inc", 1)]
    cfg.default_tier = 2
    assert [c.tier for c in nd.load_companies(cfg)] == [2, 2]


def test_plain_text_list_one_name_per_line(nd, cfg, tmp_path):
    path = _write(tmp_path / "names.txt", "# my companies\nXpel, Inc.\n\nJet2 Plc  # airline\n")
    assert [c.name for c in _load(nd, cfg, path)] == ["Xpel, Inc.", "Jet2 Plc"]


def test_headerless_single_column_csv(nd, cfg, tmp_path):
    path = _write(tmp_path / "names.csv", "Acme Plc\nBeta Inc\n")
    assert [c.name for c in _load(nd, cfg, path)] == ["Acme Plc", "Beta Inc"]


@pytest.mark.parametrize("data", [
    [{"company": "Acme Plc", "exchange": "AIM", "tier": 1}, {"company": "Beta Inc", "tier": "2"}],
    {"companies": [{"Name": "Acme Plc", "Exchange": "AIM", "Tier": 1}, {"Name": "Beta Inc", "Tier": 2}]},
])
def test_json_list_of_objects(nd, cfg, tmp_path, data):
    path = _write(tmp_path / "w.json", json.dumps(data))
    assert [(c.name, c.exchange, c.tier) for c in _load(nd, cfg, path)] == [
        ("Acme Plc", "XLON", 1), ("Beta Inc", "", 2)]


def test_json_list_of_strings(nd, cfg, tmp_path):
    path = _write(tmp_path / "w.json", '["Acme Plc", "Beta Inc"]')
    assert [c.name for c in _load(nd, cfg, path)] == ["Acme Plc", "Beta Inc"]


def test_excel_finds_the_sheet_with_companies(nd, cfg, tmp_path):
    path = _xlsx(tmp_path / "w.xlsx", {
        "Notes": [["Read me"], ["This workbook is the client's export"]],
        "Holdings": [["Security Name", "Tier"], ["Acme Plc", 1]],
    })
    assert [c.name for c in _load(nd, cfg, path)] == ["Acme Plc"]
    with pytest.raises(nd.WatchlistError, match="Missing"):
        _load(nd, cfg, path, sheet_name="Missing")


def test_explicit_column_map(nd, cfg, tmp_path):
    path = _write(tmp_path / "w.csv", "Ref,Long Description,Bucket\nX1,Acme Plc,2\n")
    with pytest.raises(nd.WatchlistError, match="company-name column"):
        _load(nd, cfg, path)
    cos = _load(nd, cfg, path, column_map=nd.parse_column_map("name=Long Description, tier=Bucket"))
    assert [(c.name, c.tier) for c in cos] == [("Acme Plc", 2)]
    with pytest.raises(nd.WatchlistError, match="not found"):
        _load(nd, cfg, path, column_map={"name": "Nope"})


@pytest.mark.parametrize("spec", ["name", "colour=Red"])
def test_bad_column_map(nd, spec):
    with pytest.raises(nd.WatchlistError):
        nd.parse_column_map(spec)


def test_duplicates_and_bad_tiers_are_reported(nd, cfg, tmp_path):
    path = _write(tmp_path / "w.csv", "Name,Tier\nAcme,1\nACME,1\nBeta,high\nGamma,\n")
    cfg.watchlist = path
    cos, report = nd.read_watchlist(cfg)
    assert [c.name for c in cos] == ["Acme"]
    assert report["duplicates"] == 1
    assert len(report["skipped"]) == 2 and "'high'" in report["skipped"][0] and "no tier" in report["skipped"][1]


@pytest.mark.parametrize("raw,mic", [
    ("Nasdaq", "XNAS"), ("NYSE", "XNYS"), ("nyse american", "XASE"), ("LSE", "XLON"),
    ("xlon", "XLON"), ("TSX-V", "XTSX"), ("", ""), ("#VALUE!", ""), ("Some Local Exchange", "Some Local Exchange"),
])
def test_normalise_exchange(nd, raw, mic):
    assert nd.normalise_exchange(raw) == mic


@pytest.mark.parametrize("raw,tier", [(1, 1), (2.0, 2), ("3", 3), ("Tier 2", 2), ("t1", 1), ("x", None), (None, None)])
def test_parse_tier(nd, raw, tier):
    assert nd.parse_tier(raw) == tier


def test_country_drives_us_checks_when_exchange_missing(nd):
    assert nd.Company("A", country="US").is_us
    assert not nd.Company("A", exchange="XLON", country="US").is_us
    assert nd.Company("A", country="CA").is_north_am


def test_companies_from_command_line_need_no_file(nd, cfg):
    cfg.watchlist = "does-not-exist.xlsx"
    cfg.companies = ["Acme Plc", " "]
    assert [(c.name, c.tier) for c in nd.load_companies(cfg)] == [("Acme Plc", 1)]


def test_missing_and_legacy_xls(nd, cfg, tmp_path):
    with pytest.raises(nd.WatchlistError, match="not found"):
        _load(nd, cfg, str(tmp_path / "nope.csv"))
    old = tmp_path / "old.xls"
    old.write_bytes(b"\xd0\xcf")
    with pytest.raises(nd.WatchlistError, match="Save As"):
        _load(nd, cfg, str(old))


@pytest.mark.parametrize("ext", [".csv", ".xlsx", ".json"])
def test_template_round_trips(nd, cfg, tmp_path, ext):
    path = str(tmp_path / f"starter{ext}")
    nd.write_watchlist_template(path)
    cos = _load(nd, cfg, path)
    assert [c.name for c in cos] == [r[0] for r in nd.WATCHLIST_TEMPLATE[1:]]
    assert cos[0].exchange == "XNAS" and cos[0].tier == 1
    with pytest.raises(nd.WatchlistError, match="already exists"):
        nd.write_watchlist_template(path)


def test_shipped_example_watchlist_loads(nd, cfg):
    path = os.path.join(os.path.dirname(nd.__file__), "examples", "watchlist.example.csv")
    cos = _load(nd, cfg, path)
    assert len(cos) == 5 and {c.tier for c in cos} == {1, 2, 3}
    assert all(c.exchange in nd.EXCHANGE_SOURCES or c.exchange in nd.NORTH_AM_EXCHANGES for c in cos)


def test_check_command_reports(nd, cfg, tmp_path, capsys):
    cfg.watchlist = _write(tmp_path / "w.csv", "Company,Exchange,Tier\nAcme,XLON,1\nBeta,Mars Exchange,2\n")
    assert nd.check_watchlist(cfg) == 0
    out = capsys.readouterr().out
    assert "name     <- 'Company'" in out and "Tier 1:" in out and "Mars Exchange" in out
    cfg.watchlist = str(tmp_path / "missing.csv")
    assert nd.check_watchlist(cfg) == 1


def test_default_watchlist_prefers_env(nd, monkeypatch):
    monkeypatch.setenv("WATCHLIST", "/some/where.csv")
    assert nd.default_watchlist() == "/some/where.csv"
    monkeypatch.delenv("WATCHLIST")
    assert os.path.basename(nd.default_watchlist()) in {
        "watchlist.xlsx", "watchlist.csv", "watchlist.json", "watchlist.txt",
        "Company List.xlsx", "watchlist.example.csv"}
