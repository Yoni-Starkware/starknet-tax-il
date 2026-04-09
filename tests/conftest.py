"""
Shared helpers for building ParsedTransaction fixtures (no RPC).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from starknet_tax.config import TOKEN_DECIMALS
from starknet_tax.fetcher import ParsedTransaction, TokenFlow


def token_addr(symbol: str) -> str:
    """Canonical token contract address for a known symbol."""
    from starknet_tax.config import ADDRESS_TO_TOKEN

    return next(k for k, v in ADDRESS_TO_TOKEN.items() if v == symbol)


def token_flow(symbol: str, amount: str | Decimal | float) -> TokenFlow:
    amt = Decimal(str(amount))
    dec = TOKEN_DECIMALS.get(symbol, 18)
    raw = int(amt * (10**dec))
    return TokenFlow(
        symbol=symbol,
        amount=amt,
        raw_amount=raw,
        contract_address=token_addr(symbol),
    )


def make_ptx(
    *,
    tokens_in: list[TokenFlow] | None = None,
    tokens_out: list[TokenFlow] | None = None,
    touched: set[str] | None = None,
    raw_events: list[dict] | None = None,
    tx_hash: str = "0x1",
    fee_token: str = "ETH",
    fee_amount: Decimal = Decimal("0"),
    ts: datetime | None = None,
    block_number: int = 1_000_000,
) -> ParsedTransaction:
    return ParsedTransaction(
        tx_hash=tx_hash,
        timestamp=ts or datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
        block_number=block_number,
        tokens_in=tokens_in or [],
        tokens_out=tokens_out or [],
        touched_contracts=set(touched or []),
        fee_token=fee_token,
        fee_amount=fee_amount,
        raw_events=raw_events or [],
    )
