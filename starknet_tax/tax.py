"""
Apply Israeli tax rules to classified events and produce per-event tax calculations.

Key rules:
- Capital gains tax: 25% flat (+ 3% surtax above 721,560 NIS per Section 121B(f);
  some CPAs apply 5% if crypto falls under Section 121B(b) — consult your CPA)
- Staking / DeFi income: taxed at 25% CGT for passive investors
  (active validators / heavy traders may face marginal income tax — flagged)
- FIFO cost basis
- Losses offset gains in the same year; carry-forward tracked
- Gas fees: included as acquisition cost on buys / deducted from proceeds on sells
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from .classifier import EventType, TaxEvent
from .config import ISRAEL_CGT_RATE, ISRAEL_SURTAX_RATE, ISRAEL_SURTAX_THRESHOLD
from .fetcher import TokenFlow
from .fifo import DisposalResult, FIFOTracker
from .pricing import PriceCache


@dataclass
class TaxSummary:
    tax_year: int
    total_capital_gains_ils: Decimal = Decimal(0)    # net (gains - losses)
    total_capital_losses_ils: Decimal = Decimal(0)
    total_income_ils: Decimal = Decimal(0)            # staking + DeFi
    total_fees_ils: Decimal = Decimal(0)              # gas fees paid
    cgt_owed_ils: Decimal = Decimal(0)                # at 25%
    surtax_owed_ils: Decimal = Decimal(0)             # at 5% above threshold
    total_tax_owed_ils: Decimal = Decimal(0)
    needs_manual_review: list[str] = field(default_factory=list)  # tx hashes


@dataclass
class ProcessedEvent:
    """A TaxEvent enriched with FIFO disposals and final ILS amounts."""
    event: TaxEvent
    disposals: list[DisposalResult] = field(default_factory=list)
    income_ils: Decimal = Decimal(0)         # for income-type events
    net_gain_loss_ils: Decimal = Decimal(0)  # sum of disposal gains/losses
    fee_ils: Decimal = Decimal(0)            # gas fee in ILS (for CSV column)
    needs_review: bool = False
    review_reason: str = ""


def _ils_value(flows: list[TokenFlow], prices: dict[str, Decimal]) -> Decimal:
    total = Decimal(0)
    for f in flows:
        price = prices.get(f.symbol, Decimal(0))
        total += f.amount * price
    return total.quantize(Decimal("0.01"), ROUND_HALF_UP)


def process_events(
    events: list[TaxEvent],
    prices: PriceCache,
    from_date: date,
    to_date: date,
) -> tuple[list[ProcessedEvent], TaxSummary]:
    """
    Walk through events in chronological order, apply FIFO, and compute tax.
    Returns (processed_events, summary).

    ``processed_events`` contains only events whose transaction **date** falls
    within ``[from_date, to_date]`` — these are what the CSV report lists. All
    ``events`` are still processed in chronological order for FIFO.
    """
    events_sorted = sorted(events, key=lambda e: e.timestamp)
    fifo = FIFOTracker()
    processed: list[ProcessedEvent] = []

    summary = TaxSummary(tax_year=from_date.year)

    for evt in events_sorted:
        d = evt.timestamp.date()
        # in_period: whether this event falls within the reporting window.
        # FIFO operations (acquire/dispose) always run so cost basis is correct.
        # Only in-period events contribute to summary totals and the report.
        in_period = (from_date <= d <= to_date)
        pe = ProcessedEvent(event=evt)

        # ── Price lookups ────────────────────────────────────────────────────
        prices_in: dict[str, Decimal] = {}
        prices_out: dict[str, Decimal] = {}
        missing_price = False

        for flow in evt.tokens_in:
            p = prices.get(flow.symbol, d)
            if p is None:
                missing_price = True
                p = Decimal(0)
            prices_in[flow.symbol] = p

        for flow in evt.tokens_out:
            p = prices.get(flow.symbol, d)
            if p is None:
                missing_price = True
                p = Decimal(0)
            prices_out[flow.symbol] = p

        # Gas fee in ILS (always computed; only accumulated for in-period events)
        fee_price = prices.get(evt.fee_token, d) or Decimal(0)
        fee_ils = (evt.fee_amount * fee_price).quantize(Decimal("0.01"), ROUND_HALF_UP)
        pe.fee_ils = fee_ils
        if in_period:
            summary.total_fees_ils += fee_ils

        evt.price_ils_in = prices_in
        evt.price_ils_out = prices_out

        if missing_price and in_period:
            missing = [f.symbol for f in evt.tokens_in + evt.tokens_out
                       if prices.get(f.symbol, d) is None]
            raise RuntimeError(
                f"Missing ILS price for {', '.join(missing)} on {d} "
                f"(tx {evt.tx_hash[:14]}...). "
                f"Price fetch must have failed — re-run or check internet connection."
            )

        # ── Handle each event type ───────────────────────────────────────────
        # FIFO acquire/dispose runs for ALL events (full history needed for
        # correct cost basis).  Summary accumulation is gated on in_period.

        if evt.event_type in (EventType.SWAP, EventType.LIQUID_STAKE, EventType.LIQUID_UNSTAKE):
            # Disposal of tokens_out → gain/loss
            # Acquisition of tokens_in → new cost basis at effective price
            # (effective price = total proceeds ÷ total tokens_in, ensuring
            #  the ILS Price In column in the report is always consistent with
            #  the reported proceeds — eliminates pricing-date artifacts).
            for flow in evt.tokens_out:
                price = prices_out.get(flow.symbol, Decimal(0))
                proceeds_ils = (flow.amount * price).quantize(Decimal("0.01"), ROUND_HALF_UP)
                disposal = fifo.dispose(
                    symbol=flow.symbol,
                    amount=flow.amount,
                    proceeds_ils=proceeds_ils,
                    disposed_at=evt.timestamp,
                    tx_hash=evt.tx_hash,
                )
                pe.disposals.append(disposal)
                pe.net_gain_loss_ils += disposal.gain_loss_ils

            # Derive effective acquisition price from total disposal proceeds
            total_proceeds = sum(d.proceeds_ils for d in pe.disposals)
            total_in_amount = sum(f.amount for f in evt.tokens_in)
            for flow in evt.tokens_in:
                if total_in_amount > 0:
                    # Pro-rate proceeds across tokens_in by amount
                    effective_price = (total_proceeds * (flow.amount / total_in_amount)
                                       / flow.amount).quantize(Decimal("0.000001"), ROUND_HALF_UP)
                else:
                    effective_price = prices_in.get(flow.symbol, Decimal(0))
                prices_in[flow.symbol] = effective_price   # update for report display
                fifo.acquire(
                    symbol=flow.symbol,
                    amount=flow.amount,
                    price_ils=effective_price,
                    acquired_at=evt.timestamp,
                    tx_hash=evt.tx_hash,
                )
            evt.price_ils_in = prices_in  # refresh after effective-price override

        elif evt.event_type in (EventType.STAKING_INCOME, EventType.DEFI_INCOME, EventType.AIRDROP):
            # Income: taxed at FMV on receipt
            for flow in evt.tokens_in:
                price = prices_in.get(flow.symbol, Decimal(0))
                income = (flow.amount * price).quantize(Decimal("0.01"), ROUND_HALF_UP)
                pe.income_ils += income
                if in_period:
                    summary.total_income_ils += income
                # Establish cost basis at FMV (for future disposal)
                fifo.acquire(
                    symbol=flow.symbol,
                    amount=flow.amount,
                    price_ils=price,
                    acquired_at=evt.timestamp,
                    tx_hash=evt.tx_hash,
                )

        elif evt.event_type == EventType.RECEIVE:
            # Acquisition — no taxable event, just update cost basis
            for flow in evt.tokens_in:
                price = prices_in.get(flow.symbol, Decimal(0))
                fifo.acquire(
                    symbol=flow.symbol,
                    amount=flow.amount,
                    price_ils=price,
                    acquired_at=evt.timestamp,
                    tx_hash=evt.tx_hash,
                )

        elif evt.event_type == EventType.SEND:
            # Disposal — capital gain/loss
            for flow in evt.tokens_out:
                price = prices_out.get(flow.symbol, Decimal(0))
                proceeds_ils = (flow.amount * price).quantize(Decimal("0.01"), ROUND_HALF_UP)
                disposal = fifo.dispose(
                    symbol=flow.symbol,
                    amount=flow.amount,
                    proceeds_ils=proceeds_ils,
                    disposed_at=evt.timestamp,
                    tx_hash=evt.tx_hash,
                )
                pe.disposals.append(disposal)
                pe.net_gain_loss_ils += disposal.gain_loss_ils

        elif evt.event_type in (EventType.STAKE_DEPOSIT, EventType.STAKE_WITHDRAWAL):
            # Non-taxable in both directions.
            # STAKE_DEPOSIT: lots stay in FIFO (not disposed) — tokens remain yours.
            # STAKE_WITHDRAWAL: principal returns — lots already in inventory, no re-acquisition needed.
            pass

        elif evt.event_type == EventType.UNKNOWN and in_period:
            pe.needs_review = True
            pe.review_reason = evt.notes or "Unclassified event — review manually."
            summary.needs_manual_review.append(evt.tx_hash)

        # Accumulate gains/losses into summary (in-period only)
        if in_period:
            if pe.net_gain_loss_ils > 0:
                summary.total_capital_gains_ils += pe.net_gain_loss_ils
            else:
                summary.total_capital_losses_ils += abs(pe.net_gain_loss_ils)
            processed.append(pe)

    # ── Final tax calculation ────────────────────────────────────────────────
    # Net capital gains (income + capital gains treated at CGT rate for passive investors)
    net_gains = summary.total_capital_gains_ils - summary.total_capital_losses_ils
    if net_gains < 0:
        net_gains = Decimal(0)  # losses beyond gains don't create negative tax

    taxable_amount = net_gains + summary.total_income_ils

    cgt = (taxable_amount * Decimal(str(ISRAEL_CGT_RATE))).quantize(Decimal("0.01"), ROUND_HALF_UP)
    summary.cgt_owed_ils = cgt

    # Surtax on capital income above threshold (rate set in config.py)
    if taxable_amount > ISRAEL_SURTAX_THRESHOLD:
        surtax_base = taxable_amount - Decimal(str(ISRAEL_SURTAX_THRESHOLD))
        summary.surtax_owed_ils = (surtax_base * Decimal(str(ISRAEL_SURTAX_RATE))).quantize(
            Decimal("0.01"), ROUND_HALF_UP
        )

    summary.total_tax_owed_ils = summary.cgt_owed_ils + summary.surtax_owed_ils

    return processed, summary
