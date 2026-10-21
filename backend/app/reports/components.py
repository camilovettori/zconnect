"""Reusable, branded PDF building blocks (ReportLab). Nothing here is specific to one report or
one customer: a report assembles these components from its own data model."""
from __future__ import annotations

import io
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

LOGO_PATH = Path(__file__).resolve().parent / "assets" / "zconnect-mark.png"


class Theme:
    """Zconnect / Ziffera brand colours (same values as the web app's design system)."""

    brand = colors.HexColor("#1f6aeb")
    brand_dark = colors.HexColor("#1854d8")
    cyan = colors.HexColor("#3fcff0")
    ink = colors.HexColor("#0f172a")
    text = colors.HexColor("#334155")
    muted = colors.HexColor("#64748b")
    faint = colors.HexColor("#94a3b8")
    line = colors.HexColor("#e2e8f0")
    surface = colors.HexColor("#f8fafc")
    brand_tint = colors.HexColor("#eef6ff")
    warning_bg = colors.HexColor("#fffbeb")
    warning_line = colors.HexColor("#fde68a")
    warning_ink = colors.HexColor("#92400e")
    success = colors.HexColor("#059669")
    danger = colors.HexColor("#e11d48")


PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 16 * mm
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN
FRAME_PADDING = 6

STYLES = {
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=19, leading=23, textColor=Theme.ink),
    "subtitle": ParagraphStyle("subtitle", fontName="Helvetica", fontSize=10.5, leading=14, textColor=Theme.muted),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=Theme.ink, spaceBefore=4, spaceAfter=6, keepWithNext=1),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9, leading=12.5, textColor=Theme.text),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=7.8, leading=10.5, textColor=Theme.muted),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.6, leading=11, textColor=Theme.text),
    "kpi_label": ParagraphStyle("kpi_label", fontName="Helvetica-Bold", fontSize=7.4, leading=9.5, textColor=Theme.muted),
    "kpi_value": ParagraphStyle("kpi_value", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=Theme.ink),
    "kpi_caption": ParagraphStyle("kpi_caption", fontName="Helvetica", fontSize=7.4, leading=9.5, textColor=Theme.muted),
}


def text(value: object) -> str:
    """Escape user data (customer and product names) for ReportLab's mini-markup."""
    return escape(str(value))


@dataclass
class ReportHeader:
    title: str
    company_name: str
    period_text: str
    generated_text: str
    footer_note: str


class BrandedReport:
    """A4 report with the brand bar, logo, company, period and generation time on every page."""

    def __init__(self, header: ReportHeader, *, compress: bool = True, author: str = "Zconnect by Ziffera"):
        self.header = header
        self.compress = compress
        self.author = author

    def _decorate(self, canvas, doc):
        h = self.header
        canvas.saveState()
        # Brand bar
        canvas.setFillColor(Theme.brand)
        canvas.rect(0, PAGE_HEIGHT - 5, PAGE_WIDTH * 0.7, 5, stroke=0, fill=1)
        canvas.setFillColor(Theme.cyan)
        canvas.rect(PAGE_WIDTH * 0.7, PAGE_HEIGHT - 5, PAGE_WIDTH * 0.3, 5, stroke=0, fill=1)
        # Header: logo, product, company / period
        top = PAGE_HEIGHT - 13 * mm
        if LOGO_PATH.exists():
            canvas.drawImage(str(LOGO_PATH), MARGIN, top - 4 * mm, width=9 * mm, height=9 * mm, mask="auto")
        canvas.setFillColor(Theme.ink)
        canvas.setFont("Helvetica-Bold", 10.5)
        canvas.drawString(MARGIN + 11.5 * mm, top + 1.3 * mm, "Zconnect")
        canvas.setFillColor(Theme.muted)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(MARGIN + 11.5 * mm, top - 2.4 * mm, "Invoice automation · Unify to Zoho")
        canvas.setFillColor(Theme.ink)
        canvas.setFont("Helvetica-Bold", 9.5)
        canvas.drawRightString(PAGE_WIDTH - MARGIN, top + 1.3 * mm, h.company_name)
        canvas.setFillColor(Theme.muted)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawRightString(PAGE_WIDTH - MARGIN, top - 2.4 * mm, h.period_text)
        canvas.setStrokeColor(Theme.line)
        canvas.setLineWidth(0.6)
        canvas.line(MARGIN, top - 6.5 * mm, PAGE_WIDTH - MARGIN, top - 6.5 * mm)
        # Footer
        canvas.line(MARGIN, 14 * mm, PAGE_WIDTH - MARGIN, 14 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(Theme.muted)
        canvas.drawString(MARGIN, 10 * mm, h.footer_note)
        canvas.drawRightString(PAGE_WIDTH - MARGIN, 10 * mm, f"Generated {h.generated_text} · Page {doc.page}")
        canvas.restoreState()

    def build(self, flowables: List) -> bytes:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            # The frame adds 6pt padding on each side; offset it so content spans exactly CONTENT_WIDTH.
            leftMargin=MARGIN - FRAME_PADDING,
            rightMargin=MARGIN - FRAME_PADDING,
            topMargin=27 * mm,
            bottomMargin=20 * mm,
            title=f"{self.header.title} — {self.header.company_name}",
            author=self.author,
            subject=self.header.period_text,
            creator="Zconnect",
            pageCompression=1 if self.compress else 0,
        )
        doc.build(flowables, onFirstPage=self._decorate, onLaterPages=self._decorate)
        return buffer.getvalue()


