"""
Generate the CSV tax report.

Structure:
  Section 1 — Transaction Detail (one row per tax event)
  Section 2 — Disposal Detail (one row per FIFO lot match, for Form 1399)
  Section 3 — Summary (totals, tax owed)

Column headers are bilingual (English + Hebrew) to match ITA Form 1399 terminology.
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from .classifier import EventType
from .config import ISRAEL_CGT_RATE, ISRAEL_SURTAX_RATE
from .tax import ProcessedEvent, TaxSummary


_ROUNDING = Decimal("0.01")


def _d(value: Decimal) -> str:
    """Format Decimal for CSV — two decimal places."""
    return str(value.quantize(_ROUNDING))


def _f(value: Decimal, decimals: int = 6) -> str:
    """Format token amount."""
    return str(value.quantize(Decimal(f"0.{'0' * decimals}")))


# ── Section 1: Transaction detail ────────────────────────────────────────────

DETAIL_HEADERS = [
    "Date / תאריך",
    "TX Hash",
    "Event Type / סוג אירוע",
    "Token In / נכס נכנס",
    "Amount In / כמות נכנסת",
    "Token Out / נכס יוצא",
    "Amount Out / כמות יוצאת",
    "ILS Price In (₪)",
    "ILS Price Out (₪)",
    "Proceeds (₪) / תמורה",
    "Cost Basis (₪) / עלות",
    "Gain/Loss (₪) / רווח/הפסד",
    "Income (₪) / הכנסה",
    "Gas Fee (₪)",
    "Needs Review / לבדיקה",
    "Notes",
]


def _format_flows(flows, prices: dict[str, Decimal]) -> tuple[str, str, str]:
    """Returns (tokens_str, amounts_str, price_str)."""
    if not flows:
        return "", "", ""
    tokens = " + ".join(f.symbol for f in flows)
    amounts = " + ".join(_f(f.amount) for f in flows)
    price_strs = []
    for f in flows:
        p = prices.get(f.symbol)
        price_strs.append(_d(p) if p is not None else "?")
    return tokens, amounts, " + ".join(price_strs)


_LARGE_RECEIVE_THRESHOLD = Decimal("100")  # NIS — flag RECEIVE events above this


def _is_large_receive(pe: ProcessedEvent) -> bool:
    """True when a RECEIVE event carries a significant NIS value."""
    if pe.event.event_type != EventType.RECEIVE:
        return False
    total = Decimal(0)
    for f in pe.event.tokens_in:
        p = pe.event.price_ils_in.get(f.symbol, Decimal(0))
        total += f.amount * p
    return total > _LARGE_RECEIVE_THRESHOLD


def _build_notes(pe: ProcessedEvent) -> str:
    if pe.needs_review:
        return f"REVIEW: {pe.review_reason}"
    base = pe.event.notes or ""
    if _is_large_receive(pe):
        base += (
            " ⚠ LARGE INCOMING TRANSFER — if this is an airdrop or grant it must "
            "be reported as income at FMV on receipt date."
        )
    return base


def write_detail_section(writer: csv.writer, events: list[ProcessedEvent]) -> None:
    writer.writerow([])
    writer.writerow(["=== SECTION 1: TRANSACTION DETAIL / פירוט עסקאות ==="])
    writer.writerow(DETAIL_HEADERS)

    for pe in events:
        evt = pe.event

        token_in, amount_in, price_in = _format_flows(evt.tokens_in, evt.price_ils_in)
        token_out, amount_out, price_out = _format_flows(evt.tokens_out, evt.price_ils_out)

        # Non-taxable lock-ups have no proceeds or cost basis — show ₪0.
        non_taxable = evt.event_type in (EventType.STAKE_DEPOSIT, EventType.STAKE_WITHDRAWAL)

        # Proceeds = sum of (amount_out * price_out)
        proceeds = Decimal(0)
        if not non_taxable:
            for f in evt.tokens_out:
                p = evt.price_ils_out.get(f.symbol, Decimal(0))
                proceeds += f.amount * p

        # Cost basis = sum from FIFO disposals
        cost_basis = Decimal(0) if non_taxable else sum(
            (d.cost_basis_ils for d in pe.disposals), Decimal(0),
        )

        writer.writerow([
            evt.timestamp.strftime("%Y-%m-%d %H:%M UTC"),
            evt.tx_hash,
            evt.event_type.value,
            token_in,
            amount_in,
            token_out,
            amount_out,
            price_in,
            price_out,
            _d(proceeds),
            _d(cost_basis),
            _d(pe.net_gain_loss_ils),
            _d(pe.income_ils),
            _d(pe.fee_ils),
            "YES" if pe.needs_review or _is_large_receive(pe) else "",
            _build_notes(pe),
        ])


# ── Section 2: FIFO disposal detail (Form 1399 data) ─────────────────────────

DISPOSAL_HEADERS = [
    "Disposal Date / תאריך מכירה",
    "TX Hash",
    "Token / נכס",
    "Amount Disposed / כמות שנמכרה",
    "Acquisition Date / תאריך רכישה",
    "Acquisition TX",
    "Amount From Lot / כמות מהרכישה",
    "Lot Unit Cost (₪) / עלות יחידה",
    "Total Lot Cost (₪) / עלות כוללת",
    "Proceeds (₪) / תמורה כוללת",
    "Gain/Loss (₪) / רווח/הפסד",
]


def write_disposal_section(writer: csv.writer, events: list[ProcessedEvent]) -> None:
    writer.writerow([])
    writer.writerow(["=== SECTION 2: FIFO DISPOSAL DETAIL (Form 1399 / טופס 1399) ==="])
    writer.writerow(DISPOSAL_HEADERS)

    for pe in events:
        for disposal in pe.disposals:
            # Proceeds per lot — pro-rate by amount
            for lot, amount_from_lot, cost_from_lot in disposal.lots_used:
                fraction = amount_from_lot / disposal.amount_disposed if disposal.amount_disposed else Decimal(0)
                lot_proceeds = (disposal.proceeds_ils * fraction).quantize(_ROUNDING)
                # Unit cost: use 6 d.p. to expose full precision (avoids rounding gaps)
                lot_unit_cost = (cost_from_lot / amount_from_lot).quantize(
                    Decimal("0.000001")
                ) if amount_from_lot else Decimal(0)
                lot_gain = (lot_proceeds - cost_from_lot).quantize(_ROUNDING)

                writer.writerow([
                    disposal.disposal_date.strftime("%Y-%m-%d"),
                    disposal.disposal_tx,
                    disposal.symbol,
                    _f(disposal.amount_disposed),
                    lot.acquired_at.strftime("%Y-%m-%d"),
                    lot.tx_hash,
                    _f(amount_from_lot),
                    str(lot_unit_cost),   # 6 d.p. — full precision
                    _d(cost_from_lot),    # authoritative cost from FIFO
                    _d(lot_proceeds),
                    _d(lot_gain),
                ])


# ── Section 3: Summary ────────────────────────────────────────────────────────

def write_summary_section(
    writer: csv.writer,
    summary: TaxSummary,
    address: str,
    from_date: date,
    to_date: date,
) -> None:
    writer.writerow([])
    writer.writerow(["=== SECTION 3: TAX SUMMARY / סיכום מס ==="])

    rows = [
        ("Wallet Address / כתובת ארנק", address),
        ("Tax Year / שנת מס", str(summary.tax_year)),
        ("Report Period / תקופת דו״ח", f"{from_date} to {to_date}"),
        ("", ""),
        ("--- Capital Gains / רווחי הון ---", ""),
        ("Gross Capital Gains (₪) / רווחי הון ברוטו", _d(summary.total_capital_gains_ils)),
        ("Capital Losses (₪) / הפסדי הון", _d(summary.total_capital_losses_ils)),
        ("Net Capital Gains (₪) / רווחי הון נטו", _d(summary.total_capital_gains_ils - summary.total_capital_losses_ils)),
        ("", ""),
        ("--- Income / הכנסה ---", ""),
        ("Staking & DeFi Income (₪) / הכנסה מסטייקינג ו-DeFi", _d(summary.total_income_ils)),
        ("", ""),
        ("--- Deductions / ניכויים ---", ""),
        ("Total Gas Fees (₪) / עמלות גז", _d(summary.total_fees_ils)),
        ("", ""),
        ("--- Tax Calculation / חישוב מס ---", ""),
        ("Taxable Amount (₪) / סכום חייב במס",
         _d(max(Decimal(0), summary.total_capital_gains_ils - summary.total_capital_losses_ils) + summary.total_income_ils)),
        (f"CGT Rate / שיעור מס רווחי הון", f"{int(ISRAEL_CGT_RATE * 100)}%"),
        ("CGT Owed (₪) / מס רווחי הון", _d(summary.cgt_owed_ils)),
        (f"Surtax Rate (above 721,560 ₪) / מס על הכנסה גבוהה",
         f"{int(ISRAEL_SURTAX_RATE * 100)}% (Section 121B(f); may be 5% per 121B(b) — consult CPA)"),
        ("Surtax Owed (₪) / מס נוסף", _d(summary.surtax_owed_ils)),
        ("", ""),
        ("TOTAL TAX OWED (₪) / סה״כ מס לתשלום", _d(summary.total_tax_owed_ils)),
        ("", ""),
        ("--- Notes / הערות ---", ""),
        ("Tax basis", "ITA Circular 05/2018; FIFO method; passive investor rates applied"),
        ("Staking income", "Taxed as capital income (25%) at FMV on claim date"),
        ("Gas fees", "Tracked separately — consult accountant for deductibility treatment"),
        ("Active validators", "May owe marginal income tax instead of CGT — consult accountant"),
        ("Form 1399 / טופס 1399",
         "File within 30 days of each disposal event and pay advance tax. "
         "This tool does NOT track filing deadlines — the taxpayer is responsible."),
        ("RECEIVE events",
         "RECEIVE events are treated as cost-basis acquisitions at FMV. "
         "If any represent airdrops, grants, or staking distributions, they must be "
         "separately reported as income. Review each RECEIVE event manually."),
        ("USD/ILS exchange rates",
         "Sourced from Yahoo Finance market mid-rates (USDILS=X), not official Bank of "
         "Israel published rates. Verify against boi.org.il before filing."),
        ("Token prices",
         "USD prices from DeFiLlama (daily snapshots); intraday volatility may cause "
         "±1-2% variance from actual execution prices."),
        ("Vault tokens (xSTRK, vWBTC, …)",
         "Priced via on-chain vault exchange rate (interpolated between earliest-tx block "
         "and latest block). Small approximation error possible vs. exact block-level rate."),
        ("Disclaimer / הצהרה",
         "THIS REPORT IS NOT TAX ADVICE. It is a computational aid only. "
         "All figures, classifications, and tax calculations must be reviewed and "
         "approved by a licensed Israeli CPA (רו\"ח) before filing with the "
         "Israel Tax Authority (רשות המסים)."),
    ]

    if summary.needs_manual_review:
        rows.append(("", ""))
        rows.append(("Transactions Needing Manual Review:", ""))
        for tx in summary.needs_manual_review:
            rows.append(("  " + tx, ""))

    for label, value in rows:
        writer.writerow([label, value])


# ── Main export ───────────────────────────────────────────────────────────────

def generate_report(
    events: list[ProcessedEvent],
    summary: TaxSummary,
    address: str,
    from_date: date,
    to_date: date,
    output_path: str,
) -> None:
    path = Path(output_path)
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig for Excel compatibility
        writer = csv.writer(f)

        writer.writerow([f"StarkNet Israeli Tax Report — {address}"])
        writer.writerow([f"Generated: {date.today().isoformat()}"])
        writer.writerow([f"Period: {from_date} to {to_date}"])
        writer.writerow([
            "Note: Section 1 (and disposal rows tied to it) include only transactions "
            "dated within the period above. Full chain history still drives FIFO cost basis."
        ])

        write_detail_section(writer, events)
        write_disposal_section(writer, events)
        write_summary_section(writer, summary, address, from_date, to_date)

    print(f"\nReport written to: {path.resolve()}")
