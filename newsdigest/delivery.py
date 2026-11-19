"""Email delivery: send the finished digest (PDF + DOCX) to a list of people.

Configure in config.yaml (smtp_host, smtp_port, smtp_user, email_to, ...) and put
the mail password in .env as SMTP_PASSWORD. Works with any SMTP server: Gmail or
Microsoft 365 (with an app password), SendGrid, Mailgun, your own server.
"""

from __future__ import annotations

import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import settings, state


class EmailError(RuntimeError):
    pass


def _password() -> str:
    return os.environ.get("SMTP_PASSWORD", "").strip().strip("\"'")


def _connect(cfg: settings.Config) -> smtplib.SMTP:
    """Open an authenticated SMTP connection, or raise EmailError."""
    if not cfg.smtp_host:
        raise EmailError("smtp_host is not set (config.yaml)")
    security = (cfg.smtp_security or "starttls").lower()
    if security not in ("starttls", "ssl", "none"):
        raise EmailError("smtp_security must be starttls, ssl or none")
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
            server.ehlo()
            if security == "starttls":
                server.starttls(context=context)
                server.ehlo()
        if cfg.smtp_user:
            pw = _password()
            if not pw:
                server.quit()
                raise EmailError("SMTP_PASSWORD is not set (.env)")
            server.login(cfg.smtp_user, pw)
        return server
    except EmailError:
        raise
    except smtplib.SMTPAuthenticationError:
        raise EmailError("the mail server rejected smtp_user / SMTP_PASSWORD "
                         "(Gmail and Microsoft 365 need an app password)") from None
    except (OSError, smtplib.SMTPException) as e:
        raise EmailError(f"couldn't reach {cfg.smtp_host}:{cfg.smtp_port} ({e.__class__.__name__}: {e})") from None


def build_message(cfg: settings.Config, attachments: list[str], subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    sender = cfg.email_from or cfg.smtp_user
    if not sender:
        raise EmailError("set email_from (or smtp_user) in config.yaml")
    msg["From"] = sender
    msg["To"] = ", ".join(cfg.email_to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg.set_content(body)
    for path in attachments:
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(path))
    return msg


def digest_body(companies: list, window: str, warnings: list[str]) -> str:
    n_items = sum(len(c.articles) for c in companies)
    lines = [f"News digest for {window}.", "",
             f"{n_items} points across {len(companies)} companies:", ""]
    for c in companies:
        lines.append(f"  - {c.name}: {len(c.articles)}")
    if warnings:
        lines += ["", "Run warnings:"] + [f"  ! {w}" for w in warnings]
    lines += ["", "The full digest is attached (PDF and Word).",
              "Summaries are AI-assisted. Verify against the linked sources. Not investment advice."]
    return "\n".join(lines)


def send(cfg: settings.Config, attachments: list[str], subject: str, body: str) -> bool:
    """Send one email to cfg.email_to. Never raises: failures go to the run
    report (which makes the run 'degraded') and are printed."""
    if not cfg.email_to:
        return False
    try:
        msg = build_message(cfg, attachments, subject, body)
        server = _connect(cfg)
        try:
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except (EmailError, OSError, smtplib.SMTPException) as e:
        print(f"   Email not sent: {e}")
        state._REPORT.source("Email", "error", str(e))
        return False
    print(f"   Emailed to {len(cfg.email_to)} recipient(s): {', '.join(cfg.email_to)}")
    state._REPORT.source("Email", "ok")
    return True


def subject_for(cfg: settings.Config, date: str, tiers: str, n_companies: int, n_items: int) -> str:
    try:
        return cfg.email_subject.format(date=date, tiers=tiers, companies=n_companies, items=n_items)
    except (KeyError, IndexError, ValueError):
        return f"News digest {date}"


def check(cfg: settings.Config) -> tuple[str, str]:
    """For --doctor: connect and log in without sending anything."""
    if not cfg.email_to:
        return "--", "off (set email_to in config.yaml to email the digest)"
    try:
        server = _connect(cfg)
        server.quit()
    except EmailError as e:
        return "FAIL", str(e)
    who = f" as {cfg.smtp_user}" if cfg.smtp_user else ""
    return "OK", f"{cfg.smtp_host}:{cfg.smtp_port} login OK{who}; sends to {len(cfg.email_to)} recipient(s)"
