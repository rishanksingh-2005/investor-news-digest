"""Product features: the config.yaml settings file, email delivery, and the
installable package / `newsdigest` command."""
import os
import subprocess
import sys
from email import message_from_bytes as _parse_mail, policy as _mail_policy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# --- config.yaml ------------------------------------------------------------------------

def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_config_file_sets_values_with_types_and_relative_paths(nd, tmp_path):
    cfg = nd.Config()
    path = _write(tmp_path / "config.yaml", """
watchlist: lists/clients.xlsx
tiers: [1, 2]
days_back: "7"
headless: yes
polite_delay: 2
use_gdelt: on
column_map: {name: Issuer, tier: Priority}
email_to: [a@example.com, b@example.com]
sheet_name: Holdings
""")
    applied = nd.load_config(path, cfg)
    assert cfg.watchlist == os.path.normpath(str(tmp_path / "lists" / "clients.xlsx"))
    assert cfg.tiers == (1, 2) and cfg.days_back == 7 and cfg.headless is True
    assert cfg.polite_delay == 2.0 and cfg.use_gdelt is True
    assert cfg.column_map == {"name": "Issuer", "tier": "Priority"}
    assert cfg.email_to == ["a@example.com", "b@example.com"] and cfg.sheet_name == "Holdings"
    assert cfg.config_path == path and "tiers" in applied


def test_config_sections_are_just_for_readability(nd, tmp_path):
    cfg = nd.Config()
    nd.load_config(_write(tmp_path / "c.yaml", "sources:\n  use_finnhub: false\nllm:\n  llm_provider: anthropic\n"), cfg)
    assert cfg.use_finnhub is False and cfg.llm_provider == "anthropic"


@pytest.mark.parametrize("text,message", [
    ("day_back: 7\n", "did you mean 'days_back'"),
    ("days_back: soon\n", "whole number"),
    ("headless: maybe\n", "true or false"),
    ("tiers: [1, two]\n", "list of whole numbers"),
    ("llm_provider: gpt\n", "ollama, openai, anthropic or bart"),
    ("doctor: true\n", "unknown setting"),                 # command-line actions aren't settings
    ("- just a list\n", "setting: value"),
    ("days_back: [\n", "not valid YAML"),
    ("column_map: {colour: Red}\n", "unknown"),
])
def test_config_mistakes_are_explained(nd, tmp_path, text, message):
    with pytest.raises((nd.ConfigError, nd.WatchlistError), match=message):
        nd.load_config(_write(tmp_path / "c.yaml", text), nd.Config())


def test_flags_override_the_config_file(nd, tmp_path, monkeypatch):
    path = _write(tmp_path / "config.yaml", "days_back: 7\nuse_trusted_sites: false\nsheet_name: Holdings\n"
                                            "column_map: {name: Issuer}\n")
    cfg = nd.parse_args(["--config", path])
    assert cfg.days_back == 7 and cfg.use_trusted_sites is False
    assert cfg.sheet_name == "Holdings" and cfg.column_map == {"name": "Issuer"}   # not wiped by absent flags
    cfg = nd.parse_args(["--config", path, "--days", "30"])
    assert cfg.days_back == 30


def test_config_is_found_by_env_var_and_project_folder(nd, tmp_path, monkeypatch):
    path = _write(tmp_path / "x.yaml", "days_back: 9\n")
    monkeypatch.setenv("NEWS_DIGEST_CONFIG", path)
    assert nd.parse_args([]).days_back == 9
    monkeypatch.delenv("NEWS_DIGEST_CONFIG")
    monkeypatch.setattr(nd, "HERE", str(tmp_path))
    _write(tmp_path / "config.yaml", "days_back: 11\n")
    assert nd.find_config() == str(tmp_path / "config.yaml")
    assert nd.parse_args([]).days_back == 11


