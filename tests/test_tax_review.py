"""Tax layer: manual-review flags and missing-price failures."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import cast

import pytest

from starknet_tax.classifier import EventType, TaxEvent
from starknet_tax.pricing import PriceCache
from starknet_tax.tax import process_events
from tests.conftest import token_flow


class FakePriceCache:
    """Minimal duck-typed stand-in for PriceCache (no network)."""

    def __init__(self, missing_symbol: str | None = None) -> None:
        self._missing_symbol = missing_symbol

    def get(self, symbol: str, _target_date: date) -> Decimal | None:
        if self._missing_symbol is not None and symbol == self._missing_symbol:
            return None
        return Decimal("1")


def _unknown_event(ts: datetime, tx_hash: str = "0xunknown1") -> TaxEvent:
    return TaxEvent(
        tx_hash=tx_hash,
        timestamp=ts,
        event_type=EventType.UNKNOWN,
        tokens_in=[],
        tokens_out=[],
        fee_token="ETH",
        fee_amount=Decimal("0"),
        notes="Review me.",
    )


class TestUnknownReviewFlags:
    def test_in_period_unknown_is_flagged_for_manual_review(self) -> None:
        ev = _unknown_event(datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
        processed, summary = process_events(
            [ev],
            cast(PriceCache, FakePriceCache()),
            date(2025, 1, 1),
            date(2025, 12, 31),
        )
        assert ev.tx_hash in summary.needs_manual_review
        assert len(processed) == 1
        assert processed[0].needs_review is True

    def test_out_of_period_unknown_not_in_needs_manual_review(self) -> None:
        """Current behavior: review list only covers events inside the report window."""
        ev = _unknown_event(
            datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            tx_hash="0xold",
        )
        processed, summary = process_events(
            [ev],
            cast(PriceCache, FakePriceCache()),
            date(2025, 1, 1),
            date(2025, 12, 31),
        )
        assert summary.needs_manual_review == []
        assert processed == []


class TestMissingPrice:
    def test_missing_price_raises_for_in_period_event(self) -> None:
        ev = TaxEvent(
            tx_hash="0xmiss",
            timestamp=datetime(2025, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
            event_type=EventType.SEND,
            tokens_in=[],
            tokens_out=[token_flow("STRK", "1")],
            fee_token="ETH",
            fee_amount=Decimal("0"),
        )
        with pytest.raises(RuntimeError) as exc_info:
            process_events(
                [ev],
                cast(PriceCache, FakePriceCache(missing_symbol="STRK")),
                date(2025, 1, 1),
                date(2025, 12, 31),
            )
        msg = str(exc_info.value)
        assert "STRK" in msg
        assert "Missing" in msg or "missing" in msg.lower()
