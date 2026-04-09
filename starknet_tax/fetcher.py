"""
Fetch transaction history: Dune Analytics discovers tx hashes, JSON-RPC loads receipts.

Strategy:
  1. Dune SQL (requires API key) returns the union of user-initiated txs and
     transfers on known token contracts — full wallet history for correct FIFO.
  2. For each hash, starknet_getTransactionReceipt parses all events.
     Cairo-0 (addresses in data) and Cairo-1 (addresses in keys) Transfer
     formats are supported.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import requests
from tqdm import tqdm

from .config import (
    ADDRESS_TO_TOKEN,
    IGNORED_TOKEN_CONTRACTS,
    PUBLIC_RPC_URLS,
    TOKEN_DECIMALS,
    TRANSFER_SELECTOR,
)

# StarkNet sequencer fee vault — every transaction ends with a Transfer of
# fee_token from the user to this address.  We skip it when building tokens_out
# because the fee is already captured separately in ptx.fee_amount.
_FEE_VAULT = "0x1176a1bd84444c89232ec27754698e5d2e7e1a7f1539f12027f28b23ec9f3d8"


# ── Data types ───────────────────────────────────────────────────────────────

@dataclass
class TokenFlow:
    symbol: str
    amount: Decimal
    raw_amount: int
    contract_address: str


@dataclass
class ParsedTransaction:
    tx_hash: str
    timestamp: datetime
    block_number: int
    tokens_in: list[TokenFlow] = field(default_factory=list)
    tokens_out: list[TokenFlow] = field(default_factory=list)
    touched_contracts: set[str] = field(default_factory=set)
    fee_token: str = "ETH"
    fee_amount: Decimal = Decimal(0)
    user_is_sender: bool = False
    raw_events: list[dict] = field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _norm(addr: str) -> str:
    if not addr:
        return ""
    try:
        return "0x" + hex(int(addr, 16))[2:].lower()
    except (ValueError, TypeError):
        return addr.lower()


def _u256(low: str, high: str) -> int:
    return int(low, 16) + (int(high, 16) << 128)


def _to_decimal(raw: int, symbol: str) -> Decimal:
    return Decimal(raw) / Decimal(10 ** TOKEN_DECIMALS.get(symbol, 18))


# ── RPC client ───────────────────────────────────────────────────────────────

class RpcClient:
    def __init__(self, url: str):
        self.url = url
        self._session = requests.Session()
        self._id = 0

    def _call(self, method: str, params, timeout: int = 60) -> object:
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": self._id}
        for attempt in range(5):
            try:
                resp = self._session.post(self.url, json=payload, timeout=timeout)
                # Treat 429 and 5xx as transient — back off and retry
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = min(30, 5 * (2 ** attempt))
                    tqdm.write(f"  HTTP {resp.status_code} — retrying in {wait}s "
                               f"(attempt {attempt+1}/5)...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    msg = str(data["error"])
                    if "rate" in msg.lower() or data["error"].get("code") == 429:
                        wait = min(30, 5 * (2 ** attempt))
                        tqdm.write(f"  RPC rate limit — retrying in {wait}s...")
                        time.sleep(wait)
                        continue
                    raise RuntimeError(f"RPC error: {data['error']}")
                return data.get("result")
            except requests.exceptions.Timeout:
                if attempt == 4:
                    raise
                tqdm.write(f"  Request timed out (attempt {attempt+1}/5), retrying...")
                time.sleep(5 * (attempt + 1))
            except requests.RequestException as exc:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        return None

    def block_number(self) -> int:
        return int(self._call("starknet_blockNumber", []))

    def get_block(self, block_number: int) -> dict:
        return self._call(
            "starknet_getBlockWithTxHashes",
            {"block_id": {"block_number": block_number}},
        ) or {}

    def get_receipt(self, tx_hash: str) -> dict:
        return self._call(
            "starknet_getTransactionReceipt",
            {"transaction_hash": tx_hash},
        ) or {}


def _pick_rpc(override: Optional[str]) -> RpcClient:
    urls = [override] if override else PUBLIC_RPC_URLS
    for url in urls:
        try:
            client = RpcClient(url)
            client.block_number()
            return client
        except Exception:
            continue
    raise RuntimeError(
        "Could not connect to any StarkNet RPC.\n"
        "Pass your Alchemy URL via --rpc-url or the STARKNET_RPC_URL env var.\n"
        "  Example: --rpc-url https://starknet-mainnet.g.alchemy.com/starknet/version/rpc/v0_10/YOUR_KEY"
    )


# ── Block timestamps (for receipt parsing) ─────────────────────────────────────

def _block_ts(rpc: RpcClient, block_number: int) -> int:
    return rpc.get_block(block_number).get("timestamp", 0)


# ── Transfer event parsing ────────────────────────────────────────────────────

def _parse_transfer(
    event: dict,
    user_addr: str,
) -> Optional[tuple[TokenFlow, str, str]]:
    """
    Parse a Transfer event for a known token. Returns (flow, from_addr, to_addr) or None.

    Returns None only for two legitimate skip cases:
      - Event is not a Transfer (different selector) — caller filters by selector elsewhere.
      - Transfer amount is zero (on-chain spam / dust).

    All other anomalies (unknown contract, malformed data) are caught upstream by
    _assert_no_unknown_transfers() before this function is called, so any Transfer
    event that reaches here with a known contract MUST have well-formed data.
    Raises RuntimeError if the data is unexpectedly malformed.
    """
    keys = event.get("keys", [])
    data = event.get("data", [])
    token_contract = _norm(event.get("from_address", ""))

    if not keys or _norm(keys[0]) != TRANSFER_SELECTOR:
        return None  # not a Transfer event — legitimate skip

    symbol = ADDRESS_TO_TOKEN.get(token_contract)
    if not symbol:
        return None  # unknown contract — already caught by _assert_no_unknown_transfers

    if len(keys) >= 3:
        # Cairo-1: keys = [selector, from, to], data = [amount_low, amount_high]
        if len(data) < 2:
            raise RuntimeError(
                f"Malformed Cairo-1 Transfer event for {symbol} ({token_contract}): "
                f"expected ≥2 data elements for u256 amount, got {len(data)}. "
                f"keys={keys} data={data}"
            )
        from_addr = _norm(keys[1])
        to_addr = _norm(keys[2])
        raw = _u256(data[0], data[1])
    else:
        # Cairo-0: keys = [selector], data = [from, to, amount_low, amount_high]
        if len(data) < 4:
            raise RuntimeError(
                f"Malformed Cairo-0 Transfer event for {symbol} ({token_contract}): "
                f"expected ≥4 data elements, got {len(data)}. "
                f"keys={keys} data={data}"
            )
        from_addr = _norm(data[0])
        to_addr = _norm(data[1])
        raw = _u256(data[2], data[3])

    if raw == 0:
        return None  # zero-amount transfer (on-chain spam) — legitimate skip

    return (
        TokenFlow(symbol=symbol, amount=_to_decimal(raw, symbol),
                  raw_amount=raw, contract_address=token_contract),
        from_addr,
        to_addr,
    )


def _assert_no_unknown_transfers(
    unknown: list[tuple[str, str]],
) -> None:
    """
    Raise RuntimeError if any Transfer events from unrecognised contracts were seen.

    `unknown` is a list of (tx_hash, contract_address) pairs collected during receipt
    parsing.  Groups by contract so the user sees the full picture in one error and
    can update config.py in a single pass.
    """
    if not unknown:
        return

    by_contract: dict[str, list[str]] = {}
    for tx_hash, contract in unknown:
        by_contract.setdefault(contract, []).append(tx_hash)

    lines = [
        "",
        "═" * 72,
        "UNKNOWN TOKEN CONTRACTS — report generation aborted.",
        "═" * 72,
        "",
        "The following contracts emitted ERC-20 Transfer events in your transactions",
        "but are not listed in ADDRESS_TO_TOKEN or IGNORED_TOKEN_CONTRACTS in config.py.",
        "",
        "You must make an explicit choice for each one:",
        "",
        "  Option A — track the token:",
        '    Add to ADDRESS_TO_TOKEN in config.py, e.g.:',
        '    "0xCONTRACT_ADDRESS": "SYMBOL",',
        "",
        "  Option B — explicitly ignore it (LP tokens, dust, internal reward tokens, etc.):",
        '    Add to IGNORED_TOKEN_CONTRACTS in config.py, e.g.:',
        '    "0xCONTRACT_ADDRESS",  # brief description of why it is ignored',
        "",
        "Unknown contracts:",
    ]
    for contract, txs in sorted(by_contract.items()):
        lines.append(f"  {contract}  ({len(txs)} transaction(s))")
        for tx in txs[:3]:
            lines.append(f"    tx: {tx}")
        if len(txs) > 3:
            lines.append(f"    ... and {len(txs) - 3} more")
    lines += ["", "═" * 72]
    raise RuntimeError("\n".join(lines))


def _warn_unknown_transfers_ignored(
    unknown: list[tuple[str, str]],
) -> None:
    """Print a visible warning when --ignore-unknown-tokens skips unlisted contracts."""
    if not unknown:
        return

    by_contract: dict[str, list[str]] = {}
    for tx_hash, contract in unknown:
        by_contract.setdefault(contract, []).append(tx_hash)

    lines = [
        "",
        "─" * 72,
        "WARNING: ignoring Transfer events from unknown token contracts "
        "(--ignore-unknown-tokens).",
        "─" * 72,
        "",
        "These flows are omitted from the report. FIFO and tax figures may be wrong "
        "if any material balance involved these tokens. Add contracts to "
        "ADDRESS_TO_TOKEN or IGNORED_TOKEN_CONTRACTS in config.py when possible.",
        "",
        "Ignored contracts:",
    ]
    for contract, txs in sorted(by_contract.items()):
        lines.append(f"  {contract}  ({len(txs)} transaction(s))")
        for tx in txs[:3]:
            lines.append(f"    tx: {tx}")
        if len(txs) > 3:
            lines.append(f"    ... and {len(txs) - 3} more")
    lines += ["", "─" * 72]
    print("\n".join(lines))


# ── Dune Analytics ───────────────────────────────────────────────────────────

_DUNE_API_BASE = "https://api.dune.com/api/v1"


def _dune_performance_tier() -> str:
    """medium (default) or large — must be sent as a query param, not JSON body."""
    tier = os.environ.get("DUNE_PERFORMANCE", "medium").strip().lower()
    return tier if tier in ("medium", "large") else "medium"


def _dune_raise(resp: requests.Response, what: str) -> None:
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        try:
            detail = resp.json()
        except Exception:
            detail = (resp.text or "")[:800]
        raise RuntimeError(f"Dune API error ({what}): HTTP {resp.status_code} — {detail}") from e


def fetch_tx_hashes_from_dune(address: str, api_key: str) -> set[str]:
    """
    Fetch ALL transaction hashes involving this StarkNet address via Dune Analytics.
    No date filter — returns full history so FIFO cost basis is complete.

    Two sources:
    1. starknet.transactions WHERE sender_address = address
       (user-initiated transactions, outgoing)
    2. starknet.events WHERE from_address IN (known token contracts)
       AND Transfer selector AND user address in keys or data
       (incoming transfers — airdrops, DEX receipts, staking rewards)

    Requires a Dune API key (free tier works, subject to rate limits).
    """
    norm = _norm(address)
    # Dune stores StarkNet values as varbinary — use unquoted hex literals (0x...).
    # Pad to 32 bytes (64 hex chars) since that's Dune's canonical felt252 format.
    def _hex(addr: str) -> str:
        return "0x" + format(int(addr, 16), "064x")

    wallet_hex = _hex(address)
    transfer_sel_hex = _hex(TRANSFER_SELECTOR)
    contracts_hex = ", ".join(_hex(c) for c in ADDRESS_TO_TOKEN.keys())

    # element_at(arr, n) is null-safe (no out-of-bounds error), unlike arr[n].
    # We skip the Transfer selector filter: we already filter by known ERC-20
    # contract addresses, and scanning the selector adds per-row array access
    # with no index benefit.  The address filter on from_address is indexed.
    sql = f"""
