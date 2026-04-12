"""
Historical ILS price lookup.

Strategy:
- DeFiLlama (coins.llama.fi) — free, no API key, returns USD prices.
  USD prices × USD/ILS rate = ILS price.
- Stablecoins (USDC, USDT, DAI): USD/ILS rate × 1.0 USD.
- USD/ILS rates: Yahoo Finance (USDILS=X), fetched in a single range call and
  cached per-day.  Note: these are market mid-rates, not the official Bank of
  Israel published rates.  For highest accuracy, verify against boi.org.il.
- If DeFiLlama has no data for a token, the run fails with a clear error.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import requests

from .config import COINGECKO_IDS, LIQUID_STAKING_SOURCES, STABLECOINS, TOKEN_DECIMALS

DEFILLAMA_CHART_URL = "https://coins.llama.fi/chart/{coins}"


# ── USD/ILS rates (Yahoo Finance) ─────────────────────────────────────────────

_RATE_CACHE: dict[date, Decimal] = {}

_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/USDILS=X"
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _fetch_boi_range(from_date: date, to_date: date) -> dict[date, Decimal]:
    """
    Fetch daily USD/ILS rates from Yahoo Finance for the given date range.
    Populates _RATE_CACHE as a side-effect.
    Returns {date: rate} for every trading day in the range.
    """
    from_ts = int(datetime(from_date.year, from_date.month, from_date.day,
                           tzinfo=timezone.utc).timestamp())
    to_ts   = int(datetime(to_date.year, to_date.month, to_date.day,
                           23, 59, 59, tzinfo=timezone.utc).timestamp())
    try:
        resp = requests.get(
            _YAHOO_URL,
            params={"interval": "1d", "period1": from_ts, "period2": to_ts},
            headers=_YAHOO_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        chart = resp.json().get("chart", {}).get("result", [{}])[0]
        timestamps = chart.get("timestamp", [])
        closes     = chart.get("indicators", {}).get("quote", [{}])[0].get("close", [])

        result: dict[date, Decimal] = {}
        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            d    = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            rate = Decimal(str(round(close, 4)))
            result[d] = rate
            _RATE_CACHE[d] = rate
        return result

    except Exception as exc:
        print(f"  Warning: Yahoo Finance USD/ILS fetch failed ({exc}); falling back to per-day.")
        return {}


def _boi_rate(target_date: date) -> Optional[Decimal]:
    """Return USD/ILS rate for a single date, fetching a narrow window if not cached."""
    if target_date in _RATE_CACHE:
        return _RATE_CACHE[target_date]
    # Fetch a 7-day window (handles weekends / holidays)
    rates = _fetch_boi_range(
        target_date - timedelta(days=3),
        target_date + timedelta(days=3),
    )
    return rates.get(target_date)


def _nearest_boi_rate(target_date: date) -> Optional[Decimal]:
    """Return the USD/ILS rate for target_date or the nearest cached date within ±3 days."""
    if target_date in _RATE_CACHE:
        return _RATE_CACHE[target_date]
    for delta in range(1, 4):
        for d in (target_date - timedelta(days=delta), target_date + timedelta(days=delta)):
            if d in _RATE_CACHE:
                return _RATE_CACHE[d]
    return _boi_rate(target_date)


# ── DeFiLlama (primary, free) ─────────────────────────────────────────────────

_DEFILLAMA_MAX_POINTS = 500  # API limit: num_coins × span ≤ 500


def _fetch_defillama_usd_batch(
    symbols: list[str],
    from_date: date,
    to_date: date,
    session: requests.Session,
) -> dict[str, dict[date, Decimal]]:
    """
    Fetch daily USD prices for multiple tokens from DeFiLlama.
    Splits the date range into chunks so num_coins × span ≤ 500 per request.
    Returns {symbol: {date: usd_price}}.
    """
    coin_keys: dict[str, str] = {}  # "coingecko:id" → symbol
    for symbol in symbols:
        cg_id = COINGECKO_IDS.get(symbol)
        if cg_id:
            coin_keys[f"coingecko:{cg_id}"] = symbol

    if not coin_keys:
        return {}

    n_coins   = len(coin_keys)
    chunk_days = max(1, _DEFILLAMA_MAX_POINTS // n_coins)
    coins_str  = ",".join(coin_keys)

    result: dict[str, dict[date, Decimal]] = {s: {} for s in coin_keys.values()}

    chunk_start = from_date
    while chunk_start <= to_date:
        span = min(chunk_days, (to_date - chunk_start).days + 1)
        from_ts = int(datetime(chunk_start.year, chunk_start.month, chunk_start.day,
                               tzinfo=timezone.utc).timestamp())
        params = {"start": from_ts, "span": span, "period": "1d", "searchWidth": 43200}

        for attempt in range(3):
            try:
                resp = session.get(
                    DEFILLAMA_CHART_URL.format(coins=coins_str),
                    params=params, timeout=30,
                )
                if resp.status_code == 429:
                    time.sleep(10)
                    continue
                resp.raise_for_status()
                coins_data = resp.json().get("coins", {})
                for coin_key, symbol in coin_keys.items():
                    for point in coins_data.get(coin_key, {}).get("prices", []):
                        d = datetime.fromtimestamp(point["timestamp"], tz=timezone.utc).date()
                        result[symbol][d] = Decimal(str(point["price"]))
                break
            except requests.RequestException as exc:
                if attempt == 2:
                    print(f"  Warning: DeFiLlama fetch failed (chunk {chunk_start}): {exc}")
                time.sleep(2 ** attempt)

        chunk_start += timedelta(days=chunk_days)

    # Drop symbols that returned no data at all
    return {s: prices for s, prices in result.items() if prices}


# ── Liquid staking on-chain rate ─────────────────────────────────────────────

# Selector for ERC-4626 convert_to_assets(uint256 shares) → (uint256 assets)
# Computed as sn_keccak("convert_to_assets") at import time.
def _sn_keccak(name: str) -> str:
    from Crypto.Hash import keccak as _k
    h = _k.new(digest_bits=256)
    h.update(name.encode("ascii"))
    return hex(int(h.hexdigest(), 16) & ((1 << 250) - 1))

_CONVERT_TO_ASSETS_SELECTOR = _sn_keccak("convert_to_assets")


def _rpc_call(rpc_url: str, method: str, params, session: requests.Session | None = None) -> object:
    """Low-level JSON-RPC helper used by pricing-layer functions."""
    sess = session or requests.Session()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(3):
        try:
            resp = sess.post(rpc_url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"RPC error: {data['error']}")
            return data.get("result")
        except requests.RequestException as exc:
            if attempt == 2:
                raise RuntimeError(f"RPC call {method} failed: {exc}") from exc
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")



def _fetch_onchain_exchange_rate(
    vault_contract: str,
    rpc_url: str,
    block_id: str | dict = "latest",
    session: requests.Session | None = None,
    *,
    vault_decimals: int = 18,
    asset_decimals: int = 18,
) -> Decimal:
    """
    Call vault.convert_to_assets(10^vault_decimals) at *block_id*.
    Returns the number of parent-token units equivalent to 1 vault token.

    *vault_decimals* is the ERC-20 decimal count of the vault share token.
    *asset_decimals* is the ERC-20 decimal count of the underlying asset.
    The returned Decimal equals ``raw_result / 10^asset_decimals``.
    """
    one_share_low  = hex(10 ** vault_decimals)
    one_share_high = "0x0"

    result = _rpc_call(
        rpc_url,
        "starknet_call",
        {
            "request": {
                "contract_address": vault_contract,
                "entry_point_selector": _CONVERT_TO_ASSETS_SELECTOR,
                "calldata": [one_share_low, one_share_high],
            },
            "block_id": block_id,
        },
        session,
    )
    if not result:
        raise RuntimeError("convert_to_assets returned empty result")
    low  = int(result[0], 16)
    high = int(result[1], 16) if len(result) > 1 else 0
    raw  = low + (high << 128)
    return Decimal(raw) / Decimal(10 ** asset_decimals)


# ── PriceCache ────────────────────────────────────────────────────────────────

class PriceCache:
    """
    In-memory cache: symbol → {date → ILS price}.

    warm_up() fetches the full date range once.  All subsequent lookups are
    in-memory.  Prices are in ILS (NIS):
      non-stablecoin: DeFiLlama USD price × Bank-of-Israel USD/ILS rate
      stablecoin:     Bank-of-Israel USD/ILS rate × 1.0
    """

    def __init__(self, rpc_url: Optional[str] = None):
        self._cache: dict[str, dict[date, Decimal]] = {}
        self._session = requests.Session()
        self._rpc_url = rpc_url

    def warm_up(
        self,
        symbols: set[str],
        from_date: date,
        to_date: date,
        earliest_block: int | None = None,
    ) -> None:
        """Pre-fetch all prices for the given symbols and date range.

        *earliest_block* is the block number of the oldest fetched transaction.
        It is used to query the on-chain vault rate at the start of the period
        for liquid-staking token interpolation (zero extra RPC calls).
        """
        # Cap to today — prices don't exist for future dates.
        today        = date.today()
        effective_to = min(to_date, today)
        if effective_to < from_date:
            print("  All transactions are in the future — no price data to fetch.")
            return
        if effective_to < to_date:
            print(
                f"  Note: price history capped at {effective_to} (today); "
                f"future-dated transactions use nearest available price."
            )

        # ── Step 1: fetch USD/ILS rates for the whole range at once ──────────
        print("  Fetching USD/ILS rates from Yahoo Finance...")
        boi_rates = _fetch_boi_range(from_date, effective_to)
        if boi_rates:
            print(f"    {len(boi_rates)} rate(s) loaded ({min(boi_rates)} → {max(boi_rates)}).")
        else:
            raise RuntimeError(
                "Failed to fetch USD/ILS exchange rates from Yahoo Finance. "
                "Check your internet connection and try again."
            )

        # ── Step 2: stablecoins — just use BOI rate (= 1 USD in NIS) ─────────
        stable_symbols = symbols & STABLECOINS
        non_stable     = symbols - STABLECOINS
        for symbol in sorted(stable_symbols):
            if symbol in self._cache:
                continue
            prices: dict[date, Decimal] = {}
            current = from_date
            while current <= effective_to:
                rate = _nearest_boi_rate(current)
                if rate:
                    prices[current] = rate
                current += timedelta(days=1)
            self._cache[symbol] = prices
            print(f"  {symbol}: {len(prices)} day(s) from BOI (stablecoin = 1 USD).")

        # ── Step 3: non-stablecoins — DeFiLlama USD × BOI rate ───────────────
        # Exclude liquid staking tokens — they are priced in step 4 via on-chain vault rate.
        needed = [s for s in sorted(non_stable) if s not in self._cache and s not in LIQUID_STAKING_SOURCES]
        if needed:
            print(f"  Fetching USD prices from DeFiLlama for: {', '.join(needed)}...")
            usd_prices = _fetch_defillama_usd_batch(needed, from_date, effective_to, self._session)

            for symbol in needed:
                usd_by_date = usd_prices.get(symbol, {})
                if not usd_by_date:
                    raise RuntimeError(
                        f"Could not fetch USD price data for {symbol} from DeFiLlama "
                        f"for the requested date range. Check your connection or try again later."
                    )

                # Convert USD → ILS using BOI rates
                ils: dict[date, Decimal] = {}
                for d, usd in usd_by_date.items():
                    rate = _nearest_boi_rate(d)
                    if rate:
                        ils[d] = usd * rate
                self._cache[symbol] = ils
                print(f"  {symbol}: {len(ils)} day(s) (DeFiLlama USD × BOI rate).")

        # ── Step 4: liquid staking tokens — parent price × on-chain vault rate ──
        # Fetch the vault rate at two points (period-start block + latest) and
        # linearly interpolate for intermediate dates.  This corrects the ~8%/yr
        # drift that the old "latest-only" approach suffered from.
        for symbol, (parent_symbol, vault_contract) in LIQUID_STAKING_SOURCES.items():
            if symbol not in symbols:
                continue
            if symbol in self._cache:
                continue
            if not self._rpc_url:
                raise RuntimeError(
                    f"Cannot price {symbol}: an --rpc-url is required to fetch the "
                    f"on-chain exchange rate from the vault contract."
                )
            parent_prices = self._cache.get(parent_symbol, {})
            if not parent_prices:
                raise RuntimeError(
                    f"Cannot price {symbol}: parent token {parent_symbol} has no cached prices."
                )

            v_dec = TOKEN_DECIMALS.get(symbol, 18)
            a_dec = TOKEN_DECIMALS.get(parent_symbol, 18)

            print(f"  Fetching {symbol} vault rate (latest" +
                  (f" + block {earliest_block}" if earliest_block else "") + ")...")
            rate_end = _fetch_onchain_exchange_rate(
                vault_contract, self._rpc_url, "latest", self._session,
                vault_decimals=v_dec, asset_decimals=a_dec,
            )

            rate_start = rate_end
            if earliest_block is not None:
                try:
                    rate_start = _fetch_onchain_exchange_rate(
                        vault_contract, self._rpc_url,
                        {"block_number": earliest_block}, self._session,
                        vault_decimals=v_dec, asset_decimals=a_dec,
                    )
                except RuntimeError as exc:
                    print(f"  Warning: historical {symbol} vault rate fetch failed ({exc}); "
                          f"using latest-only rate for entire period.")

            total_days = max(1, (effective_to - from_date).days)
            print(
                f"  {symbol}: vault rate {rate_start:.8f} (≈{from_date}) → "
                f"{rate_end:.8f} (latest); interpolating over {total_days} days."
            )

            ils: dict[date, Decimal] = {}
            for d, parent_price in parent_prices.items():
                t = Decimal((d - from_date).days) / Decimal(total_days)
                t = max(Decimal(0), min(t, Decimal(1)))
                rate_d = rate_start + (rate_end - rate_start) * t
                ils[d] = parent_price * rate_d
            self._cache[symbol] = ils
            print(f"  {symbol}: {len(ils)} day(s) derived from {parent_symbol} × interpolated vault rate.")

    def get(self, symbol: str, target_date: date) -> Optional[Decimal]:
        """
        Return the ILS price for `symbol` on `target_date`.
        Searches ±3 days to handle weekends and public holidays.
        """
        cached = self._cache.get(symbol, {})
        if not cached:
            return None
        if target_date in cached:
            return cached[target_date]
        for delta in range(1, 4):
            for d in (target_date - timedelta(days=delta), target_date + timedelta(days=delta)):
                if d in cached:
                    return cached[d]
        return None

    def get_required(self, symbol: str, target_date: date, tx_hash: str) -> Decimal:
        """Like get(), but warns and returns 0 if price is missing."""
        price = self.get(symbol, target_date)
        if price is None:
            print(
                f"  Warning: no {symbol}/ILS price for {target_date} "
                f"(tx {tx_hash[:12]}...) — flagged for manual review."
            )
            return Decimal(0)
        return price
