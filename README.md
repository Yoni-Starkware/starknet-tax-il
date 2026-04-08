# starknet-tax-il

Israeli annual tax report generator for StarkNet wallet activity.

Produces a CSV with every transaction classified, valued in NIS (₪), and a
bottom-line tax figure based on ITA Circular 05/2018 (25% CGT, FIFO method).

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
pip install git+https://github.com/YOUR_USERNAME/starknet-tax-il
```

## Quick start

### 1. Get a free Voyager API key
Sign up at **https://voyager.online/** — takes 30 seconds, no credit card.

### 2. Run

```bash
starknet-tax \
  --address 0x04f8f5... \
  --from-date 2024-01-01 \
  --to-date 2024-12-31 \
  --voyager-api-key YOUR_KEY
```

Output: `starknet_tax_04f8f5_2024.csv` in the current directory.

### All options

```
  -a, --address            StarkNet wallet address (required)
  -f, --from-date          Start date YYYY-MM-DD (required)
  -t, --to-date            End date YYYY-MM-DD (required)
  -o, --output             Output CSV path (default: auto-named)
      --voyager-api-key    Voyager API key (or VOYAGER_API_KEY env var)
      --rpc-url            Custom StarkNet RPC URL (default: public Blast endpoint)
      --coingecko-api-key  CoinGecko Demo key for higher rate limits (optional)
```

## CSV output

The report has three sections:

1. **Transaction Detail** — every event with token amounts, ILS prices, gain/loss
2. **FIFO Disposal Detail** — lot-by-lot breakdown for Form 1399
3. **Tax Summary** — totals and bottom-line tax owed

## Legal disclaimer

This tool is informational only. Israeli tax law is complex and subject to
change. Consult a licensed Israeli CPA before filing.

Key filing requirement: **Form 1399 must be filed and advance tax paid within
30 days of each disposal event** — not just at year end.
