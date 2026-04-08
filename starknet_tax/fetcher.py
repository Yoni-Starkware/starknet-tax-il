"""
Fetch transaction history via StarkNet JSON-RPC (Alchemy, Blast, Nethermind, etc.)

Strategy:
  1. Date → block number: pure binary search (~23 RPC calls, O(log N)).
  2. Account event scan: starknet_getEvents with address=USER_ADDRESS returns all
     events emitted by the account contract (Argent, Braavos, OZ all emit at least
     one event per executed transaction).  This correctly discovers every user-
     initiated transaction regardless of which token or protocol is used.
     NOTE: Key-based filtering (key[1]/key[2] = user address) is NOT used because
     most StarkNet ERC-20 tokens (STRK, USDC, USDT, etc.) use the legacy Cairo-0
     Transfer event encoding where from/to addresses are in event data, not keys.
  3. Staking contract: additionally scanned for PoolMemberRewardClaimed /
     StakerRewardClaimed in case rewards are auto-claimed by a third party rather
     than by the user themselves.
  4. All unique tx hashes → starknet_getTransactionReceipt → parse all events.
     Both Cairo-0 (addresses in data) and Cairo-1 (addresses in keys) Transfer
     event formats are supported in the receipt parsing step.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import requests
from tqdm import tqdm

from .config import (
    ADDRESS_TO_TOKEN,
    IGNORED_TOKEN_CONTRACTS,
    POOL_MEMBER_EXIT_ACTION_SELECTOR,
    POOL_MEMBER_REWARD_CLAIMED_SELECTOR,
    STAKER_REWARD_CLAIMED_SELECTOR,
    PUBLIC_RPC_URLS,
    STAKING_CONTRACT,
    TOKEN_DECIMALS,
    TRANSFER_SELECTOR,
    _sn_keccak,
)

# All known tokens (for Transfer event parsing in receipts)
ETH_CONTRACT = "0x049d36570d4e46f48e99674bd3fcc84644ddd6b96f7c741b1562b82f9e004dc7"

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

    def get_events(
        self,
        from_block: int,
        to_block: int,
        address: Optional[str],
        keys: list[list[str]],
        chunk_size: int = 1000,
        continuation_token: Optional[str] = None,
    ) -> dict:
        f: dict = {
            "from_block": {"block_number": from_block},
            "to_block": {"block_number": to_block},
            "chunk_size": chunk_size,
        }
        if keys:
            f["keys"] = keys
        if address:
            f["address"] = address
        if continuation_token:
            f["continuation_token"] = continuation_token
        return self._call("starknet_getEvents", {"filter": f}, timeout=60) or {}


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


# ── Date → block conversion ───────────────────────────────────────────────────

def _block_ts(rpc: RpcClient, block_number: int) -> int:
    return rpc.get_block(block_number).get("timestamp", 0)


def _find_block_for_date(rpc: RpcClient, target: date, latest_block: int) -> int:
    """
    Binary search for the first block whose timestamp >= midnight of target date.
    Uses ~23 RPC calls (log2 of StarkNet's block count).
    """
    target_ts = int(
        datetime(target.year, target.month, target.day, tzinfo=timezone.utc).timestamp()
    )
    latest_ts = _block_ts(rpc, latest_block)
    if latest_ts <= target_ts:
        return latest_block

    lo, hi = 0, latest_block
    while lo < hi:
        mid = (lo + hi) // 2
        if _block_ts(rpc, mid) < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ── Event scanning ────────────────────────────────────────────────────────────

def _drain_events(
    rpc: RpcClient,
    from_block: int,
    to_block: int,
    address: Optional[str],
    keys: list[list[str]],
    label: str = "",
) -> list[dict]:
    """
    Collect all events matching the filter over the full block range.
    Follows continuation tokens until exhausted.
    Prints a one-line progress update for visibility.
    """
    events: list[dict] = []
    continuation = None
    pages = 0
    t0 = time.time()
    while True:
        result = rpc.get_events(from_block, to_block, address, keys,
                                chunk_size=1000, continuation_token=continuation)
        batch = result.get("events", [])
        events.extend(batch)
        continuation = result.get("continuation_token")
        pages += 1
        if not continuation:
            break
        time.sleep(0.05)
    elapsed = time.time() - t0
    if events or pages > 2:
        tqdm.write(f"    {label}: {len(events)} events in {pages} pages ({elapsed:.1f}s)")
    return events


def _get_user_tx_hashes(
    rpc: RpcClient,
    user_addr: str,
    from_block: int,
    to_block: int,
) -> set[str]:
    """
    Find all transactions initiated by the user by scanning events emitted
    BY the account contract itself.

    Every account contract (Argent X, Braavos, OpenZeppelin) emits at least
    one event per executed transaction, so this discovers all transactions
    where the user was the sender.

    NOTE: this does NOT discover transactions initiated by a third party that
    spent an allowance you granted (e.g. a DEX pulling tokens via transferFrom).
    Those are caught separately by _scan_token_transfers(), which scans Transfer
    events and finds any tx where the user's address appears as from/to.
    """
    events = _drain_events(
        rpc, from_block, to_block,
        address=user_addr,
        keys=[],   # no key filter — collect ALL events from this contract
        label="account-txs",
    )
    tx_hashes = {ev["transaction_hash"] for ev in events}
    return tx_hashes


def _scan_token_transfers(
    rpc: RpcClient,
    user_addr: str,
    from_block: int,
    to_block: int,
) -> set[str]:
    """
    Find tx hashes where the user appears in a Transfer event from any known token
    contract.  Fallback for wallets whose account contracts do not emit per-tx
    events (OZ accounts, older Braavos/Argent versions).

    Encoding is detected dynamically per token by sampling one event:
    - Cairo-1 (keys = [selector, from, to]): RPC-side key filter — O(user events).
    - Cairo-0 (keys = [selector], data = [from, to, ...]): full scan with
      client-side matching.  No sleep between pages; RpcClient retries on 429.
      Progress is printed every 20 pages.
    If a token has no Transfer events in the block range it is skipped entirely.
    """
    tx_hashes: set[str] = set()

    for token_contract, symbol in ADDRESS_TO_TOKEN.items():
        t0 = time.time()

        # ── Detect encoding via one-event sample ────────────────────────────
        sample = rpc.get_events(from_block, to_block, token_contract,
                                [[TRANSFER_SELECTOR]], chunk_size=1)
        sample_events = sample.get("events", [])

        if not sample_events:
            tqdm.write(f"  {symbol}: no transfers in period, skipping")
            continue

        is_cairo1 = len(sample_events[0].get("keys", [])) >= 3

        if is_cairo1:
            # ── Cairo-1: key-filtered scan — only fetches the user's own events
            found: set[str] = set()
            for filt in [
                [[TRANSFER_SELECTOR], [user_addr]],       # keys[1] = from (outgoing)
                [[TRANSFER_SELECTOR], [], [user_addr]],   # keys[2] = to   (incoming)
            ]:
                for ev in _drain_events(rpc, from_block, to_block, token_contract,
                                        filt, label=f"{symbol}"):
                    found.add(ev["transaction_hash"])
            elapsed = time.time() - t0
            tqdm.write(f"  {symbol}: {len(found)} tx(s) via key filter (Cairo-1, {elapsed:.1f}s)")

        else:
            # ── Cairo-0: scan ALL Transfer events, match user address in data
            tqdm.write(f"  {symbol}: scanning all transfers (Cairo-0, from/to in data)...")
            found = set()
            continuation: Optional[str] = None
            pages = 0
            total_events = 0
            while True:
                result = rpc.get_events(
                    from_block, to_block,
                    address=token_contract,
                    keys=[[TRANSFER_SELECTOR]],
                    chunk_size=1000,
                    continuation_token=continuation,
                )
                batch = result.get("events", [])
                continuation = result.get("continuation_token")
                pages += 1
                total_events += len(batch)

                for ev in batch:
                    data = ev.get("data", [])
                    if len(data) >= 2 and (
                        _norm(data[0]) == user_addr or _norm(data[1]) == user_addr
                    ):
                        found.add(ev["transaction_hash"])

                if pages % 20 == 0:
                    elapsed = time.time() - t0
                    tqdm.write(
                        f"    {symbol}: page {pages} | {total_events:,} events scanned"
                        f" | {len(found)} matching ({elapsed:.0f}s)..."
                    )

                if not continuation:
                    break

            elapsed = time.time() - t0
            tqdm.write(
                f"  {symbol}: {len(found)} tx(s) found"
                f" ({total_events:,} events, {pages} pages, {elapsed:.1f}s)"
            )

        tx_hashes |= found

    return tx_hashes


def _scan_staking(
    rpc: RpcClient,
    user_addr: str,
    from_block: int,
    to_block: int,
    extra_pool_contracts: Optional[list[str]] = None,
) -> set[str]:
    """
    Return tx hashes of staking reward events for this user.

    Covers two roles:
    - Validator: StakerRewardClaimed on the main staking contract
    - Delegator: PoolMemberRewardClaimed on each delegation pool contract.
      Delegation pools are discovered by scanning the staking contract for
      PoolMemberStakeChanged events that reference the user, then scanning
      the resulting pool contracts.  Any extra pool addresses can be passed
      via extra_pool_contracts.
    """
    tx_hashes: set[str] = set()

    # 1. Validator rewards on the main staking contract
    events = _drain_events(rpc, from_block, to_block, STAKING_CONTRACT,
                           [[STAKER_REWARD_CLAIMED_SELECTOR]], "validator-rewards")
    for ev in events:
        vals = [_norm(v) for v in (ev.get("keys", [])[1:] + ev.get("data", []))]
        if user_addr in vals:
            tx_hashes.add(ev["transaction_hash"])

    # 2. Discover delegation pool contracts for this user.
    #    The staking contract emits events when pool member stake changes.
    #    We scan for any staking event that includes the user's address.
    POOL_MEMBER_SELECTORS = [
        POOL_MEMBER_REWARD_CLAIMED_SELECTOR,
        _sn_keccak("PoolMemberStakeChanged"),
        POOL_MEMBER_EXIT_ACTION_SELECTOR,
    ]
    pool_contracts: set[str] = set()

    # Scan all staking-contract events for any mention of the user (in any position)
    for selector in POOL_MEMBER_SELECTORS:
        events = _drain_events(rpc, from_block, to_block, STAKING_CONTRACT,
                               [[selector]], f"pool-discovery")
        for ev in events:
            vals = [_norm(v) for v in (ev.get("keys", [])[1:] + ev.get("data", []))]
            if user_addr in vals:
                # The event data usually includes the pool address — collect all addresses
                for v in vals:
                    if v and v != user_addr and len(v) > 10:
                        pool_contracts.add(v)

    if extra_pool_contracts:
        pool_contracts.update(_norm(p) for p in extra_pool_contracts)

    # 3. Scan discovered pool contracts for PoolMemberRewardClaimed
    if pool_contracts:
        print(f"  Found {len(pool_contracts)} delegation pool contract(s) — scanning rewards...")
        for pool_addr in pool_contracts:
            events = _drain_events(rpc, from_block, to_block, pool_addr,
                                   [[POOL_MEMBER_REWARD_CLAIMED_SELECTOR]],
                                   f"pool-rewards({pool_addr[:10]}...)")
            for ev in events:
                vals = [_norm(v) for v in (ev.get("keys", [])[1:] + ev.get("data", []))]
                if user_addr in vals:
                    tx_hashes.add(ev["transaction_hash"])

    return tx_hashes


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


# ── Dune Analytics ───────────────────────────────────────────────────────────

_DUNE_API_BASE = "https://api.dune.com/api/v1"


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
    resp.raise_for_status()
    query_id = resp.json()["query_id"]
    print(f"  Query created: {query_id}")

    # Step 2: Execute the query
    resp = session.post(
        f"{_DUNE_API_BASE}/query/{query_id}/execute",
        headers=headers,
        json={"performance": "large"},
        timeout=30,
    )
    resp.raise_for_status()
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
) -> list[ParsedTransaction]:
    """
    Fetch the receipt for every tx hash, parse token flows and fees.

    When skip_date_filter=True (CSV/Dune modes) all transactions are returned
    regardless of date so that process_events() can build a complete FIFO
    history.  When False (RPC mode) only transactions within [from_date,
    to_date] are returned (block binary-search may slightly overshoot).
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
            continue  # RPC binary search may slightly overshoot; skip out-of-range

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

    # Fail loudly if any unrecognised token contracts were encountered.
    _assert_no_unknown_transfers(unknown_transfers)

    parsed.sort(key=lambda p: p.timestamp)
    return parsed


# ── Main entrypoint ───────────────────────────────────────────────────────────

def fetch_transactions(
    address: str,
    from_date: date,
    to_date: date,
    rpc_url: Optional[str],
    delegation_pools: Optional[list[str]] = None,
    dune_api_key: Optional[str] = None,
) -> list[ParsedTransaction]:
    """
    Fetch and parse all transactions for `address`.

    Two discovery modes:
    - Dune mode (dune_api_key is set): fetches ALL-TIME tx hashes via Dune
      Analytics SQL, then fetches receipts via RPC.  No date filter on the
      returned transactions — process_events() gates the tax summary to
      [from_date, to_date] while still running FIFO over full history.
    - RPC mode (default): discovers txs via account-contract event scanning,
      token Transfer scanning, and staking contract scanning within
      [from_date, to_date] only.
    """
    norm_addr = _norm(address)
    rpc = _pick_rpc(rpc_url)

    if dune_api_key:
        # ── Dune mode — full history, no date filter ──────────────────────────
        print(f"Fetching all-time transactions from Dune Analytics for {address[:14]}...")
        tx_hashes: set[str] = fetch_tx_hashes_from_dune(address, dune_api_key)
        return _fetch_and_parse_receipts(
            rpc, norm_addr, tx_hashes, from_date, to_date, skip_date_filter=True
        )

    else:
        # ── RPC mode ─────────────────────────────────────────────────────────

        # Step 1: date → block range
        print("Finding block range for the given dates (binary search)...")
        latest = rpc.block_number()
        from_block = _find_block_for_date(rpc, from_date, latest)
        to_block   = min(_find_block_for_date(rpc, to_date + timedelta(days=1), latest), latest)

        from_ts_dt = datetime.fromtimestamp(_block_ts(rpc, from_block), tz=timezone.utc)
        to_ts_dt   = datetime.fromtimestamp(_block_ts(rpc, to_block),   tz=timezone.utc)
        print(f"  Block {from_block:,} ({from_ts_dt.date()})  →  Block {to_block:,} ({to_ts_dt.date()})")

        # Step 2a: account-contract events (Argent X, newer Braavos)
        print("\nScanning account contract events to discover transactions...")
        tx_hashes = _get_user_tx_hashes(rpc, norm_addr, from_block, to_block)
        print(f"  → {len(tx_hashes)} transaction(s) found via account events.")

        # Step 2b: token Transfer events (fallback for OZ / older wallets)
        print("\nScanning token Transfer events to find any missed transactions...")
        transfer_found = _scan_token_transfers(rpc, norm_addr, from_block, to_block)
        new_from_transfers = transfer_found - tx_hashes
        if new_from_transfers:
            print(f"  → {len(new_from_transfers)} additional transaction(s) found via token transfers.")
        tx_hashes |= transfer_found

        # Step 3: staking reward events (catches third-party auto-claims)
        print("\nScanning staking reward events...")
        staking_found = _scan_staking(rpc, norm_addr, from_block, to_block,
                                      extra_pool_contracts=delegation_pools)
        if staking_found:
            print(f"  → {len(staking_found)} additional staking reward transaction(s).")
        tx_hashes |= staking_found

        print(
            f"\nNote: incoming transfers sent directly to your address by third parties "
            f"(airdrops, direct sends) are not automatically discovered. "
            f"Token flows within your own transactions are fully captured."
        )

    if not tx_hashes:
        print("No transactions found for this address in the given period.")
        return []

    return _fetch_and_parse_receipts(rpc, norm_addr, tx_hashes, from_date, to_date)
