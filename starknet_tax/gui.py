"""
GUI wrapper for starknet-tax-il.

Launches a FreeSimpleGUI window that lets the user fill in parameters and
watch the report run in real time.  All print() output from the underlying
library is captured via a queue-backed stream and appended to the log panel
without blocking the GUI event loop.
"""
from __future__ import annotations

import io
import queue
import sys
import textwrap
import threading
import traceback
from collections import Counter
from datetime import datetime, date
from pathlib import Path

import FreeSimpleGUI as sg

from .classifier import classify_all
from .config import ADDRESS_TO_TOKEN
from .fetcher import fetch_transactions
from .form1399 import generate_form_1399
from .pricing import PriceCache
from .report import generate_report
from .tax import process_events


# ── Constants ────────────────────────────────────────────────────────────────

WINDOW_TITLE = "StarkNet Tax Report — Israeli CGT"
DEFAULT_RPC = "https://rpc.pathfinder.equilibrium.co/mainnet/rpc/v0_10"
POLL_MS = 100  # GUI queue poll interval

# Sentinel objects posted to the log queue
_DONE_OK = object()
_DONE_ERR = object()


# ── Queue-backed stdout redirect ─────────────────────────────────────────────

class _QueueStream(io.TextIOBase):
    """Wraps a queue.Queue so that print() calls post lines to the GUI."""

    def __init__(self, q: queue.Queue) -> None:
        self._q = q
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._q.put(line + "\n")
        return len(text)

    def flush(self) -> None:
        if self._buf:
            self._q.put(self._buf)
            self._buf = ""

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


# ── Background worker ────────────────────────────────────────────────────────

def _run_report(
    log_q: queue.Queue,
    address: str,
    from_d: date,
    to_d: date,
    rpc_url: str,
    dune_api_key: str,
    output_csv: str,
) -> None:
    """
    Execute the full report pipeline in a background thread.
    All print() output is redirected to log_q.
    Posts _DONE_OK or _DONE_ERR as the final queue item.
    """
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    stream = _QueueStream(log_q)
    sys.stdout = stream  # type: ignore[assignment]
    sys.stderr = stream  # type: ignore[assignment]

    try:
        form_output = output_csv.replace(".csv", "_form1399.pdf")

        print(f"\nStarkNet Israeli Tax Report")
        print(f"  Address : {address}")
        print(f"  Period  : {from_d} → {to_d}")
        print(f"  CSV     : {output_csv}")
        print(f"  Form 1399 PDF: {form_output}")
        print("")
        print("Supported tokens:")
        for addr, symbol in sorted(ADDRESS_TO_TOKEN.items(), key=lambda x: x[1]):
            print(f"  {symbol:<8}  {addr}")
        print("")

        print("Step 1/4: Fetching ALL-TIME transaction list via Dune Analytics...")
        print("  (RPC loads receipts; full history ensures correct FIFO)")

        transactions = fetch_transactions(
            address=address,
            from_date=from_d,
            to_date=to_d,
            rpc_url=rpc_url,
            dune_api_key=dune_api_key,
        )

        if not transactions:
            print("No transactions found in the given period.")
            log_q.put(_DONE_OK)
            return

        print(f"  → {len(transactions)} transactions fetched.")

        # Step 2: Classify
        print("\nStep 2/4: Classifying transactions...")
        events = classify_all(transactions)
        counts = Counter(e.event_type.value for e in events)
        for etype, count in sorted(counts.items()):
            print(f"  {etype:<22} {count:>4}")
        n_in_period = sum(1 for e in events if from_d <= e.timestamp.date() <= to_d)
        print(
            f"  ── {n_in_period} of {len(events)} event(s) dated within the report period "
            f"({from_d} … {to_d}). The CSV lists these only; other txs still affect FIFO."
        )

        # Step 3: Prices
        print("\nStep 3/4: Fetching ILS prices...")
        all_symbols: set[str] = set()
        for evt in events:
            for f in evt.tokens_in + evt.tokens_out:
                all_symbols.add(f.symbol)
            all_symbols.add(evt.fee_token)

        all_event_dates = [evt.timestamp.date() for evt in events]
        price_from = min(all_event_dates) if all_event_dates else from_d
        price_to   = max(all_event_dates) if all_event_dates else to_d

        earliest_block = min((tx.block_number for tx in transactions), default=None)

        prices = PriceCache(rpc_url=rpc_url)
        prices.warm_up(all_symbols, price_from, price_to, earliest_block=earliest_block)

        # Step 4: Tax
        print("\nStep 4/4: Calculating tax (FIFO, Israeli CGT rules)...")
        processed, summary = process_events(events, prices, from_d, to_d)

        # Summary
        print("")
        print("=" * 50)
        print("  TAX SUMMARY (₪ NIS)")
        print("=" * 50)
        print(f"  Capital gains (gross)  : ₪{summary.total_capital_gains_ils:>12,.2f}")
        print(f"  Capital losses         : ₪{summary.total_capital_losses_ils:>12,.2f}")
        net = summary.total_capital_gains_ils - summary.total_capital_losses_ils
        print(f"  Net capital gains      : ₪{net:>12,.2f}")
        print(f"  Staking/DeFi income    : ₪{summary.total_income_ils:>12,.2f}")
        print(f"  Gas fees paid          : ₪{summary.total_fees_ils:>12,.2f}")
        print(f"  ─────────────────────────────────")
        print(f"  CGT (25%)              : ₪{summary.cgt_owed_ils:>12,.2f}")
        if summary.surtax_owed_ils > 0:
            print(f"  Surtax (5%)            : ₪{summary.surtax_owed_ils:>12,.2f}")
        print(f"  ═════════════════════════════════")
        print(f"  TOTAL TAX OWED         : ₪{summary.total_tax_owed_ils:>12,.2f}")
        print("=" * 50)

        if summary.needs_manual_review:
            print(
                f"\n  ⚠  {len(summary.needs_manual_review)} transaction(s) need manual review "
                "(marked in CSV)."
            )

        print(
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
            output_path=output_csv,
        )

        # Write PDF
        n = generate_form_1399(
            events=processed,
            summary=summary,
            address=address,
            from_date=from_d,
            to_date=to_d,
            output_path=form_output,
        )
        if n:
            print(f"Form 1399 PDF written to: {Path(form_output).resolve()}  ({n} disposal event(s))")
        else:
            print("Form 1399 PDF: no disposal events — PDF not generated.")

        log_q.put(_DONE_OK)

    except Exception:
        tb = traceback.format_exc()
        print(f"\n[ERROR]\n{tb}")
        log_q.put(_DONE_ERR)

    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