def title_block(title: str, subtitle: str) -> List:
    return [Paragraph(text(title), STYLES["title"]), Spacer(1, 2), Paragraph(text(subtitle), STYLES["subtitle"]), Spacer(1, 10)]


def section(title: str) -> List:
    return [Spacer(1, 8), Paragraph(text(title), STYLES["h2"])]


def notice(message: str, tone: str = "info") -> Table:
    background, border, ink = (
        (Theme.warning_bg, Theme.warning_line, Theme.warning_ink) if tone == "warning" else (Theme.brand_tint, colors.HexColor("#bcdcff"), colors.HexColor("#1b3d8a"))
    )
    style = ParagraphStyle("notice", parent=STYLES["body"], textColor=ink, fontSize=8.4, leading=11.5)
    table = Table([[Paragraph(message, style)]], colWidths=[CONTENT_WIDTH])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), background),
                ("BOX", (0, 0), (-1, -1), 0.6, border),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return table


@dataclass
class Kpi:
    label: str
    value: str
    caption: str = ""


def kpi_grid(items: Sequence[Kpi], columns: int = 3) -> Table:
    cells = [
        [Paragraph(text(item.label).upper(), STYLES["kpi_label"]), Paragraph(text(item.value), STYLES["kpi_value"]), Paragraph(text(item.caption), STYLES["kpi_caption"])]
        for item in items
    ]
    rows = [cells[i : i + columns] for i in range(0, len(cells), columns)]
    if rows and len(rows[-1]) < columns:
        rows[-1] += [""] * (columns - len(rows[-1]))
    gap = 6
    width = (CONTENT_WIDTH - gap * (columns - 1)) / columns
    grid = Table(
        [[_kpi_card(cell, width) if cell else "" for cell in row] for row in rows],
        colWidths=[width + (gap if i < columns - 1 else 0) for i in range(columns)],
    )
    grid.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), gap), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), gap), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return grid


def _kpi_card(parts: List, width: float) -> Table:
    card = Table([[parts[0]], [parts[1]], [parts[2]]], colWidths=[width])
    card.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.6, Theme.line),
                ("LINEABOVE", (0, 0), (-1, 0), 2, Theme.brand),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (0, 0), 7),
                ("TOPPADDING", (0, 1), (-1, -1), 1),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
            ]
        )
    )
    return card


def data_table(headers: Sequence[str], rows: Sequence[Sequence[str]], widths: Sequence[float], numeric_columns: Sequence[int] = (), empty_text: str = "No data for this period.") -> Table:
    if not rows:
        return notice(text(empty_text))
    header_style = ParagraphStyle("th", parent=STYLES["cell"], fontName="Helvetica-Bold", textColor=Theme.muted, fontSize=7.6)
    data = [[Paragraph(text(h), header_style) for h in headers]] + [[Paragraph(text(value), STYLES["cell"]) for value in row] for row in rows]
    table = Table(data, colWidths=[CONTENT_WIDTH * w for w in widths], repeatRows=1)
    style = [
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, Theme.line),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, Theme.line),
        ("BACKGROUND", (0, 0), (-1, 0), Theme.surface),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    for column in numeric_columns:
        style.append(("ALIGN", (column, 0), (column, -1), "RIGHT"))
    table.setStyle(TableStyle(style))
    for column in numeric_columns:  # right-align the numeric Paragraphs too
        for row in data:
            row[column].style = ParagraphStyle("num", parent=row[column].style, alignment=2)
    return table


def bar_chart(points: Sequence[Tuple[str, Optional[Decimal]]], value_label: Callable[[Decimal], str], height: float = 46 * mm) -> Drawing:
    """Bars per period. A None value means "not recorded" and is drawn as an empty dashed bar,
    never as zero."""
    drawing = Drawing(CONTENT_WIDTH, height)
    left, bottom, top_pad = 2, 14, 12
    plot_height = height - bottom - top_pad
    known = [value for _label, value in points if value is not None]
    peak = max(known) if known and max(known) > 0 else Decimal("1")
    slot = (CONTENT_WIDTH - left) / max(len(points), 1)
    bar_width = min(slot * 0.62, 26)
    drawing.add(Line(left, bottom, CONTENT_WIDTH, bottom, strokeColor=Theme.line, strokeWidth=0.8))
    show_every = max(1, len(points) // 12)
    for index, (label, value) in enumerate(points):
        x = left + slot * index + (slot - bar_width) / 2
        if value is None:
            drawing.add(Rect(x, bottom, bar_width, plot_height * 0.35, fillColor=None, strokeColor=Theme.faint, strokeWidth=0.6, strokeDashArray=[2, 2]))
            drawing.add(String(x + bar_width / 2, bottom + plot_height * 0.35 + 3, "n/r", fontName="Helvetica", fontSize=6, fillColor=Theme.faint, textAnchor="middle"))
        else:
            bar_height = float(value / peak) * plot_height if value > 0 else 0
            drawing.add(Rect(x, bottom, bar_width, max(bar_height, 0.6), fillColor=Theme.brand, strokeColor=None))
            if len(points) <= 14 and value > 0:
                drawing.add(String(x + bar_width / 2, bottom + bar_height + 3, value_label(value), fontName="Helvetica", fontSize=6, fillColor=Theme.muted, textAnchor="middle"))
        if index % show_every == 0:
            drawing.add(String(x + bar_width / 2, 3, label, fontName="Helvetica", fontSize=6.5, fillColor=Theme.muted, textAnchor="middle"))
    return drawing


def logo_image(size_mm: float = 14) -> Optional[Image]:
    return Image(str(LOGO_PATH), width=size_mm * mm, height=size_mm * mm) if LOGO_PATH.exists() else None