def test_bad_config_stops_with_a_clear_message(nd, tmp_path):
    with pytest.raises(SystemExit, match="Config problem: .*unknown setting"):
        nd.parse_args(["--config", _write(tmp_path / "c.yaml", "colour: red\n")])
    with pytest.raises(SystemExit, match="not found"):
        nd.parse_args(["--config", str(tmp_path / "missing.yaml")])


def test_starter_config_lists_every_setting_and_loads_back(nd, tmp_path):
    text = nd.starter_config()
    for name in ("days_back", "use_gdelt", "verify_figures", "email_to", "smtp_host", "llm_max_failures"):
        assert f"{name}:" in text
    for name in ("doctor:", "check_only:", "companies:"):
        assert name not in text
    assert "API keys don't go here" in text
    path = tmp_path / "config.yaml"
    nd.write_starter_config(str(path))
    cfg = nd.Config()
    nd.load_config(str(path), cfg)                      # the starter file itself is valid
    assert cfg.tiers == nd.Config().tiers
    with pytest.raises(nd.ConfigError, match="already exists"):
        nd.write_starter_config(str(path))


def test_init_config_command(nd, tmp_path, monkeypatch, capsys):
    out = tmp_path / "my.yaml"
    assert nd.main(["--init-config", str(out)]) == 0
    assert out.exists() and "Wrote" in capsys.readouterr().out


# --- email delivery ---------------------------------------------------------------------

class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.calls, self.sent = host, port, [], []
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, pw):
        self.calls.append(("login", user, pw))
        if pw == "wrong":
            import smtplib
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        self.calls.append("quit")


