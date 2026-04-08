"""
Generate a Form 1399י (טופס 1399י) data PDF for Israeli capital gains tax.

Form 1399 ("Notice of Asset Sale and Tax Calculation") is required under
Section 91 of the Israeli Income Tax Ordinance for every capital gain disposal.
For virtual assets (נכסים וירטואליים) the applicable transaction code is 77
and the gain is reported on Row 19 (Code 09).

Simplifications that apply to all StarkNet assets (acquired after 1.1.2012):
  - Time-period ratios: Ratio 1 = 0, Ratio 2 = 0, Ratio 3 = 1
    → entire gain is post-change-date (Row 27, Code 64)
  - Inflationary amount (Row 17): set to 0 — see disclaimer
  - No depreciation, no enhancement expenses, no replacement asset offsets

This file is generated programmatically and is intended as supporting
documentation to be attached to Form 1301. Verify all figures with a
licensed Israeli CPA (רואה חשבון מוסמך) before filing.

ITA reference: Circular 05/2018 and FAQ on digital assets at gov.il.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .classifier import EventType
from .config import ISRAEL_CGT_RATE
from .tax import ProcessedEvent, TaxSummary


def _bidi(text: str) -> str:
    """
    Apply Unicode BiDi reordering so Hebrew renders correctly in ReportLab
    (which treats all text as left-to-right).
    Falls back to identity if python-bidi is not installed.
    """
    try:
        from bidi.algorithm import get_display  # type: ignore
        return get_display(text)
    except ImportError:
        return text


# ── Font registration ─────────────────────────────────────────────────────────

_DEJAVU_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_DEJAVU_BOLD    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONTS_REGISTERED = False


def _register_fonts() -> str:
    """Register DejaVu (Hebrew-capable) if available; fall back to Helvetica."""
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return "DejaVu" if os.path.exists(_DEJAVU_REGULAR) else "Helvetica"

    if os.path.exists(_DEJAVU_REGULAR) and os.path.exists(_DEJAVU_BOLD):
        pdfmetrics.registerFont(TTFont("DejaVu",  _DEJAVU_REGULAR))
        pdfmetrics.registerFont(TTFont("DejaVuB", _DEJAVU_BOLD))
        _FONTS_REGISTERED = True
        return "DejaVu"

    _FONTS_REGISTERED = True
    return "Helvetica"


# ── Colours ───────────────────────────────────────────────────────────────────

_BLUE_DARK  = colors.HexColor("#1a3c5e")   # header background
_BLUE_MID   = colors.HexColor("#2e6da4")   # sub-header / accent
_BLUE_LIGHT = colors.HexColor("#d6e8f7")   # alternating row tint
_GREEN      = colors.HexColor("#1a6b3c")   # gain
_RED        = colors.HexColor("#9b1c1c")   # loss
_GREY_LINE  = colors.HexColor("#b0b8c1")
_WHITE      = colors.white
_BLACK      = colors.black


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ils(value: Decimal) -> str:
    """Format a Decimal as NIS with thousands separator."""
    v = float(value)
    sign = "-" if v < 0 else ""
    return f"{sign}₪{abs(v):,.2f}"


def _date(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y")


def _disposal_events(events: list[ProcessedEvent]) -> list[ProcessedEvent]:
    """Return only events that carry capital gain/loss disposals."""
    disposal_types = {
        EventType.SWAP,
        EventType.LIQUID_STAKE,
        EventType.LIQUID_UNSTAKE,
        EventType.SEND,
    }
    return [pe for pe in events if pe.event.event_type in disposal_types and pe.disposals]


# ── Styles ────────────────────────────────────────────────────────────────────

def _make_styles(font: str) -> dict:
    bold = font + "B" if font == "DejaVu" else font + "-Bold"
    return {
        "cover_title": ParagraphStyle(
            "cover_title", fontName=bold, fontSize=18,
            textColor=_BLUE_DARK, spaceAfter=4,
        ),
        "cover_sub": ParagraphStyle(
            "cover_sub", fontName=font, fontSize=11,
            textColor=_BLUE_MID, spaceAfter=2,
        ),
        "cover_note": ParagraphStyle(
            "cover_note", fontName=font, fontSize=8,
            textColor=colors.HexColor("#555555"), spaceAfter=2,
        ),
        "section_head": ParagraphStyle(
            "section_head", fontName=bold, fontSize=13,
            textColor=_WHITE, spaceAfter=0, spaceBefore=0,
        ),
        "label": ParagraphStyle(
            "label", fontName=font, fontSize=9, textColor=_BLACK,
        ),
        "label_bold": ParagraphStyle(
            "label_bold", fontName=bold, fontSize=9, textColor=_BLACK,
        ),
        "note": ParagraphStyle(
            "note", fontName=font, fontSize=7.5,
            textColor=colors.HexColor("#555555"), spaceAfter=2, leading=10,
        ),
        "disclaimer": ParagraphStyle(
            "disclaimer", fontName=font, fontSize=7,
            textColor=colors.HexColor("#777777"), leading=9,
        ),
    }


# ── Form 1399 fields ──────────────────────────────────────────────────────────

# Each entry: (row_number, code, english_label, hebrew_label)
# Only the rows relevant for virtual assets acquired post-2012 are included.
_FORM_ROWS = [
    # Row / Code / English label / Hebrew label (official form 1399י 2024)
    ("1",  "15", "Sale Consideration",                       _bidi("תמורה")),
    ("2",  "20", "Acquisition Cost",                         _bidi("עלות")),
    ("9",  "—",  "Remaining Original Price",                 _bidi("יתרת מחיר מקורי")),
    ("10", "55", "Sale Expenses (gas fees)",                 _bidi("הוצאות הקשורות במכירה")),
    ("16", "—",  "Inflationary Amount (= 0 post-2012) *",   _bidi("סכום אינפלציוני *")),
    ("17", "—",  "Capital Gain / (Loss)",                   _bidi("רווח / הפסד הון")),
    ("18", "71", "Virtual Currency Realization",             _bidi("מימוש / המרה מטבע וירטואלי")),
    ("25", "68", "Post-Change-Date Real Gain (≥2012)",      _bidi("יתרת רווח הון ריאלי לאחר מועד השינוי")),
    ("31", "—",  "Tax at 25%",                              _bidi("מס 25%")),
]


def _build_event_section(
    pe: ProcessedEvent,
    styles: dict,
    font: str,
) -> list:
    """Return a list of flowables for a single disposal event."""
    bold = font + "B" if font == "DejaVu" else font + "-Bold"
    evt = pe.event
    flowables = []

    # ── Section header ────────────────────────────────────────────────────────
    symbols_out = " + ".join(f.symbol for f in evt.tokens_out)
    symbols_in  = " + ".join(f.symbol for f in evt.tokens_in) if evt.tokens_in else "—"
    header_text = (
        f"Form 1399י  |  {evt.event_type.value}  |  "
        f"{symbols_out}"
        + (f" → {symbols_in}" if evt.tokens_in else "")
        + f"  |  {_date(evt.timestamp)}"
    )
    header_table = Table(
        [[Paragraph(header_text, styles["section_head"])]],
        colWidths=[17 * cm],
    )
    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _BLUE_DARK),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
    ]))
    flowables.append(header_table)
    flowables.append(Spacer(1, 3 * mm))

    # ── Meta info ─────────────────────────────────────────────────────────────
    meta_rows = [
        ["TX Hash", evt.tx_hash],
        ["Asset Type", _bidi("Virtual Currency / מטבע וירטואלי")],
        [_bidi("Transaction Code (סמל עסקה)"), "77"],
        [_bidi("Sale Date (תאריך מכירה)"), _date(evt.timestamp)],
    ]
    # Earliest acquisition date across all lots
    all_lots = [lot for d in pe.disposals for lot, _, _c in d.lots_used]
    if all_lots:
        acq_dates = sorted({lot.acquired_at.date() for lot in all_lots})
        acq_str = acq_dates[0].strftime("%d/%m/%Y")
        if len(acq_dates) > 1:
            acq_str += f" – {acq_dates[-1].strftime('%d/%m/%Y')}"
        meta_rows.append([_bidi("Acquisition Date(s) (תאריך רכישה)"), acq_str])

    meta_table = Table(
        [[Paragraph(k, styles["label"]), Paragraph(v, styles["label_bold"])]
         for k, v in meta_rows],
        colWidths=[7 * cm, 10 * cm],
    )
    meta_table.setStyle(TableStyle([
        ("FONTNAME",      (0, 0), (-1, -1), font),
        ("FONTSIZE",      (0, 0), (-1, -1), 8.5),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("GRID",          (0, 0), (-1, -1), 0.3, _GREY_LINE),
        ("BACKGROUND",    (0, 0), (0, -1), colors.HexColor("#f0f4f8")),
    ]))
    flowables.append(meta_table)
    flowables.append(Spacer(1, 4 * mm))

    # ── Form 1399 calculation rows ────────────────────────────────────────────
    proceeds     = sum(d.proceeds_ils   for d in pe.disposals)
    cost_basis   = sum(d.cost_basis_ils for d in pe.disposals)
    gain_loss    = pe.net_gain_loss_ils
    inflationary = Decimal(0)     # see disclaimer
    real_gain    = gain_loss - inflationary
    fee_ils      = Decimal(0)
    if evt.price_ils_out.get(evt.fee_token):
        fee_ils = (evt.fee_amount * evt.price_ils_out[evt.fee_token]).quantize(Decimal("0.01"))
    elif evt.price_ils_in.get(evt.fee_token):
        fee_ils = (evt.fee_amount * evt.price_ils_in[evt.fee_token]).quantize(Decimal("0.01"))
    tax_25 = max(Decimal(0), real_gain * Decimal(str(ISRAEL_CGT_RATE))).quantize(Decimal("0.01"))

    values = {
        "1":  proceeds,
        "2":  cost_basis,
        "9":  cost_basis,       # no depreciation / enhancements for crypto
        "10": fee_ils,
        "16": inflationary,     # = 0 for post-2012 crypto
        "17": gain_loss,        # Row 17 = capital gain/loss (Row 1 − Row 9 − Row 10)
        "18": real_gain,        # Row 18 Code 71 = virtual currency gain (= Row 17 for crypto)
        "25": real_gain,        # Row 25 Code 68 = post-2012 portion = 100% for crypto
        "31": tax_25,
    }

    calc_header = ["Row", "Code", "Field", "Amount (₪)"]
    calc_data   = [calc_header]
    for row_num, code, eng_label, heb_label in _FORM_ROWS:
        val = values[row_num]
        val_str = _ils(val)
        is_gain  = (row_num in ("16", "18", "19", "27")) and val > 0
        is_loss  = (row_num in ("16", "18", "19", "27")) and val < 0
        calc_data.append([
            row_num,
            code,
            f"{eng_label} / {heb_label}",
            val_str,
        ])

    col_w = [1.2 * cm, 1.2 * cm, 10.8 * cm, 3.8 * cm]
    calc_table = Table(calc_data, colWidths=col_w)

    tbl_style = [
        ("FONTNAME",      (0, 0), (-1, -1), font),
        ("FONTSIZE",      (0, 0), (-1, -1), 8.5),
        ("FONTNAME",      (0, 0), (-1, 0), bold),
        ("BACKGROUND",    (0, 0), (-1, 0), _BLUE_MID),
        ("TEXTCOLOR",     (0, 0), (-1, 0), _WHITE),
        ("GRID",          (0, 0), (-1, -1), 0.3, _GREY_LINE),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("ALIGN",         (3, 0), (3, -1), "RIGHT"),   # amounts right-aligned
        ("ALIGN",         (0, 0), (1, -1), "CENTER"),
    ]
    # Tint alternating rows
    for i in range(1, len(calc_data)):
        if i % 2 == 0:
            tbl_style.append(("BACKGROUND", (0, i), (-1, i), _BLUE_LIGHT))

    # Colour the gain/loss rows
    for i, (row_num, *_) in enumerate(_FORM_ROWS, start=1):
        val = values[row_num]
        if row_num in ("17", "18", "25"):
            colour = _GREEN if val >= 0 else _RED
            tbl_style.append(("TEXTCOLOR", (3, i), (3, i), colour))
            tbl_style.append(("FONTNAME",  (3, i), (3, i), bold))

    # Bold the total tax row (last row = Row 31)
    tbl_style.append(("FONTNAME",  (0, len(_FORM_ROWS)), (-1, len(_FORM_ROWS)), bold))
    tbl_style.append(("BACKGROUND",(0, len(_FORM_ROWS)), (-1, len(_FORM_ROWS)),
                      colors.HexColor("#fff3cd")))

    calc_table.setStyle(TableStyle(tbl_style))
    flowables.append(calc_table)
    flowables.append(Spacer(1, 3 * mm))

    # ── FIFO lot detail ───────────────────────────────────────────────────────
    lot_header = [
        "Symbol", "Amount Disposed", "Acq. Date", "Acq. TX (short)",
        "Lot Cost ₪", "Proceeds ₪", "Gain/(Loss) ₪",
    ]
    lot_rows = [lot_header]
    for disposal in pe.disposals:
        for lot, amt_from_lot, cost_from_lot in disposal.lots_used:
            fraction    = amt_from_lot / disposal.amount_disposed if disposal.amount_disposed else Decimal(0)
            lot_proc    = (disposal.proceeds_ils * fraction).quantize(Decimal("0.01"))
            lot_cost    = cost_from_lot
            lot_gain    = (lot_proc - lot_cost).quantize(Decimal("0.01"))
            lot_rows.append([
                disposal.symbol,
                f"{float(amt_from_lot):,.6f}",
                lot.acquired_at.strftime("%d/%m/%Y"),
                lot.tx_hash[:14] + "…",
                f"{float(lot_cost):,.2f}",
                f"{float(lot_proc):,.2f}",
                f"{float(lot_gain):+,.2f}",
            ])

    if len(lot_rows) > 1:
        flowables.append(Paragraph(
            _bidi("FIFO Lot Detail (נספח פירוט רכישות FIFO):"), styles["label_bold"]
        ))
        flowables.append(Spacer(1, 1 * mm))

        lot_col_w = [1.5*cm, 2.8*cm, 2.0*cm, 3.5*cm, 2.3*cm, 2.3*cm, 2.6*cm]
        lot_table = Table(lot_rows, colWidths=lot_col_w)
        lot_tbl_style = [
            ("FONTNAME",      (0, 0), (-1, -1), font),
            ("FONTSIZE",      (0, 0), (-1, -1), 7.5),
            ("FONTNAME",      (0, 0), (-1, 0), bold),
            ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#4a5568")),
            ("TEXTCOLOR",     (0, 0), (-1, 0), _WHITE),
            ("GRID",          (0, 0), (-1, -1), 0.3, _GREY_LINE),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING",   (0, 0), (-1, -1), 3),
            ("ALIGN",         (4, 0), (6, -1), "RIGHT"),
        ]
        for i in range(1, len(lot_rows)):
            val = Decimal(lot_rows[i][6].replace(",", "").replace("+", ""))
            colour = _GREEN if val >= 0 else _RED
            lot_tbl_style.append(("TEXTCOLOR", (6, i), (6, i), colour))
            if i % 2 == 0:
                lot_tbl_style.append(("BACKGROUND", (0, i), (-1, i), _BLUE_LIGHT))
        lot_table.setStyle(TableStyle(lot_tbl_style))
        flowables.append(lot_table)

    flowables.append(Spacer(1, 3 * mm))
    flowables.append(HRFlowable(width="100%", thickness=0.5, color=_GREY_LINE))
    flowables.append(Spacer(1, 5 * mm))
    return flowables


# ── Summary page ──────────────────────────────────────────────────────────────

def _build_summary_page(
    summary: TaxSummary,
    address: str,
    from_date: date,
    to_date: date,
    n_events: int,
    styles: dict,
    font: str,
) -> list:
    bold = font + "B" if font == "DejaVu" else font + "-Bold"
    flowables = [PageBreak()]

    hdr = Table(
        [[Paragraph(_bidi("Form 1399י — Annual Summary / סיכום שנתי"), styles["section_head"])]],
        colWidths=[17 * cm],
    )
    hdr.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), _BLUE_DARK),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
    ]))
    flowables += [hdr, Spacer(1, 6 * mm)]

    net = summary.total_capital_gains_ils - summary.total_capital_losses_ils
    taxable = max(Decimal(0), net) + summary.total_income_ils

    rows = [
        ("Wallet Address", address, False),
        (_bidi("Tax Year / שנת מס"), str(summary.tax_year), False),
        ("Report Period",
         f"{from_date.strftime('%d/%m/%Y')} – {to_date.strftime('%d/%m/%Y')}", False),
        ("Disposal Events Reported", str(n_events), False),
        ("", "", False),
        (_bidi("Capital Gains (gross) / רווחי הון ברוטו"),
         _ils(summary.total_capital_gains_ils), False),
        (_bidi("Capital Losses / הפסדי הון"),
         _ils(summary.total_capital_losses_ils), False),
        (_bidi("Net Capital Gains / רווח הון נטו"), _ils(net), True),
        (_bidi("Staking / DeFi Income / הכנסה"), _ils(summary.total_income_ils), False),
        (_bidi("Gas Fees / עמלות גז"), _ils(summary.total_fees_ils), False),
        ("", "", False),
        (_bidi("Taxable Amount / סכום חייב"), _ils(taxable), True),
        (_bidi(f"CGT at {int(ISRAEL_CGT_RATE*100)}% / מס רווח הון"),
         _ils(summary.cgt_owed_ils), False),
        (_bidi("Surtax (5% above ₪721,560) / מס נוסף"),
         _ils(summary.surtax_owed_ils), False),
        (_bidi("TOTAL TAX OWED / סה״כ מס לתשלום"),
         _ils(summary.total_tax_owed_ils), True),
    ]

    tbl_data = []
    tbl_style = [
        ("FONTNAME",      (0, 0), (-1, -1), font),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("GRID",          (0, 0), (-1, -1), 0.3, _GREY_LINE),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("ALIGN",         (1, 0), (1, -1), "RIGHT"),
        ("BACKGROUND",    (0, 0), (0, -1), colors.HexColor("#f0f4f8")),
    ]
    for i, (label, value, is_total) in enumerate(rows):
        tbl_data.append([label, value])
        if is_total:
            tbl_style.append(("FONTNAME",  (0, i), (-1, i), bold))
            tbl_style.append(("BACKGROUND",(0, i), (-1, i), colors.HexColor("#fff3cd")))
        if not label:
            tbl_style.append(("BACKGROUND",(0, i), (-1, i), _WHITE))

    summary_table = Table(tbl_data, colWidths=[12 * cm, 5 * cm])
    summary_table.setStyle(TableStyle(tbl_style))
    flowables += [summary_table, Spacer(1, 8 * mm)]

    # Filing checklist
    checklist_items = [
        "File Form 1399 + pay advance CGT within 30 days of each disposal",
        "Attach this document to annual Form 1301 (deadline: 30 April following tax year)",
        "Verify inflationary adjustment with your CPA — set to ₪0 in this report",
        "Losses can offset gains in the same year; carry-forward requires separate application",
        "Staking/DeFi income is treated as passive capital income (25%) — active miners may owe marginal rate",
        "Substantial shareholders (>10% ownership of any company) are subject to 30% CGT — verify if applicable",
    ]
    flowables.append(Paragraph(_bidi("Filing Checklist / רשימת בדיקה:"), styles["label_bold"]))
    flowables.append(Spacer(1, 2 * mm))
    for item in checklist_items:
        flowables.append(Paragraph(f"☐  {item}", styles["note"]))
    flowables.append(Spacer(1, 6 * mm))

    # Disclaimer
    disclaimer = (
        "DISCLAIMER: This document is generated programmatically from on-chain data "
        "and is provided for informational purposes only. It does not constitute tax advice. "
        "Figures have not been reviewed by a Certified Public Accountant. "
        "The inflationary amount (Row 17) has been set to ₪0 — this may understate "
        "the taxable amount if the acquisition price was in USD and the NIS/USD rate changed. "
        "Consult a licensed Israeli CPA (רואה חשבון מוסמך) before filing. "
        "ITA references: Income Tax Ordinance §88, §91; Circular 05/2018; gov.il FAQ on digital assets."
    )
    flowables.append(Paragraph(_bidi(disclaimer), styles["disclaimer"]))
    return flowables


# ── Main export ───────────────────────────────────────────────────────────────

def generate_form_1399(
    events: list[ProcessedEvent],
    summary: TaxSummary,
    address: str,
    from_date: date,
    to_date: date,
    output_path: str,
) -> int:
    """
    Generate a Form 1399 PDF for all capital gain/loss disposal events.
    Returns the number of disposal events written.
    """
    font = _register_fonts()
    styles = _make_styles(font)
    bold = font + "B" if font == "DejaVu" else font + "-Bold"

    disposal_events = _disposal_events(events)
    if not disposal_events:
        return 0

    path = Path(output_path)
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        title=f"Form 1399 – {address[:14]} – {from_date.year}",
        author="starknet-tax-il",
        subject="Israeli Capital Gains Tax – Virtual Assets",
    )

    flowables: list = []

    # ── Cover ─────────────────────────────────────────────────────────────────
    flowables.append(Spacer(1, 1 * cm))
    flowables.append(Paragraph(
        _bidi("הודעה על מכירת נכס וחישוב המס"), styles["cover_title"]
    ))
    flowables.append(Paragraph(
        "Notice of Asset Sale and Tax Calculation — Form 1399י", styles["cover_title"]
    ))
    flowables.append(Spacer(1, 4 * mm))
    flowables.append(Paragraph(
        f"Tax Year: {from_date.year}  |  Wallet: {address}  |  "
        f"Period: {from_date.strftime('%d/%m/%Y')} – {to_date.strftime('%d/%m/%Y')}  |  "
        f"Disposal events: {len(disposal_events)}",
        styles["cover_sub"],
    ))
    flowables.append(Spacer(1, 2 * mm))
    flowables.append(Paragraph(
        _bidi(
            "Transaction Code (סמל עסקה): 77 — All other assets including virtual currency  |  "
            "Asset Type: Virtual Currency / מטבע וירטואלי  |  "
            "Sections 88 & 91, Income Tax Ordinance"
        ),
        styles["cover_note"],
    ))
    flowables.append(HRFlowable(width="100%", thickness=1, color=_BLUE_DARK, spaceAfter=8))

    # ── One block per disposal event ──────────────────────────────────────────
    for pe in disposal_events:
        flowables.extend(_build_event_section(pe, styles, font))

    # ── Summary ───────────────────────────────────────────────────────────────
    flowables.extend(_build_summary_page(
        summary, address, from_date, to_date, len(disposal_events), styles, font
    ))

    doc.build(flowables)
    return len(disposal_events)
