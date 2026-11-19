"""Step 6, publishing: the PDF and DOCX reports."""

from __future__ import annotations

import html
from datetime import datetime, timedelta
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, KeepTogether
import docx
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.shared import Pt, RGBColor, Inches
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

from . import models, settings


# ============================================================================
# STEP 6 — Publish (presentable PDF + DOCX)
# ============================================================================
NAVY = HexColor("#0b3d62")
BLUE = HexColor("#1a73e8")
GREY = HexColor("#5f6368")
DARK = HexColor("#202124")
LINE = HexColor("#e0e0e0")


def _tiers_label(cfg: settings.Config) -> str:
    ts = sorted(set(cfg.tiers))
    if len(ts) == 1:
        return str(ts[0])
    if len(ts) == 2:
        return f"{ts[0]} & {ts[1]}"
    return ", ".join(str(t) for t in ts[:-1]) + f" & {ts[-1]}"


def _tiers_fname(cfg: settings.Config) -> str:
    return "-".join(str(t) for t in sorted(set(cfg.tiers)))


def _window_label(cfg: settings.Config) -> str:
    end = datetime.now()
    start = end - timedelta(days=cfg.days_back)
    return f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"


def _fmt_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d %b %Y")
    except Exception:
        return iso[:10]


def build_pdf(companies: list[models.Company], cfg: settings.Config, path: str) -> str:
    styles = getSampleStyleSheet()

    def S(name, **kw):
        return ParagraphStyle(name, parent=styles["Normal"], **kw)

    title = ParagraphStyle("title", parent=styles["Title"], fontSize=22, textColor=NAVY,
                           spaceAfter=2, alignment=0)
    sub = S("sub", fontSize=9.5, textColor=GREY, spaceAfter=14)
    co_style = ParagraphStyle("co", parent=styles["Heading2"], fontSize=14, textColor=NAVY,
                              spaceBefore=14, spaceAfter=1)
    meta = S("meta", fontSize=8, textColor=GREY, spaceAfter=6)
    subhead = ParagraphStyle("subhead", parent=styles["Normal"], fontName="Helvetica-Bold",
                             fontSize=10.5, textColor=NAVY, spaceBefore=7, spaceAfter=1)
    bullet = S("bullet", fontSize=10.5, textColor=DARK, leading=15,
               leftIndent=16, firstLineIndent=-8, spaceBefore=2)
    link_s = S("link", fontSize=8, textColor=GREY, leading=12, spaceAfter=4, leftIndent=16)
    note = S("note", fontSize=10, textColor=GREY, leading=14, spaceAfter=4)

    def esc(s):
        return html.escape(s or "")

    doc = SimpleDocTemplate(path, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            title=f"Tier {_tiers_label(cfg)} Weekly News Digest")
    flow = [
        Paragraph(f"Tier {_tiers_label(cfg)} Weekly News Digest", title),
        Paragraph(f"Week of {_window_label(cfg)}&nbsp;&nbsp;|&nbsp;&nbsp;"
                  f"Generated {datetime.now().strftime('%A, %d %B %Y')}&nbsp;&nbsp;|&nbsp;&nbsp;"
                  f"Sources: Google News, trusted outlets, official market feeds", sub),
        HRFlowable(width="100%", thickness=1.2, color=NAVY, spaceAfter=4),
    ]

    for c in companies:
        bits = " · ".join(b for b in [c.exchange, c.currency, (f"Tier {c.tier}" if c.tier else "")] if b)
        header = [Paragraph(esc(c.name), co_style)]
        if bits:
            header.append(Paragraph(esc(bits), meta))
        units = []
        if not c.articles:
            units.append([Paragraph(c.note or "No material news in this window.", note)])
        else:
            for a in c.articles:
                unit = []
                if a.heading:
                    unit.append(Paragraph(esc(a.heading), subhead))
                link = a.final_url or a.url
                tail = " — ".join(x for x in [esc(a.source), _fmt_date(a.published)] if x)
                unit.append(Paragraph(
                    f'<font color="#1a73e8">•</font>&nbsp;&nbsp;{esc(a.bullet)}', bullet))
                sub_bits = []
                if tail:
                    sub_bits.append(tail)
                if link:
                    sub_bits.append(f'<link href="{esc(link)}"><font color="#1a73e8">Read full article »</font></link>')
                for ex in a.extra_sources:
                    exl, exs = ex.get("url"), ex.get("source") or "source"
                    if exl:
                        sub_bits.append(f'<link href="{esc(exl)}"><font color="#1a73e8">{esc(exs)} »</font></link>')
                if sub_bits:
                    unit.append(Paragraph(" &nbsp;·&nbsp; ".join(sub_bits), link_s))
                units.append(unit)
        if units:
            units[0] = header + units[0]
        else:
            units = [header]
        for unit in units:
            flow.append(KeepTogether(unit))
        flow.append(Spacer(1, 4))
        flow.append(HRFlowable(width="100%", thickness=0.5, color=LINE))

    flow.append(Spacer(1, 8))
    flow.append(Paragraph("Generated automatically · summaries are AI-assisted and should be "
                          "verified against the linked source before any decision.",
                          S("foot", fontSize=7.5, textColor=GREY)))
    doc.build(flow)
    return path