@pytest.fixture
def smtp(nd, monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(nd.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(nd.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("SMTP_PASSWORD", "app-password-123")
    return FakeSMTP


def _mail_cfg(nd, **kw):
    cfg = nd.Config()
    cfg.email_to, cfg.smtp_host, cfg.smtp_user = ["team@example.com", "boss@example.com"], "smtp.example.com", "me@example.com"
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def test_send_attaches_files_and_logs_in(nd, smtp, tmp_path):
    pdf, docx = tmp_path / "news digest 2026-11-19.pdf", tmp_path / "news digest 2026-11-19.docx"
    pdf.write_bytes(b"%PDF-1.4 test")
    docx.write_bytes(b"PK docx")
    cfg = _mail_cfg(nd)
    assert nd.send(cfg, [str(pdf), str(docx)], "News digest 2026-11-19", "body text") is True
    server = smtp.instances[0]
    assert (server.host, server.port) == ("smtp.example.com", 587)
    assert server.calls[:3] == ["ehlo", "starttls", "ehlo"]
    assert ("login", "me@example.com", "app-password-123") in server.calls and server.calls[-1] == "quit"
    msg = _parse_mail(server.sent[0].as_bytes(), policy=_mail_policy.default)
    assert msg["To"] == "team@example.com, boss@example.com" and msg["From"] == "me@example.com"
    names = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert names == ["news digest 2026-11-19.pdf", "news digest 2026-11-19.docx"]
    assert nd._REPORT.sources["Email"]["ok"] == 1


def test_ssl_mode_and_no_login(nd, smtp):
    cfg = _mail_cfg(nd, smtp_security="ssl", smtp_port=465, smtp_user="", email_from="digest@example.com")
    assert nd.send(cfg, [], "s", "b")
    assert smtp.instances[0].calls == ["quit"]            # implicit TLS: no starttls, no login


@pytest.mark.parametrize("change,expected", [
    ({"smtp_host": ""}, "smtp_host is not set"),
    ({"smtp_security": "tls"}, "starttls, ssl or none"),
    ({"smtp_user": "", "email_from": ""}, "email_from"),
])
def test_send_failures_are_reported_not_raised(nd, smtp, capsys, change, expected):
    assert nd.send(_mail_cfg(nd, **change), [], "s", "b") is False
    assert expected in capsys.readouterr().out
    assert nd._REPORT.sources["Email"]["error"] == 1
    assert nd._REPORT.exit_code() == nd.EXIT_DEGRADED


def test_wrong_password_and_missing_password(nd, smtp, monkeypatch, capsys):
    monkeypatch.setenv("SMTP_PASSWORD", "wrong")
    assert not nd.send(_mail_cfg(nd), [], "s", "b")
    assert "app password" in capsys.readouterr().out
    monkeypatch.delenv("SMTP_PASSWORD")
    assert not nd.send(_mail_cfg(nd), [], "s", "b")
    assert "SMTP_PASSWORD is not set" in capsys.readouterr().out


def test_no_recipients_means_no_email(nd, smtp):
    assert nd.send(nd.Config(), [], "s", "b") is False and smtp.instances == []


def test_subject_placeholders(nd):
    cfg = nd.Config()
    cfg.email_subject = "Digest {date}: {items} points, {companies} companies (Tier {tiers})"
    assert nd.subject_for(cfg, "2026-11-19", "1 & 2", 5, 9) == "Digest 2026-11-19: 9 points, 5 companies (Tier 1 & 2)"
    cfg.email_subject = "Digest {oops}"
    assert nd.subject_for(cfg, "2026-11-19", "1", 1, 1) == "News digest 2026-11-19"


def test_doctor_email_check_logs_in_without_sending(nd, smtp):
    status, detail = nd.check(_mail_cfg(nd))
    assert status == "OK" and "login OK" in detail and smtp.instances[0].sent == []
    assert nd.check(nd.Config())[0] == "--"


def test_email_flags(nd):
    assert nd.parse_args(["--email-to", "a@x.com", "--email-to", "b@x.com"]).email_to == ["a@x.com", "b@x.com"]
    assert nd.parse_args(["--email-to", "a@x.com", "--no-email"]).email_to == []


def test_run_emails_the_published_digest(nd, cfg, smtp, tmp_path, monkeypatch):
    """End to end: a run with email_to set sends the PDF it just wrote."""
    from datetime import datetime, timedelta, timezone
    (tmp_path / "w.csv").write_text("Company,Exchange,Tier\nAcme Widgets Plc,XLON,1\n", encoding="utf-8")
    cfg.watchlist, cfg.output_dir = str(tmp_path / "w.csv"), str(tmp_path)
    cfg.email_to, cfg.smtp_host, cfg.smtp_user = ["team@example.com"], "smtp.example.com", "me@example.com"
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    monkeypatch.setattr(nd, "discover", lambda c, cfg: [nd.Article(
        headline="Acme Widgets wins £40m defence contract", url="https://www.reuters.com/acme", source="Reuters",
        published=recent, snippet="Acme Widgets won a £40m contract.")])

    def read(articles, cfg):
        for a in articles:
            a.body = ("Acme Widgets Plc said on Monday it had won a £40m defence contract, its largest ever, "
                      "lifting its order book by 18%. ") * 6
            a.authentic, a.final_url = True, "https://www.reuters.com/acme"
        return {"cached": 0, "http": len(articles), "browser": 0, "unreadable": 0, "redated": 0}

    monkeypatch.setattr(nd, "extract_all", read)
    paths = nd.run(cfg)
    msg = _parse_mail(smtp.instances[0].sent[0].as_bytes(), policy=_mail_policy.default)
    assert [p.get_filename() for p in msg.walk() if p.get_filename()] == [os.path.basename(p) for p in paths]
    text = msg.get_body(("plain",)).get_content()
    assert "1 point across 1 company" in text and "£40m defence contract" in text
    page = msg.get_body(("html",)).get_content()
    assert "Acme Widgets Plc" in page and 'href="https://www.reuters.com/acme"' in page
    assert msg.get_content_type() == "multipart/mixed"     # HTML + text body, then the attachments


def _digest(nd):
    c = nd.Company(name="Acme <Widgets> & Co", exchange="XLON", currency="GBp", tier=1)
    a = nd.Article(headline="h", url="https://news.example/acme", source="Reuters",
                   published="2026-11-17T09:00:00+00:00", bullet='Won a £40m contract <script>alert(1)</script>',
                   heading="Contracts", final_url="https://www.reuters.com/acme")
    a.extra_sources = [{"url": "https://www.ft.com/acme", "source": "FT"},
                       {"url": "javascript:alert(1)", "source": "Bad"}]
    c.articles = [a]
    quiet = nd.Company(name="Quiet Plc", note="No material news in this window.")
    return [c, quiet]


def test_html_email_shows_the_digest_safely(nd):
    cfg = nd.Config()
    page = nd.digest_html(_digest(nd), cfg, [])
    assert "Acme &lt;Widgets&gt; &amp; Co" in page and "<script>" not in page     # everything escaped
    assert "Contracts" in page and "XLON · GBp · Tier 1" in page and "Reuters — 17 Nov 2026" in page
    assert 'href="https://www.reuters.com/acme"' in page and 'href="https://www.ft.com/acme"' in page
    assert "javascript:" not in page                                          # only web links
    assert "Quiet Plc" in page and "1 point across 2 companies" in page
    assert "attached as a PDF and a Word file" in page and "Not investment advice" in page


def test_plain_text_email_is_the_full_digest(nd):
    cfg = nd.Config()
    cfg.email_attach_docx = False
    text = nd.digest_text(_digest(nd), cfg, [])
    assert "ACME <WIDGETS> & CO  (XLON · GBp · Tier 1)" in text and "  Contracts" in text
    assert "Reuters, 17 Nov 2026: https://www.reuters.com/acme  https://www.ft.com/acme" in text
    assert "javascript:" not in text and "attached as a PDF." in text


def test_run_warnings_can_be_left_out_of_client_email(nd):
    cfg = nd.Config()
    warn = ["Finnhub: key rejected"]
    assert "Finnhub: key rejected" in nd.digest_html(_digest(nd), cfg, warn)
    assert "Finnhub: key rejected" in nd.digest_text(_digest(nd), cfg, warn)
    cfg.email_show_warnings = False
    assert "Finnhub" not in nd.digest_html(_digest(nd), cfg, warn)
    assert "Finnhub" not in nd.digest_text(_digest(nd), cfg, warn)


def test_html_part_sits_beside_text_and_attachments(nd, smtp, tmp_path):
    pdf = tmp_path / "d.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    assert nd.send(_mail_cfg(nd), [str(pdf)], "s", "plain body", "<p>html body</p>")
    msg = _parse_mail(smtp.instances[0].sent[0].as_bytes(), policy=_mail_policy.default)
    assert [p.get_content_type() for p in msg.walk()] == [
        "multipart/mixed", "multipart/alternative", "text/plain", "text/html", "application/pdf"]
    assert msg.get_body().get_content().strip() == "<p>html body</p>"         # mail apps prefer the HTML


# --- package / command ------------------------------------------------------------------

def test_package_entry_points_run(tmp_path):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for cmd in ([sys.executable, "-m", "newsdigest", "--help"], [sys.executable, str(ROOT / "news digest.py"), "--help"]):
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env, timeout=120)
        assert out.returncode == 0, out.stderr
        assert "--init-config" in out.stdout and "--email-to" in out.stdout


def test_pyproject_declares_the_newsdigest_command():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'newsdigest = "newsdigest.cli:main"' in text and "pyyaml" in text


def test_home_folder_rules(nd, monkeypatch, tmp_path):
    import importlib
    settings = importlib.import_module("newsdigest.settings")
    monkeypatch.setenv("NEWS_DIGEST_HOME", str(tmp_path))
    assert settings._home() == str(tmp_path)
    monkeypatch.delenv("NEWS_DIGEST_HOME")
    assert settings._home() == str(ROOT)                  # source checkout: the repo folder
