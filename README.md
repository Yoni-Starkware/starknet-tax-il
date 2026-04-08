# starknet-tax-il

Israeli annual tax report generator for StarkNet wallet activity.

Produces a CSV with every transaction classified, valued in NIS (₪), and a
bottom-line tax figure based on ITA Circular 05/2018 (25% CGT, FIFO method),
plus a supporting **Form 1399י-style PDF** for disposal events.

## What it covers

| Activity | Tax treatment applied |
|---|---|
| DEX swaps (Ekubo, JediSwap, …) | Capital gain/loss on disposed token |
| STRK native staking rewards | Income at FMV on claim date |
| Endur liquid staking (STRK ↔ xSTRK) | Taxable crypto-to-crypto exchange |
| DeFi yield (zkLend, Nostra interest) | Income at FMV on receipt |
| Airdrops / unknown inflows | Flagged — review manually |
| Outgoing transfers | Treated as disposal (CGT) |
| Gas fees | Tracked for deductibility |

## Install

```bash
pip install git+https://github.com/Yoni-Starkware/starknet-tax-il
```

Requires Python 3.9+.

## How transaction history is fetched

The tool talks to StarkNet over **JSON-RPC** (for block lookup and transaction receipts).

- **Default (no Dune):** It discovers transactions in the **report period only** (`--from-date` … `--to-date`) by scanning your account’s events, token `Transfer` events, and staking-related events. Use this when your cost basis does not depend on activity before that window, or when you only need a quick picture of the year.
- **With `--dune-api-key`:** It loads the **full all-time** transaction list from **Dune Analytics**, then still pulls each receipt via RPC. The tax **summary** is still limited to your chosen dates, but **FIFO cost basis** is built over complete history—important if you acquired tokens before the report year and sell or swap during it.

Get a free Dune API key at [dune.com/settings/api](https://dune.com/settings/api).

## Quick start

```bash
starknet-tax \
  --address 0x04f8f5... \
  --from-date 2024-01-01 \
  --to-date 2024-12-31
```

Outputs in the current directory:

- `starknet_tax_<address prefix>_<year>.csv` — three sections: transaction detail, FIFO disposal detail, tax summary
- `starknet_tax_<address prefix>_<year>_form1399.pdf` — generated when there are disposal events (otherwise skipped)

For heavier wallets or rate limits, pass your own RPC (e.g. Alchemy):

```bash
export STARKNET_RPC_URL='https://starknet-mainnet.g.alchemy.com/starknet/version/rpc/v0_10/YOUR_KEY'
starknet-tax -a 0x... -f 2024-01-01 -t 2024-12-31
```

### Full FIFO across all prior years

```bash
starknet-tax \
  -a 0x04f8f5... \
  -f 2024-01-01 \
  -t 2024-12-31 \
  --dune-api-key YOUR_DUNE_KEY
```

### CLI options

| Option | Description |
|--------|-------------|
| `-a`, `--address` | StarkNet account contract address (required) |
| `-f`, `--from-date` | Start date `YYYY-MM-DD`, inclusive (required) |
| `-t`, `--to-date` | End date `YYYY-MM-DD`, inclusive (required) |
| `-o`, `--output` | Output CSV path (default: auto-named from address and year) |
| `--rpc-url` | StarkNet JSON-RPC URL; env: `STARKNET_RPC_URL` (default: public Pathfinder mainnet) |
| `--delegation-pool` | STRK delegation pool address; repeat for multiple pools (optional; pools are also auto-discovered when possible) |
| `--coingecko-api-key` | CoinGecko Demo API key for price fallback / rate limits; env: `COINGECKO_API_KEY` |
| `--dune-api-key` | Dune API key for all-time tx discovery; env: `DUNE_API_KEY` |

Run `starknet-tax --help` for the full inline help.

### Pricing

Token prices are resolved in NIS using DeFiLlama (USD) × USD/ILS (Yahoo Finance), with optional CoinGecko ILS fallback when `--coingecko-api-key` is set. Stablecoins track USD/ILS. See `starknet_tax/pricing.py` for details.

## Legal disclaimer

This tool is informational only. Israeli tax law is complex and subject to
change. Consult a licensed Israeli CPA before filing.

Key filing requirement: **Form 1399 must be filed and advance tax paid within
30 days of each disposal event** — not just at year end.
