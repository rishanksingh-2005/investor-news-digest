"""Watchlist loading (step 0) and command-line parsing."""
import openpyxl
import pytest


def _workbook(path, rows, sheet="Watchlist"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append(r)
    wb.save(path)
    return str(path)


def test_load_companies_filters_by_tier_and_skips_bad_rows(nd, cfg, tmp_path):
    cfg.watchlist = _workbook(tmp_path / "w.xlsx", [
        ["Microsoft Excel Ticker", " NAME ", "Currency", "Exchange", "Tier"],   # header case/space-insensitive
        ["#VALUE!", "Jet2 Plc", "GBp", "XLON", 1.0],
        ["#VALUE!", "Xpel, Inc.", "USD", "XNAS", 2],
        ["#VALUE!", "Tier Four Co", "USD", "XNAS", 4],      # tier not selected
        ["#VALUE!", None, "USD", "XNAS", 1],                 # no name
        ["#VALUE!", "No Tier Co", "USD", "XNAS", None],      # no tier
        ["#VALUE!", "Bad Tier Co", "USD", "XNAS", "abc"],    # non-numeric tier
        [None, None, None, None, None],
    ])
    cos = nd.load_companies(cfg)
    assert [(c.name, c.currency, c.exchange, c.tier) for c in cos] == [
        ("Jet2 Plc", "GBp", "XLON", 1),
        ("Xpel, Inc.", "USD", "XNAS", 2),
    ]
    assert cos[1].is_us and not cos[1].is_uk and cos[0].is_uk


def test_load_companies_respects_tier_selection(nd, cfg, tmp_path):
    cfg.watchlist = _workbook(tmp_path / "w.xlsx", [
        ["Name", "Tier"], ["A", 1], ["B", 2], ["C", 3],
    ])
    cfg.tiers = (2, 3)
    assert [c.name for c in nd.load_companies(cfg)] == ["B", "C"]


def test_real_watchlist_if_present(nd):
    """Sanity check on the real watchlist when it is available locally."""
    cfg = nd.Config()
    import os
    if not os.path.exists(cfg.watchlist):
        pytest.skip("Company List.xlsx not present (it may be kept out of the repo)")
    cos = nd.load_companies(cfg)
    assert cos, "no companies loaded from the real watchlist"
    assert all(c.name and c.tier in (1, 2, 3) for c in cos)


@pytest.fixture
def argv(monkeypatch):
    def set_args(*args):
        monkeypatch.setattr("sys.argv", ["news digest.py", *args])
    return set_args


def test_parse_args_defaults_follow_config(nd, argv):
    argv()
    cfg = nd.parse_args()
    d = nd.Config()
    assert cfg.tiers == d.tiers
    assert cfg.days_back == d.days_back
    assert cfg.llm_provider == d.llm_provider
    assert (cfg.use_finnhub, cfg.use_sec_edgar, cfg.use_trusted_sites) == (True, True, True)
    assert cfg.headless is False


def test_parse_args_flags(nd, argv):
    argv("--tiers", "1, 3", "--days", "7", "--provider", "anthropic", "--model", "claude-x",
         "--headless", "--no-trusted", "--no-finnhub", "--no-sec")
    cfg = nd.parse_args()
    assert cfg.tiers == (1, 3)
    assert cfg.days_back == 7
    assert cfg.llm_provider == "anthropic" and cfg.anthropic_model == "claude-x"
    assert cfg.headless and not cfg.use_trusted_sites
    assert not cfg.use_finnhub and not cfg.use_sec_edgar


@pytest.mark.parametrize("provider,attr", [
    ("ollama", "ollama_model"), ("openai", "openai_model"), ("bart", "bart_model_dir"),
])
def test_parse_args_model_override_per_provider(nd, argv, provider, attr):
    argv("--provider", provider, "--model", "custom")
    assert getattr(nd.parse_args(), attr) == "custom"


def test_cache_and_speed_flags(nd, argv):
    argv()
    cfg = nd.parse_args()
    assert cfg.use_cache is True and cfg.new_only is False and cfg.browser_workers == 3
    argv("--no-cache", "--new-only", "--browsers", "0")
    cfg = nd.parse_args()
    assert cfg.use_cache is False and cfg.new_only is True and cfg.browser_workers == 1


def test_single_tier_flag(nd, argv):
    argv("--tier", "2")
    assert nd.parse_args().tiers == (2,)


def test_invalid_provider_exits(nd, argv):
    argv("--provider", "gpt")
    with pytest.raises(SystemExit):
        nd.parse_args()
