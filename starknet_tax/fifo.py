"""
FIFO cost-basis tracker (Israeli tax law mandated method).

Rules applied:
- Oldest acquired lots are disposed of first (FIFO).
- Cost basis for each lot = ILS value at acquisition time.
- For income events (staking rewards, DeFi yield): the ILS FMV at receipt
  becomes the cost basis for the newly acquired tokens.
- Capital losses offset gains in the same year and carry forward.
- Gas fees on disposal transactions are added to the acquisition cost
  of the received asset (or deducted from proceeds on a pure send).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP


@dataclass
class Lot:
    """A single acquisition lot."""
    acquired_at: datetime
    amount: Decimal          # token amount (human-readable)
    cost_basis_ils: Decimal  # total ILS paid for this lot
    tx_hash: str             # acquisition transaction

    @property
    def unit_cost(self) -> Decimal:
        if self.amount == 0:
            return Decimal(0)
        return self.cost_basis_ils / self.amount


@dataclass
class DisposalResult:
    """Result of disposing (selling/swapping/sending) some amount of a token."""
    symbol: str
    amount_disposed: Decimal
    proceeds_ils: Decimal
    cost_basis_ils: Decimal
    gain_loss_ils: Decimal    # positive = gain, negative = loss
    lots_used: list[tuple[Lot, Decimal, Decimal]]  # (lot, amount_from_lot, cost_from_lot)
    disposal_tx: str
    disposal_date: datetime
    is_short_term: bool = True  # Israel has no long/short distinction, kept for info

    # lots_used tuples: (lot, amount_from_lot, cost_from_lot)
    # cost_from_lot is the ILS cost actually charged from that lot (authoritative).


class FIFOTracker:
    """
    Tracks FIFO cost basis for multiple token types simultaneously.
    """

    def __init__(self) -> None:
        # symbol → deque of Lot (oldest first)
        self._lots: dict[str, deque[Lot]] = {}
        # symbol → total tokens held (for sanity checks)
        self._balance: dict[str, Decimal] = {}

    def acquire(
        self,
        symbol: str,
        amount: Decimal,
        price_ils: Decimal,   # ILS per token at acquisition
        acquired_at: datetime,
        tx_hash: str,
    ) -> None:
        """Record acquisition of `amount` tokens at `price_ils` per token."""
        if amount <= 0:
            return
        cost_basis_ils = (amount * price_ils).quantize(Decimal("0.01"), ROUND_HALF_UP)
        lot = Lot(
            acquired_at=acquired_at,
            amount=amount,
            cost_basis_ils=cost_basis_ils,
            tx_hash=tx_hash,
        )
        if symbol not in self._lots:
            self._lots[symbol] = deque()
            self._balance[symbol] = Decimal(0)
        self._lots[symbol].append(lot)
        self._balance[symbol] += amount

    def dispose(
        self,
        symbol: str,
        amount: Decimal,
        proceeds_ils: Decimal,    # total ILS received for `amount` tokens
        disposed_at: datetime,
        tx_hash: str,
    ) -> DisposalResult:
        """
        Dispose of `amount` tokens using FIFO.
        Returns a DisposalResult with the gain/loss.
        If we have fewer tokens than `amount` (e.g. missing history),
        we create a synthetic lot at zero cost and note it.
        """
        lots_queue = self._lots.get(symbol, deque())
        remaining = amount
        total_cost = Decimal(0)
        lots_used: list[tuple[Lot, Decimal]] = []

        while remaining > 0 and lots_queue:
            lot = lots_queue[0]
            if lot.amount <= remaining:
                # Use entire lot
                used = lot.amount
                used_cost = lot.cost_basis_ils
                total_cost += used_cost
                lots_used.append((lot, used, used_cost))
                lots_queue.popleft()
                remaining -= used
            else:
                # Use partial lot
                fraction = remaining / lot.amount
                used_cost = (lot.cost_basis_ils * fraction).quantize(Decimal("0.01"), ROUND_HALF_UP)
                total_cost += used_cost
                lots_used.append((lot, remaining, used_cost))
                lot.amount -= remaining
                lot.cost_basis_ils -= used_cost
                remaining = Decimal(0)

        if remaining > 0:
            # We have more disposed than tracked — missing acquisition history.
            # Create a synthetic zero-cost lot and flag it.
            synthetic_cost = Decimal(0)
            total_cost += synthetic_cost
            print(
                f"  Warning: disposing {remaining:.6f} {symbol} with no acquisition record "
                f"(tx {tx_hash[:12]}...). Cost basis set to 0 — review manually."
            )

        gain_loss = (proceeds_ils - total_cost).quantize(Decimal("0.01"), ROUND_HALF_UP)

        # Update balance
        disposed = amount - remaining  # actual amount we had
        self._balance[symbol] = self._balance.get(symbol, Decimal(0)) - disposed

        return DisposalResult(
            symbol=symbol,
            amount_disposed=amount,
            proceeds_ils=proceeds_ils,
            cost_basis_ils=total_cost,
            gain_loss_ils=gain_loss,
            lots_used=lots_used,
            disposal_tx=tx_hash,
            disposal_date=disposed_at,
        )

    def balance(self, symbol: str) -> Decimal:
        return self._balance.get(symbol, Decimal(0))

    def all_balances(self) -> dict[str, Decimal]:
        return {s: b for s, b in self._balance.items() if b > 0}