# ── Layout helpers ───────────────────────────────────────────────────────────

def _label(text: str) -> sg.Text:
    return sg.Text(text, size=(22, 1))


def _input(key: str, default: str = "", password: bool = False) -> sg.Input:
    return sg.Input(
        default_text=default,
        key=key,
        expand_x=True,
        password_char="*" if password else "",
    )


def _build_layout() -> list:
    today = date.today().isoformat()
    year_start = f"{date.today().year}-01-01"

    form_rows = [
        [_label("Wallet Address *"), _input("-ADDR-")],
        [_label("From Date (YYYY-MM-DD) *"), _input("-FROM-", year_start)],
        [_label("To Date (YYYY-MM-DD) *"), _input("-TO-", today)],
        [_label("RPC URL"), _input("-RPC-", DEFAULT_RPC)],
        [_label("Dune API Key *"), _input("-DUNE-", password=True)],
        [_label("Output CSV path"), _input("-OUTPUT-", "starknet_tax_report.csv")],
    ]

    button_row = [
        sg.Button("Run Report", key="-RUN-", button_color=("white", "#2d7d46"), size=(14, 1)),
        sg.Button("Clear Log", key="-CLEAR-", size=(10, 1)),
        sg.Push(),
        sg.Text("", key="-STATUS-", size=(30, 1), justification="right",
                font=("Helvetica", 10, "bold")),
    ]

    log_frame = sg.Frame(
        "Output Log",
        [[
            sg.Multiline(
                key="-LOG-",
                size=(90, 22),
                autoscroll=True,
                expand_x=True,
                expand_y=True,
                disabled=True,
                font=("Courier", 9),
                background_color="#1e1e1e",
                text_color="#d4d4d4",
            )
        ]],
        expand_x=True,
        expand_y=True,
    )

    csv_row = [
        sg.Text("CSV:", size=(4, 1)),
        sg.Text("", key="-CSV-PATH-", expand_x=True, text_color="#4fc3f7"),
        sg.Button("Open Folder", key="-OPEN-FOLDER-", visible=False, size=(12, 1)),
    ]

    layout = [
        *form_rows,
        [sg.HorizontalSeparator()],
        button_row,
        [log_frame],
        csv_row,
    ]
    return layout


# ── Main GUI entry point ─────────────────────────────────────────────────────

def _append_log(window: sg.Window, text: str) -> None:
    window["-LOG-"].update(disabled=False)
    window["-LOG-"].print(text, end="")
    window["-LOG-"].update(disabled=True)