def _w_el(tag: str, val: str | None = None):
    """Create a WordprocessingML element, optionally with a w:val attribute."""
    el = OxmlElement(tag)
    if val is not None:
        el.set(qn("w:val"), val)
    return el


def _docx_hyperlink(paragraph, url, text, color="1a73e8"):
    """Insert a real clickable hyperlink run into a python-docx paragraph."""
    part = paragraph.part
    r_id = part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    for el in (_w_el("w:color", color), _w_el("w:u", "single"), _w_el("w:sz", "16")):
        rPr.append(el)
    new_run.append(rPr)
    t = OxmlElement("w:t")
    t.text = text
    new_run.append(t)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)
    return hyperlink


_DOCX_GREY = RGBColor(0x5f, 0x63, 0x68)
_DOCX_NAVY = RGBColor(0x0b, 0x3d, 0x62)


def _style_run(run, size: float, color=None, bold: bool = False, italic: bool = False):
    run.font.size = Pt(size)
    if color is not None:
        run.font.color.rgb = color
    if bold:
        run.bold = True
    if italic:
        run.italic = True
    return run


def build_docx(companies: list[models.Company], cfg: settings.Config, path: str) -> str:
    d = docx.Document()
    d.add_heading(f"Tier {_tiers_label(cfg)} Weekly News Digest", level=0)
    sub = d.add_paragraph()
    _style_run(sub.add_run(f"Week of {_window_label(cfg)}   |   Generated "
                           f"{datetime.now().strftime('%A, %d %B %Y')}   |   "
                           f"Google News · trusted outlets · official market feeds"),
               9, _DOCX_GREY, italic=True)

    for c in companies:
        ch = d.add_heading(c.name, level=1)
        ch.paragraph_format.keep_with_next = True
        bits = " · ".join(b for b in [c.exchange, c.currency, (f"Tier {c.tier}" if c.tier else "")] if b)
        if bits:
            m = d.add_paragraph()
            _style_run(m.add_run(bits), 8.5, _DOCX_GREY)
            m.paragraph_format.keep_with_next = True
        if not c.articles:
            p = d.add_paragraph(c.note or "No material news in this window.")
            p.runs[0].italic = True
            p.paragraph_format.keep_together = True
            continue
        for a in c.articles:
            if a.heading:
                hp = d.add_paragraph()
                _style_run(hp.add_run(a.heading), 10.5, _DOCX_NAVY, bold=True)
                hp.paragraph_format.space_before = Pt(6)
                hp.paragraph_format.space_after = Pt(0)
                hp.paragraph_format.keep_with_next = True
                hp.paragraph_format.keep_together = True
            p = d.add_paragraph(style="List Bullet")
            p.add_run(a.bullet)
            p.paragraph_format.keep_together = True
            p.paragraph_format.keep_with_next = True
            sub = d.add_paragraph()
            sub.paragraph_format.left_indent = Inches(0.3)
            sub.paragraph_format.keep_together = True
            tail = " · ".join(x for x in [a.source, _fmt_date(a.published)] if x)
            if tail:
                _style_run(sub.add_run(tail + "  "), 8, _DOCX_GREY)
            link = a.final_url or a.url
            if link:
                _docx_hyperlink(sub, link, "Read full article »")
            for ex in a.extra_sources:
                exl = ex.get("url")
                if exl:
                    _style_run(sub.add_run("  ·  "), 8, _DOCX_GREY)
                    _docx_hyperlink(sub, exl, f"{ex.get('source') or 'source'} »")

    foot = d.add_paragraph()
    fr = foot.add_run("Generated automatically · summaries are AI-assisted and should be "
                      "verified against the linked source before any decision.")
    fr.italic = True
    fr.font.size = Pt(7.5)
    fr.font.color.rgb = RGBColor(0x5f, 0x63, 0x68)
    d.save(path)
    return path
