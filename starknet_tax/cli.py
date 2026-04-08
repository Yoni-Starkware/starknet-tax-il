"""
CLI entrypoint for starknet-tax-il.

Usage:
  starknet-tax --address 0x04f8... --from-date 2024-01-01 --to-date 2024-12-31 --output report.csv

Install:
  pip install git+https://github.com/YOUR_USERNAME/starknet-tax-il
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import click

from .classifier import classify_all
from .config import ADDRESS_TO_TOKEN, PUBLIC_RPC_URLS
from .fetcher import fetch_transactions
from .form1399 import generate_form_1399
from .pricing import PriceCache
from .report import generate_report
from .tax import process_events


@click.command()
@click.option(
    "--address", "-a",
    required=True,
    help="StarkNet account contract address (0x...)",
)
@click.option(
    "--from-date", "-f",
    required=True,
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Start date (YYYY-MM-DD), inclusive.",
)
@click.option(
    "--to-date", "-t",
    required=True,
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="End date (YYYY-MM-DD), inclusive.",
)
@click.option(
    "--output", "-o",
    default=None,
    help="Output CSV path. Defaults to starknet_tax_<address[:8]>_<year>.csv",
)
@click.option(
    "--rpc-url",
    envvar="STARKNET_RPC_URL",
    default="https://rpc.pathfinder.equilibrium.co/mainnet/rpc/v0_10",
    show_default=True,
    help=(
        "StarkNet JSON-RPC URL. For higher rate limits use your own Alchemy endpoint "
        "(https://starknet-mainnet.g.alchemy.com/starknet/version/rpc/v0_10/YOUR_KEY). "
        "Can also be set via STARKNET_RPC_URL env var."
    ),
)
@click.option(
    "--delegation-pool",
    "delegation_pools",
    multiple=True,
    help=(
        "Delegation pool contract address (0x...) for STRK staking rewards. "
        "Repeat for multiple pools. If not provided, pool contracts are auto-discovered "
        "from the staking contract events (may not find all pools). "
        "Find your pool at https://starknet.io/staking/"
    ),
)
@click.option(
    "--coingecko-api-key",
    envvar="COINGECKO_API_KEY",
    default=None,
    help=(
        "CoinGecko Demo API key (optional, for higher rate limits). "
        "Get one free at https://www.coingecko.com/en/api"
    ),
)
@click.option(
    "--dune-api-key",
    envvar="DUNE_API_KEY",
    default=None,
    help=(
        "Dune Analytics API key. When provided, fetches the full ALL-TIME "
        "transaction history from Dune so that FIFO cost basis is complete. "
        "Get a free key at https://dune.com/settings/api"
    ),
)
def main(
    address: str,
    from_date,
    to_date,
    output: str | None,
    rpc_url: str | None,
    delegation_pools: tuple,
    coingecko_api_key: str | None,
    dune_api_key: str | None,
) -> None:
    """
    Generate an Israeli tax report for a StarkNet wallet.

    Produces a CSV with:
      - Section 1: Every transaction classified and valued in NIS
      - Section 2: FIFO lot detail for Form 1399
      - Section 3: Summary with total tax owed at 25% CGT

    \b
    Example:
      starknet-tax \\
        --address 0x04f8f5... \\
        --from-date 2024-01-01 \\
        --to-date 2024-12-31 \\
        --rpc-url https://starknet-mainnet.g.alchemy.com/starknet/version/rpc/v0_10/YOUR_KEY
    """
    from_d = from_date.date()
    to_d = to_date.date()

    if from_d > to_d:
        click.echo("Error: --from-date must be before --to-date", err=True)
        sys.exit(1)

    if output is None:
        short_addr = address[:10].replace("0x", "")
        output = f"starknet_tax_{short_addr}_{from_d.year}.csv"
    form_output = output.replace(".csv", "_form1399.pdf")

    click.echo(f"\nStarkNet Israeli Tax Report")
    click.echo(f"  Address : {address}")
    click.echo(f"  Period  : {from_d} → {to_d}")
    click.echo(f"  CSV     : {output}")
    click.echo(f"  Form 1399 PDF: {form_output}")
    click.echo("")
    click.echo("Supported tokens:")
    # Reverse the normalized map to get original padded addresses for display
    for addr, symbol in sorted(ADDRESS_TO_TOKEN.items(), key=lambda x: x[1]):
        click.echo(f"  {symbol:<8}  {addr}")
    click.echo("")

    # Step 1: Fetch transactions
    if dune_api_key:
        click.echo("Step 1/4: Fetching ALL-TIME transactions via Dune Analytics...")
        click.echo("  (Full history ensures correct FIFO cost basis for all disposals)")
    else:
        click.echo("Step 1/4: Fetching transactions via RPC...")
    try:
        transactions = fetch_transactions(
            address=address,
            from_date=from_d,
            to_date=to_d,
            voyager_api_key=None,
            rpc_url=rpc_url,
            delegation_pools=list(delegation_pools) if delegation_pools else None,
            dune_api_key=dune_api_key,
        )
    except PermissionError as e:
        click.echo(f"\n{e}", err=True)
        sys.exit(1)

    if not transactions:
        click.echo("No transactions found in the given period.")
        sys.exit(0)

    click.echo(f"  → {len(transactions)} transactions fetched.")

    # Step 2: Classify
    click.echo("\nStep 2/4: Classifying transactions...")
    events = classify_all(transactions)

    from collections import Counter
    counts = Counter(e.event_type.value for e in events)
    for etype, count in sorted(counts.items()):
        click.echo(f"  {etype:<22} {count:>4}")

    # Step 3: Price lookup
    click.echo("\nStep 3/4: Fetching ILS prices...")
    all_symbols: set[str] = set()
    for evt in events:
        for f in evt.tokens_in + evt.tokens_out:
            all_symbols.add(f.symbol)
        all_symbols.add(evt.fee_token)

    # Cover the full span of all events so pre-period acquisitions get correct
    # cost basis (important for FIFO accuracy).
    all_event_dates = [evt.timestamp.date() for evt in events]
    price_from = min(all_event_dates) if all_event_dates else from_d
    price_to   = max(all_event_dates) if all_event_dates else to_d

    prices = PriceCache(coingecko_api_key=coingecko_api_key, rpc_url=rpc_url)
    prices.warm_up(all_symbols, price_from, price_to)

    # Step 4: Tax calculation
    click.echo("\nStep 4/4: Calculating tax (FIFO, Israeli CGT rules)...")
    processed, summary = process_events(events, prices, from_d, to_d)

    # Print bottom-line summary to terminal
    click.echo("")
    click.echo("=" * 50)
    click.echo("  TAX SUMMARY (₪ NIS)")
    click.echo("=" * 50)
    click.echo(f"  Capital gains (gross)  : ₪{summary.total_capital_gains_ils:>12,.2f}")
    click.echo(f"  Capital losses         : ₪{summary.total_capital_losses_ils:>12,.2f}")
    net = summary.total_capital_gains_ils - summary.total_capital_losses_ils
    click.echo(f"  Net capital gains      : ₪{net:>12,.2f}")
    click.echo(f"  Staking/DeFi income    : ₪{summary.total_income_ils:>12,.2f}")
    click.echo(f"  Gas fees paid          : ₪{summary.total_fees_ils:>12,.2f}")
    click.echo(f"  ─────────────────────────────────")
    click.echo(f"  CGT (25%)              : ₪{summary.cgt_owed_ils:>12,.2f}")
    if summary.surtax_owed_ils > 0:
        click.echo(f"  Surtax (5%)            : ₪{summary.surtax_owed_ils:>12,.2f}")
    click.echo(f"  ═════════════════════════════════")
    click.echo(f"  TOTAL TAX OWED         : ₪{summary.total_tax_owed_ils:>12,.2f}")
    click.echo("=" * 50)

    if summary.needs_manual_review:
        click.echo(
            f"\n  ⚠  {len(summary.needs_manual_review)} transaction(s) need manual review "
            "(marked in CSV)."
        )

    click.echo(
        "\nIMPORTANT: File Form 1399 + pay advance tax within 30 days of each disposal.\n"
        "This report is informational only. Consult a licensed Israeli CPA.\n"
    )

    # Write CSV
    generate_report(
        events=processed,
        summary=summary,
        address=address,
        from_date=from_d,
        to_date=to_d,
        output_path=output,
    )

    # Write Form 1399 PDF
    n = generate_form_1399(
        events=processed,
        summary=summary,
        address=address,
        from_date=from_d,
        to_date=to_d,
        output_path=form_output,
    )
    if n:
        click.echo(f"Form 1399 PDF written to: {Path(form_output).resolve()}  ({n} disposal event(s))")
    else:
        click.echo("Form 1399 PDF: no disposal events — PDF not generated.")


if __name__ == "__main__":
    main()
