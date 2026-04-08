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

## Dune API key (required)

Transaction discovery uses **Dune Analytics** so your **full on-chain history** is included. That keeps **FIFO cost basis** correct when you bought or received tokens before the tax year and dispose of them later. RPC-only scanning of a date range was slow, incomplete, and easy to get wrong—so this tool **requires** a Dune key.

**How to get a free API key**

1. Create a free account at [dune.com](https://dune.com) (sign in with GitHub/Google/email).
2. Open **[Settings → API](https://dune.com/settings/api)** (or: profile menu → **Settings** → **API**).
3. Create an API key and copy it.

Then either:

```bash
export DUNE_API_KEY='paste_your_key_here'
```

or pass **`--dune-api-key`** on each run.

Free tier is enough for typical use; heavy wallets may hit rate limits—see Dune’s docs for quotas.

Query runs use Dune’s **`medium`** performance tier by default. For heavier SQL, set **`DUNE_PERFORMANCE=large`** (may require credits on your Dune plan).

## How data is fetched

1. **Dune** runs a query that returns **all transaction hashes** relevant to your wallet (user txs plus transfers on known token contracts).
2. **StarkNet JSON-RPC** loads each **transaction receipt** (token flows, fees). Use a reliable RPC—your own [Alchemy](https://www.alchemy.com/) endpoint is recommended for speed and limits.

The **report period** (`--from-date` … `--to-date`) still controls what appears in the **tax summary**; events outside that window are used only for FIFO inventory.

## Quick start

```bash
export DUNE_API_KEY='...'   # from https://dune.com/settings/api

starknet-tax \
  --address 0x04f8f5... \
  --from-date 2024-01-01 \
  --to-date 2024-12-31
```

Outputs in the current directory:

- `starknet_tax_<address prefix>_<year>.csv` — transaction detail, FIFO disposal detail, tax summary
- `starknet_tax_<address prefix>_<year>_form1399.pdf` — when there are disposal events (otherwise skipped)

The CSV **Section 1** rows are only transactions **dated inside** `--from-date` … `--to-date`. Dune still loads your full history so FIFO is correct; staking deposits/withdrawals outside the tax year won’t appear in the CSV even though they’re counted in Step 2’s classification totals.

Optional: set a dedicated RPC (recommended for large histories):

```bash
export STARKNET_RPC_URL='https://starknet-mainnet.g.alchemy.com/starknet/version/rpc/v0_10/YOUR_KEY'
```

### CLI options

| Option | Description |
|--------|-------------|
| `-a`, `--address` | StarkNet account contract address (required) |
| `-f`, `--from-date` | Start date `YYYY-MM-DD`, inclusive (required) |
| `-t`, `--to-date` | End date `YYYY-MM-DD`, inclusive (required) |
| `-o`, `--output` | Output CSV path (default: auto-named from address and year) |
| `--dune-api-key` | **Required.** Dune API key, or set env **`DUNE_API_KEY`** |
| `--rpc-url` | StarkNet JSON-RPC URL; env: **`STARKNET_RPC_URL`** (default: public Pathfinder mainnet) |

Run `starknet-tax --help` for the full inline help.

### Pricing

Token prices are resolved in NIS using DeFiLlama (USD) × USD/ILS (Yahoo Finance). Stablecoins track USD/ILS. See `starknet_tax/pricing.py` for details.

## Limitations & known approximations

| Area | Detail |
|------|--------|
| **USD/ILS exchange rates** | Sourced from Yahoo Finance market mid-rates (`USDILS=X`), **not** official Bank of Israel published rates. For tax filing, verify against [boi.org.il](https://www.boi.org.il/en/economic-roles/financial-markets/exchange-rates/). |
| **Token prices** | Daily snapshots from DeFiLlama. Intraday volatility may cause ±1–2 % variance from actual execution prices. |
| **Liquid staking (xSTRK)** | On-chain vault rate is sampled at the **start** and **end** of the report period and linearly interpolated. Actual rate accrues staking rewards continuously (~8 %/yr), so intermediate dates carry a small approximation error. |
| **RECEIVE events** | Incoming transfers are treated as cost-basis acquisitions at FMV. If any are airdrops, grants, or staking distributions, they are **unreported income** — verify each one manually. |
| **Surtax rate** | Default is 3 % (Section 121B(f)). Some CPAs apply 5 % if crypto capital income falls under Section 121B(b). Consult your CPA. |
| **Form 1399 deadlines** | The tool does **not** track the 30-day filing window. Each disposal must be reported on Form 1399 within 30 days — the taxpayer is responsible. |

## Legal disclaimer

**This tool is NOT tax advice.** It is a computational aid only. Israeli tax
law is complex, evolving, and depends on individual circumstances.

All figures, classifications, and tax calculations produced by this tool **must
be reviewed and approved by a licensed Israeli CPA (רו״ח)** before filing
with the Israel Tax Authority (רשות המסים).

Key filing requirement: **Form 1399 must be filed and advance tax paid within
30 days of each disposal event** — not just at year end.
