"""Email delivery: send the finished digest (PDF + DOCX) to a list of people.

Configure in config.yaml (smtp_host, smtp_port, smtp_user, email_to, ...) and put
the mail password in .env as SMTP_PASSWORD. Works with any SMTP server: Gmail or
Microsoft 365 (with an app password), SendGrid, Mailgun, your own server.
"""

from __future__ import annotations

import html
import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import models, publishing, settings, state


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


def build_message(cfg: settings.Config, attachments: list[str], subject: str, body: str,
                  html_body: str = "") -> EmailMessage:
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
    if html_body:
        msg.add_alternative(html_body, subtype="html")   # mail apps show this; the text is the fallback
    for path in attachments:
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(path))
    return msg


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"{n} {word}"
    return f"{n} {word[:-1]}ies" if word.endswith("y") else f"{n} {word}s"


def _safe_url(url: str) -> str:
    """Only web links go into the email (a feed could carry a javascript: URL)."""
    url = (url or "").strip()
    return url if url.lower().startswith(("https://", "http://")) else ""


def _meta(c: models.Company) -> str:
    return " · ".join(b for b in [c.exchange, c.currency, (f"Tier {c.tier}" if c.tier else "")] if b)


def _sources(a: models.Article) -> list[tuple[str, str]]:
    """[(label, url)] for the main story and any merged duplicates."""
    out = []
    main = _safe_url(a.final_url or a.url)
    if main:
        out.append(("Read full article", main))
    for ex in a.extra_sources:
        u = _safe_url(ex.get("url", ""))
        if u:
            out.append((ex.get("source") or "source", u))
    return out


def _attached_note(cfg: settings.Config) -> str:
    return "Also attached as a PDF" + (" and a Word file." if cfg.email_attach_docx else ".")


DISCLAIMER = ("Generated automatically. Summaries are AI-assisted: verify against the linked "
              "source before any decision. Not investment advice.")


def _headline_counts(companies: list) -> tuple[str, str]:
    n_items = sum(len(c.articles) for c in companies)
    return (f"{_plural(n_items, 'point')} across {_plural(len(companies), 'company')}",
            ", ".join(f"{c.name} ({len(c.articles)})" for c in companies))


def digest_text(companies: list, cfg: settings.Config, warnings: list[str]) -> str:
    """The whole digest as plain text: the fallback for mail apps that don't show HTML."""
    summary, _ = _headline_counts(companies)
    lines = [f"Tier {publishing._tiers_label(cfg)} Weekly News Digest",
             f"Week of {publishing._window_label(cfg)} · {summary}", ""]
    for c in companies:
        meta = _meta(c)
        lines.append(c.name.upper() + (f"  ({meta})" if meta else ""))
        if not c.articles:
            lines.append(f"  {c.note or 'No material news in this window.'}")
        for a in c.articles:
            if a.heading:
                lines.append(f"  {a.heading}")
            lines.append(f"  • {a.bullet}")
            tail = ", ".join(x for x in [a.source, publishing._fmt_date(a.published)] if x)
            links = "  ".join(u for _, u in _sources(a))
            if tail or links:
                lines.append("    " + ": ".join(x for x in [tail, links] if x))
        lines.append("")
    if warnings and cfg.email_show_warnings:
        lines += ["Run warnings:"] + [f"  ! {w}" for w in warnings] + [""]
    lines += [_attached_note(cfg), DISCLAIMER]
    return "\n".join(lines)


# Colours match the PDF. Inline styles and tables, because that is what Gmail,
# Outlook and Apple Mail all render the same way.
_NAVY, _BLUE, _GREY, _DARK, _LINE, _BG = "#0b3d62", "#1a73e8", "#5f6368", "#202124", "#e0e0e0", "#f1f3f4"
_TABLE = 'role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"'


