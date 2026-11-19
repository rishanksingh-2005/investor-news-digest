"""The whole pipeline end to end — offline (discovery and the browser are faked)."""
from datetime import datetime, timedelta, timezone

import openpyxl
import pytest


@pytest.fixture
def offline_run(nd, cfg, tmp_path, monkeypatch):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Watchlist"
    for row in [["Name", "Currency", "Exchange", "Tier"],
                ["Acme Widgets Plc", "GBp", "XLON", 1],
                ["Quiet Holdings Plc", "GBp", "XLON", 2]]:
        ws.append(row)
    wb.save(tmp_path / "w.xlsx")

    cfg.watchlist = str(tmp_path / "w.xlsx")
    cfg.output_dir = str(tmp_path)
    cfg.use_cache = True
    cfg.cache_path = str(tmp_path / "cache.sqlite")
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

    def discover(c, cfg):
        if c.name.startswith("Quiet"):
            return []
        return [nd.Article(headline="Acme Widgets wins £40m defence contract",
                           url="https://news.google.com/rss/articles/ACME1", source="Reuters",
                           published=recent, snippet="Acme Widgets won a £40m contract.")]

    def browser(articles, cfg):
        for a in articles:
            a.final_url = "https://www.reuters.com/acme-contract"
            a.body = ("Acme Widgets Plc said on Monday it had won a £40m defence contract, its largest "
                      "ever, lifting its order book by 18%. ") * 6
            a.authentic = True
            browser.calls += 1
    browser.calls = 0

    monkeypatch.setattr(nd, "discover", discover)
    monkeypatch.setattr(nd, "_extract_with_browsers", browser)
    return cfg, browser


def test_run_publishes_pdf_and_docx(nd, offline_run, capsys):
    cfg, browser = offline_run
    paths = nd.run(cfg)
    assert [p.rsplit(".", 1)[1] for p in paths] == ["pdf", "docx"]
    out = capsys.readouterr().out
    assert "1 companies with material news" in out and "Done in" in out
    assert browser.calls == 1


def test_second_run_reuses_cache_and_new_only_skips_published(nd, offline_run, capsys):
    cfg, browser = offline_run
    nd.run(cfg)
    capsys.readouterr()

    nd.run(cfg)                                  # rerun: article comes from the cache
    assert browser.calls == 1
    assert "1 from cache" in capsys.readouterr().out

    cfg.new_only = True                          # already published -> nothing new
    assert nd.run(cfg) == []
    assert "Nothing to publish" in capsys.readouterr().out


def test_no_cache_run_writes_no_cache_file(nd, offline_run, tmp_path):
    cfg, browser = offline_run
    cfg.use_cache = False
    nd.run(cfg)
    nd.run(cfg)
    assert browser.calls == 2
    assert not (tmp_path / "cache.sqlite").exists()


def test_run_restores_disabled_cache_afterwards(nd, offline_run):
    cfg, _ = offline_run
    nd.run(cfg)
    assert nd._CACHE.enabled is False