def _set_status(window: sg.Window, ok: bool | None) -> None:
    """ok=True → green, ok=False → red, ok=None → clear."""
    if ok is None:
        window["-STATUS-"].update("", text_color=sg.theme_text_color())
    elif ok:
        window["-STATUS-"].update("DONE — success", text_color="#4caf50")
    else:
        window["-STATUS-"].update("FAILED — see log", text_color="#f44336")


def main() -> None:
    sg.theme("DarkGrey13")

    layout = _build_layout()
    window = sg.Window(
        WINDOW_TITLE,
        layout,
        size=(860, 680),
        minimum_size=(800, 600),
        resizable=True,
        finalize=True,
    )

    log_q: queue.Queue = queue.Queue()
    running = False
    csv_output_path: str | None = None

    while True:
        event, values = window.read(timeout=POLL_MS)

        # ── Window close ──────────────────────────────────────────────────────
        if event in (sg.WIN_CLOSED, "Exit"):
            break

        # ── Drain log queue (runs every POLL_MS even when event==TIMEOUT) ─────
        while True:
            try:
                item = log_q.get_nowait()
            except queue.Empty:
                break

            if item is _DONE_OK:
                running = False
                window["-RUN-"].update(disabled=False)
                _set_status(window, True)
                if csv_output_path:
                    window["-CSV-PATH-"].update(csv_output_path)
                    window["-OPEN-FOLDER-"].update(visible=True)

            elif item is _DONE_ERR:
                running = False
                window["-RUN-"].update(disabled=False)
                _set_status(window, False)

            else:
                # Regular log line
                _append_log(window, item)

        # ── Button: Run Report ────────────────────────────────────────────────
        if event == "-RUN-" and not running:
            address = values["-ADDR-"].strip()
            from_str = values["-FROM-"].strip()
            to_str = values["-TO-"].strip()
            rpc_url = values["-RPC-"].strip() or DEFAULT_RPC
            dune_key = values["-DUNE-"].strip()
            output_csv = values["-OUTPUT-"].strip() or "starknet_tax_report.csv"

            # Validate
            errors: list[str] = []
            if not address:
                errors.append("Wallet Address is required.")
            if not dune_key:
                errors.append(
                    "Dune API Key is required (free: dune.com → sign in → Settings → API)."
                )
            try:
                from_d = datetime.strptime(from_str, "%Y-%m-%d").date()
            except ValueError:
                errors.append("From Date must be YYYY-MM-DD.")
                from_d = None  # type: ignore[assignment]
            try:
                to_d = datetime.strptime(to_str, "%Y-%m-%d").date()
            except ValueError:
                errors.append("To Date must be YYYY-MM-DD.")
                to_d = None  # type: ignore[assignment]

            if from_d and to_d and from_d > to_d:
                errors.append("From Date must be before To Date.")

            if errors:
                sg.popup_error("\n".join(errors), title="Validation Error")
                continue

            # Default output name based on address + year if user left default
            if output_csv == "starknet_tax_report.csv":
                short = address[:10].replace("0x", "")
                output_csv = f"starknet_tax_{short}_{from_d.year}.csv"
                window["-OUTPUT-"].update(output_csv)

            csv_output_path = str(Path(output_csv).resolve())

            _set_status(window, None)
            window["-CSV-PATH-"].update("")
            window["-OPEN-FOLDER-"].update(visible=False)
            window["-LOG-"].update(disabled=False)
            window["-LOG-"].update("")
            window["-LOG-"].update(disabled=True)
            window["-RUN-"].update(disabled=True)
            running = True

            t = threading.Thread(
                target=_run_report,
                kwargs=dict(
                    log_q=log_q,
                    address=address,
                    from_d=from_d,
                    to_d=to_d,
                    rpc_url=rpc_url,
                    dune_api_key=dune_key,
                    output_csv=output_csv,
                ),
                daemon=True,
            )
            t.start()

        # ── Button: Clear Log ─────────────────────────────────────────────────
        elif event == "-CLEAR-":
            window["-LOG-"].update(disabled=False)
            window["-LOG-"].update("")
            window["-LOG-"].update(disabled=True)
            _set_status(window, None)

        # ── Button: Open Folder ───────────────────────────────────────────────
        elif event == "-OPEN-FOLDER-" and csv_output_path:
            folder = str(Path(csv_output_path).parent)
            import subprocess
            import platform
            try:
                if platform.system() == "Windows":
                    subprocess.Popen(["explorer", folder])
                elif platform.system() == "Darwin":
                    subprocess.Popen(["open", folder])
                else:
                    subprocess.Popen(["xdg-open", folder])
            except Exception as exc:
                sg.popup_error(f"Could not open folder:\n{exc}")

    window.close()


if __name__ == "__main__":
    main()