def _company_html(c: models.Company) -> str:
    e = html.escape
    parts = [f'<div style="font-size:18px;font-weight:bold;color:{_NAVY}">{e(c.name)}</div>']
    meta = _meta(c)
    if meta:
        parts.append(f'<div style="font-size:12px;color:{_GREY};padding-top:2px">{e(meta)}</div>')
    if not c.articles:
        parts.append(f'<div style="font-size:14px;color:{_GREY};padding:10px 0 4px">'
                     f'{e(c.note or "No material news in this window.")}</div>')
    for a in c.articles:
        if a.heading:
            parts.append(f'<div style="font-size:14px;font-weight:bold;color:{_NAVY};padding:14px 0 0">'
                         f'{e(a.heading)}</div>')
        sub = " — ".join(e(x) for x in [a.source, publishing._fmt_date(a.published)] if x)
        sub = [sub] if sub else []
        sub += [f'<a href="{e(u)}" style="color:{_BLUE};text-decoration:none">{e(label)}&nbsp;»</a>'
                for label, u in _sources(a)]
        line = (f'<div style="font-size:12px;line-height:18px;color:{_GREY};padding-top:2px">'
                + " &nbsp;·&nbsp; ".join(sub) + "</div>") if sub else ""
        parts.append(
            f'<table {_TABLE} style="padding-top:6px"><tr>'
            f'<td valign="top" width="16" style="color:{_BLUE};font-size:15px;line-height:22px">•</td>'
            f'<td style="font-size:15px;line-height:22px;color:{_DARK}">{e(a.bullet)}{line}</td>'
            "</tr></table>")
    return f'<tr><td style="padding:20px 28px 18px">{"".join(parts)}</td></tr>'


def digest_html(companies: list, cfg: settings.Config, warnings: list[str]) -> str:
    """The whole digest as an HTML email: same layout as the PDF, readable on a phone."""
    e = html.escape
    title = f"Tier {publishing._tiers_label(cfg)} Weekly News Digest"
    summary, per_company = _headline_counts(companies)
    divider = (f'<tr><td style="padding:0 28px"><div style="border-top:1px solid {_LINE};'
               'font-size:0;line-height:0">&nbsp;</div></td></tr>')
    foot = []
    if warnings and cfg.email_show_warnings:
        foot.append('<div style="font-size:12px;color:#b06000;padding-bottom:8px"><b>Run warnings:</b><br>'
                    + "<br>".join(e(w) for w in warnings) + "</div>")
    foot.append(f'<div style="font-size:12px;color:{_GREY};line-height:18px">'
                f'{e(_attached_note(cfg))}<br>{e(DISCLAIMER)}</div>')
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{e(title)}</title></head>'
        f'<body style="margin:0;padding:0;background:{_BG}">'
        # Inbox preview line, hidden in the message itself.
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{e(summary)}: {e(per_company)}</div>'
        f'<table {_TABLE} style="background:{_BG}"><tr><td align="center" style="padding:24px 10px">'
        f'<table {_TABLE} style="max-width:640px;background:#ffffff;border-radius:8px;'
        f'font-family:Arial,Helvetica,sans-serif;color:{_DARK}">'
        f'<tr><td style="padding:28px 28px 16px;border-bottom:3px solid {_NAVY}">'
        f'<div style="font-size:22px;font-weight:bold;color:{_NAVY}">{e(title)}</div>'
        f'<div style="font-size:13px;color:{_GREY};padding-top:6px">'
        f'Week of {e(publishing._window_label(cfg))} &nbsp;·&nbsp; {e(summary)}</div></td></tr>'
        + divider.join(_company_html(c) for c in companies)
        + f'<tr><td style="padding:16px 28px 24px;background:#fafafa;border-top:1px solid {_LINE};'
          f'border-radius:0 0 8px 8px">{"".join(foot)}</td></tr>'
        "</table></td></tr></table></body></html>")


def send(cfg: settings.Config, attachments: list[str], subject: str, body: str, html_body: str = "") -> bool:
    """Send one email to cfg.email_to. Never raises: failures go to the run
    report (which makes the run 'degraded') and are printed."""
    if not cfg.email_to:
        return False
    try:
        msg = build_message(cfg, attachments, subject, body, html_body)
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