SELECT DISTINCT hash AS tx_hash
FROM starknet.transactions
WHERE sender_address = {wallet_hex}

UNION

SELECT DISTINCT transaction_hash AS tx_hash
FROM starknet.events
WHERE from_address IN ({contracts_hex})
  AND (
      element_at(keys, 2) = {wallet_hex} OR element_at(keys, 3) = {wallet_hex}
      OR element_at(data, 1) = {wallet_hex} OR element_at(data, 2) = {wallet_hex}
  )
"""

    session = requests.Session()
    headers = {"X-Dune-API-Key": api_key, "Content-Type": "application/json"}

    # Step 1: Create a (private) query
    print("  Creating Dune query...")
    resp = session.post(
        f"{_DUNE_API_BASE}/query",
        headers=headers,
        json={
            "name": f"starknet-tax-{norm[:10]}",
            "query_sql": sql,
            "is_private": True,
        },
        timeout=30,
    )
    if resp.status_code == 401:
        raise PermissionError(
            "Dune API key rejected (401). Check --dune-api-key / DUNE_API_KEY."
        )
    _dune_raise(resp, "create query")
    query_id = resp.json()["query_id"]
    print(f"  Query created: {query_id}")

    # Step 2: Execute — performance is a *query* parameter (see Dune OpenAPI), not JSON body.
    perf = _dune_performance_tier()
    resp = session.post(
        f"{_DUNE_API_BASE}/query/{query_id}/execute",
        headers=headers,
        params={"performance": perf},
        timeout=30,
    )
    _dune_raise(resp, "execute query")
    execution_id = resp.json()["execution_id"]
    print(f"  Execution started: {execution_id}")

    # Step 3: Poll until complete
    for attempt in range(120):  # up to ~10 minutes
        time.sleep(5)
        resp = session.get(
            f"{_DUNE_API_BASE}/execution/{execution_id}/status",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        status = resp.json().get("state", "")
        if attempt % 6 == 0:
            print(f"  Dune status: {status} (waiting...)")
        if status == "QUERY_STATE_COMPLETED":
            break
        if status in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"):
            error = resp.json().get("error", {}).get("message", status)
            raise RuntimeError(f"Dune query failed: {error}")
    else:
        raise TimeoutError("Dune query did not complete within 10 minutes.")

    # Step 4: Page through results
    tx_hashes: set[str] = set()
    offset = 0
    limit = 25_000
    while True:
        resp = session.get(
            f"{_DUNE_API_BASE}/execution/{execution_id}/results",
            headers=headers,
            params={"limit": limit, "offset": offset},
            timeout=60,
        )
        resp.raise_for_status()
        rows = resp.json().get("result", {}).get("rows", [])
        for row in rows:
            h = row.get("tx_hash", "")
            if h:
                tx_hashes.add(h)
        if len(rows) < limit:
            break
        offset += limit

    print(f"  Dune returned {len(tx_hashes)} unique transaction(s).")
    return tx_hashes


# ── Receipt fetching & parsing ────────────────────────────────────────────────

def _fetch_and_parse_receipts(
    rpc: RpcClient,
    norm_addr: str,
    tx_hashes: set[str],
    from_date: date,
    to_date: date,
    skip_date_filter: bool = False,
    ignore_unknown_tokens: bool = False,
) -> list[ParsedTransaction]:
    """
    Fetch the receipt for every tx hash, parse token flows and fees.

    When skip_date_filter=True (Dune: full history) all transactions are
    returned regardless of date so process_events() can build a complete FIFO
    history. When False, only transactions within [from_date, to_date] are kept.
    """
    print(f"\nFetching {len(tx_hashes)} transaction receipt(s)...")

    block_ts_cache: dict[int, int] = {}

    def get_block_ts(bn: int) -> int:
        if bn not in block_ts_cache:
            block_ts_cache[bn] = _block_ts(rpc, bn)
        return block_ts_cache[bn]

    parsed: list[ParsedTransaction] = []
    unknown_transfers: list[tuple[str, str]] = []  # (tx_hash, contract) for unrecognised tokens

    for tx_hash in tqdm(sorted(tx_hashes), desc="Receipts", unit="tx"):
        receipt = rpc.get_receipt(tx_hash)  # raises after all retries if RPC fails

        if receipt.get("execution_status") == "REVERTED":
            continue

        block_number = receipt.get("block_number", 0)
        ts_unix = get_block_ts(block_number)
        timestamp = datetime.fromtimestamp(ts_unix, tz=timezone.utc)

        if not skip_date_filter and not (from_date <= timestamp.date() <= to_date):
            continue

        events = receipt.get("events", [])

        # Collect any Transfer events from unrecognised contracts before parsing.
        # We accumulate across all receipts and raise once at the end so the user
        # can fix everything in config.py in a single pass.
        for event in events:
            keys = event.get("keys", [])
            if not keys or _norm(keys[0]) != TRANSFER_SELECTOR:
                continue
            contract = _norm(event.get("from_address", ""))
            if contract and contract not in ADDRESS_TO_TOKEN and contract not in IGNORED_TOKEN_CONTRACTS:
                unknown_transfers.append((tx_hash, contract))

        actual_fee = receipt.get("actual_fee", {})
        fee_raw = int(actual_fee.get("amount", "0x0"), 16)
        fee_unit = actual_fee.get("unit", "WEI")
        fee_token = "STRK" if fee_unit == "FRI" else "ETH"

        ptx = ParsedTransaction(
            tx_hash=tx_hash,
            timestamp=timestamp,
            block_number=block_number,
            fee_token=fee_token,
            fee_amount=_to_decimal(fee_raw, fee_token),
            user_is_sender=(_norm(receipt.get("sender_address", "")) == norm_addr),
            raw_events=events,
            touched_contracts={
                _norm(e.get("from_address", ""))
                for e in events if e.get("from_address")
            },
        )

        for event in events:
            result = _parse_transfer(event, norm_addr)
            if result is None:
                continue
            flow, from_addr, to_addr = result
            if to_addr == norm_addr:
                ptx.tokens_in.append(flow)
            elif from_addr == norm_addr and to_addr != _FEE_VAULT:
                ptx.tokens_out.append(flow)
            # else: fee transfer to sequencer vault — already in ptx.fee_amount, skip

        parsed.append(ptx)
        time.sleep(0.03)

    if unknown_transfers:
        if ignore_unknown_tokens:
            _warn_unknown_transfers_ignored(unknown_transfers)
        else:
            _assert_no_unknown_transfers(unknown_transfers)

    parsed.sort(key=lambda p: p.timestamp)
    return parsed


# ── Main entrypoint ───────────────────────────────────────────────────────────

def fetch_transactions(
    address: str,
    from_date: date,
    to_date: date,
    rpc_url: Optional[str],
    dune_api_key: str,
    *,
    ignore_unknown_tokens: bool = False,
) -> list[ParsedTransaction]:
    """
    Fetch and parse all transactions for `address`.

    Tx hashes come from Dune Analytics (all-time). Receipts are loaded via RPC.
    No date filter on returned transactions — process_events() limits the tax
    summary to [from_date, to_date] while FIFO runs over full history.

    If *ignore_unknown_tokens* is True, Transfer events from contracts not listed
    in config are skipped (with a console warning) instead of aborting.
    """
    norm_addr = _norm(address)
    rpc = _pick_rpc(rpc_url)

    print(f"Fetching all-time transactions from Dune Analytics for {address[:14]}...")
    tx_hashes: set[str] = fetch_tx_hashes_from_dune(address, dune_api_key)
    if not tx_hashes:
        print("No transactions found for this address in Dune.")
        return []

    return _fetch_and_parse_receipts(
        rpc,
        norm_addr,
        tx_hashes,
        from_date,
        to_date,
        skip_date_filter=True,
        ignore_unknown_tokens=ignore_unknown_tokens,
    )
