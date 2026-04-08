"""
Classify parsed transactions into Israeli tax event types.

Israeli tax rules applied:
- Crypto-to-crypto swap → capital gain/loss on disposed token
- Staking reward claim → income (taxed at receipt FMV in NIS)
- DeFi yield / airdrop received → income
- Token sent to external address → capital gain/loss (disposal)
- Token received from external address → acquisition (cost basis established)
- Self-transfer → non-taxable
- Gas fees → deductible from gains (tracked separately)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .config import (
    ALL_PROTOCOL_CONTRACTS,
    DEFI_INCOME_CONTRACTS,
    DEX_CONTRACTS,
    POOL_MEMBER_EXIT_ACTION_SELECTOR,
    POOL_MEMBER_REWARD_CLAIMED_SELECTOR,
    STAKER_REWARD_CLAIMED_SELECTOR,
    STAKING_CONTRACT,
)
from .fetcher import ParsedTransaction, TokenFlow


class EventType(str, Enum):
    SWAP = "SWAP"                         # crypto-to-crypto exchange → CGT
    STAKING_INCOME = "STAKING_INCOME"     # STRK reward claim → income
    DEFI_INCOME = "DEFI_INCOME"           # yield / interest → income
    AIRDROP = "AIRDROP"                   # received without sending → income
    SEND = "SEND"                         # outgoing transfer → CGT (disposal)
    RECEIVE = "RECEIVE"                   # incoming purchase / gift → cost basis
    LIQUID_STAKE = "LIQUID_STAKE"         # STRK → xSTRK (taxable exchange)
    LIQUID_UNSTAKE = "LIQUID_UNSTAKE"     # xSTRK → STRK (taxable exchange)
    STAKE_DEPOSIT = "STAKE_DEPOSIT"       # STRK delegated to staking pool (non-taxable lock-up)
    STAKE_WITHDRAWAL = "STAKE_WITHDRAWAL" # staked STRK principal returned (non-taxable)
    FEE_ONLY = "FEE_ONLY"                 # only gas paid, no token movement
    UNKNOWN = "UNKNOWN"                   # needs manual review


@dataclass
class TaxEvent:
    tx_hash: str
    timestamp: datetime
    event_type: EventType
    tokens_in: list[TokenFlow]   # tokens RECEIVED by wallet
    tokens_out: list[TokenFlow]  # tokens SENT from wallet
    # Gas fee for this tx (in ETH or STRK, deductible)
    fee_token: str
    fee_amount: Decimal
    notes: str = ""

    # Filled in by the tax calculator after price lookup
    proceeds_ils: Decimal = Decimal(0)
    cost_basis_ils: Decimal = Decimal(0)
    gain_loss_ils: Decimal = Decimal(0)
    income_ils: Decimal = Decimal(0)
    price_ils_in: dict[str, Decimal] = field(default_factory=dict)   # symbol → ILS
    price_ils_out: dict[str, Decimal] = field(default_factory=dict)  # symbol → ILS


def _has_staking_claim_event(ptx: ParsedTransaction) -> bool:
    """Return True if the tx contains a staking reward claim event."""
    claim_selectors = {
        POOL_MEMBER_REWARD_CLAIMED_SELECTOR.lower(),
        STAKER_REWARD_CLAIMED_SELECTOR.lower(),
    }
    for event in ptx.raw_events:
        keys = event.get("keys", [])
        if keys and keys[0].lower() in claim_selectors:
            return True
    return False


def _has_staking_exit_event(ptx: ParsedTransaction) -> bool:
    """Return True if the tx contains a PoolMemberExitAction event (principal withdrawal)."""
    selector = POOL_MEMBER_EXIT_ACTION_SELECTOR.lower()
    for event in ptx.raw_events:
        keys = event.get("keys", [])
        if keys and keys[0].lower() == selector:
            return True
    return False


def _touches_dex(ptx: ParsedTransaction) -> bool:
    return bool(ptx.touched_contracts & DEX_CONTRACTS)


def _touches_defi_income(ptx: ParsedTransaction) -> bool:
    return bool(ptx.touched_contracts & DEFI_INCOME_CONTRACTS)


def _touches_staking(ptx: ParsedTransaction) -> bool:
    return STAKING_CONTRACT in ptx.touched_contracts


def _is_liquid_stake(ptx: ParsedTransaction) -> bool:
    """STRK goes out, xSTRK comes in (or vice versa)."""
    symbols_in = {f.symbol for f in ptx.tokens_in}
    symbols_out = {f.symbol for f in ptx.tokens_out}
    return (
        ("STRK" in symbols_out and "xSTRK" in symbols_in)
        or ("xSTRK" in symbols_out and "STRK" in symbols_in)
    )


def classify(ptx: ParsedTransaction) -> TaxEvent:
    has_in = bool(ptx.tokens_in)
    has_out = bool(ptx.tokens_out)

    # ── Liquid staking ───────────────────────────────────────────────────────
    if _is_liquid_stake(ptx):
        symbols_out = {f.symbol for f in ptx.tokens_out}
        etype = EventType.LIQUID_STAKE if "STRK" in symbols_out else EventType.LIQUID_UNSTAKE
        return TaxEvent(
            tx_hash=ptx.tx_hash,
            timestamp=ptx.timestamp,
            event_type=etype,
            tokens_in=ptx.tokens_in,
            tokens_out=ptx.tokens_out,
            fee_token=ptx.fee_token,
            fee_amount=ptx.fee_amount,
            notes="Liquid staking exchange — treated as taxable crypto-to-crypto swap under ITA rules.",
        )

    # ── Staking reward claim / principal withdrawal ─────────────────────────
    if _touches_staking(ptx) and has_in and not has_out:
        if _has_staking_claim_event(ptx):
            return TaxEvent(
                tx_hash=ptx.tx_hash,
                timestamp=ptx.timestamp,
                event_type=EventType.STAKING_INCOME,
                tokens_in=ptx.tokens_in,
                tokens_out=[],
                fee_token=ptx.fee_token,
                fee_amount=ptx.fee_amount,
                notes="STRK staking reward — taxable as income at FMV on claim date (ITA Circular 05/2018).",
            )
        if _has_staking_exit_event(ptx):
            return TaxEvent(
                tx_hash=ptx.tx_hash,
                timestamp=ptx.timestamp,
                event_type=EventType.STAKE_WITHDRAWAL,
                tokens_in=ptx.tokens_in,
                tokens_out=[],
                fee_token=ptx.fee_token,
                fee_amount=ptx.fee_amount,
                notes="Staked STRK principal returned — non-taxable. FIFO lots preserved from original acquisition.",
            )
        # Tokens received from staking contract with no recognised event selector.
        # Could be a new contract version or an edge case — flag for manual review
        # rather than incorrectly taxing the principal as income.
        return TaxEvent(
            tx_hash=ptx.tx_hash,
            timestamp=ptx.timestamp,
            event_type=EventType.UNKNOWN,
            tokens_in=ptx.tokens_in,
            tokens_out=[],
            fee_token=ptx.fee_token,
            fee_amount=ptx.fee_amount,
            notes="Tokens received from staking contract with no recognised event — review manually (possible principal withdrawal).",
        )

    # ── Swap on DEX ─────────────────────────────────────────────────────────
    if _touches_dex(ptx) and has_in and has_out:
        symbols_in = {f.symbol for f in ptx.tokens_in}
        symbols_out = {f.symbol for f in ptx.tokens_out}
        # Same token in and out = liquidity rebalance; different = actual swap
        if symbols_in != symbols_out:
            return TaxEvent(
                tx_hash=ptx.tx_hash,
                timestamp=ptx.timestamp,
                event_type=EventType.SWAP,
                tokens_in=ptx.tokens_in,
                tokens_out=ptx.tokens_out,
                fee_token=ptx.fee_token,
                fee_amount=ptx.fee_amount,
                notes="DEX swap — capital gain/loss on disposed token (FIFO cost basis).",
            )

    # ── Tokens in only (no tokens out except fees) ──────────────────────────
    if has_in and not has_out:
        if _touches_defi_income(ptx) or _touches_staking(ptx):
            return TaxEvent(
                tx_hash=ptx.tx_hash,
                timestamp=ptx.timestamp,
                event_type=EventType.DEFI_INCOME,
                tokens_in=ptx.tokens_in,
                tokens_out=[],
                fee_token=ptx.fee_token,
                fee_amount=ptx.fee_amount,
                notes="DeFi yield / interest — taxable as income at FMV on receipt.",
            )
        # Received from unknown address — could be airdrop or purchase
        return TaxEvent(
            tx_hash=ptx.tx_hash,
            timestamp=ptx.timestamp,
            event_type=EventType.RECEIVE,
            tokens_in=ptx.tokens_in,
            tokens_out=[],
            fee_token=ptx.fee_token,
            fee_amount=ptx.fee_amount,
            notes="Incoming transfer — establishes cost basis. Verify if airdrop (taxable) or purchase.",
        )

    # ── Tokens out only ──────────────────────────────────────────────────────
    if has_out and not has_in:
        if not ptx.touched_contracts & ALL_PROTOCOL_CONTRACTS:
            return TaxEvent(
                tx_hash=ptx.tx_hash,
                timestamp=ptx.timestamp,
                event_type=EventType.SEND,
                tokens_in=[],
                tokens_out=ptx.tokens_out,
                fee_token=ptx.fee_token,
                fee_amount=ptx.fee_amount,
                notes="Outgoing transfer — treated as disposal; capital gain/loss calculated.",
            )
        # Delegating STRK to the native staking contract — non-taxable lock-up.
        # The tokens remain yours; only the rewards are taxable (as STAKING_INCOME).
        # Note: when you eventually unstake, the returned STRK will appear as RECEIVE
        # from the pool contract — verify it doesn't create a duplicate cost basis.
        if _touches_staking(ptx):
            return TaxEvent(
                tx_hash=ptx.tx_hash,
                timestamp=ptx.timestamp,
                event_type=EventType.STAKE_DEPOSIT,
                tokens_in=[],
                tokens_out=ptx.tokens_out,
                fee_token=ptx.fee_token,
                fee_amount=ptx.fee_amount,
                notes="STRK staked/delegated — non-taxable lock-up. Tokens remain in your FIFO inventory.",
            )
        # Sending to some other protocol without receiving anything back
        return TaxEvent(
            tx_hash=ptx.tx_hash,
            timestamp=ptx.timestamp,
            event_type=EventType.UNKNOWN,
            tokens_in=[],
            tokens_out=ptx.tokens_out,
            fee_token=ptx.fee_token,
            fee_amount=ptx.fee_amount,
            notes="Tokens sent to protocol with no tokens received — review manually (LP deposit, collateral, etc.).",
        )

    # ── Single-token exchange via unlisted contract ──────────────────────────
    # One distinct token out, one distinct token in, different symbols = swap routed
    # through a bridge / aggregator / migration contract not in DEX_CONTRACTS.
    # Taxable as SWAP (disposal at FMV triggers CGT), same as a DEX swap.
    if has_in and has_out:
        symbols_in  = {f.symbol for f in ptx.tokens_in}
        symbols_out = {f.symbol for f in ptx.tokens_out}
        if len(symbols_in) == 1 and len(symbols_out) == 1 and symbols_in != symbols_out:
            return TaxEvent(
                tx_hash=ptx.tx_hash,
                timestamp=ptx.timestamp,
                event_type=EventType.SWAP,
                tokens_in=ptx.tokens_in,
                tokens_out=ptx.tokens_out,
                fee_token=ptx.fee_token,
                fee_amount=ptx.fee_amount,
                notes=(
                    "Token exchange via unlisted contract (bridge / aggregator) — "
                    "treated as swap; capital gain/loss on disposed token (FIFO)."
                ),
            )

    # ── Multi-token flows (LP add/remove, complex DeFi) ─────────────────────
    if has_in and has_out:
        return TaxEvent(
            tx_hash=ptx.tx_hash,
            timestamp=ptx.timestamp,
            event_type=EventType.UNKNOWN,
            tokens_in=ptx.tokens_in,
            tokens_out=ptx.tokens_out,
            fee_token=ptx.fee_token,
            fee_amount=ptx.fee_amount,
            notes="Multi-token DeFi interaction (LP, collateral, etc.) — review manually.",
        )

    # ── Fee only ─────────────────────────────────────────────────────────────
    if not has_in and not has_out:
        return TaxEvent(
            tx_hash=ptx.tx_hash,
            timestamp=ptx.timestamp,
            event_type=EventType.FEE_ONLY,
            tokens_in=[],
            tokens_out=[],
            fee_token=ptx.fee_token,
            fee_amount=ptx.fee_amount,
            notes="Gas fee only — no token movement (approval or failed inner call).",
        )

    return TaxEvent(
        tx_hash=ptx.tx_hash,
        timestamp=ptx.timestamp,
        event_type=EventType.UNKNOWN,
        tokens_in=ptx.tokens_in,
        tokens_out=ptx.tokens_out,
        fee_token=ptx.fee_token,
        fee_amount=ptx.fee_amount,
        notes="Could not classify — review manually.",
    )


def classify_all(transactions: list[ParsedTransaction]) -> list[TaxEvent]:
    return [classify(ptx) for ptx in transactions]
