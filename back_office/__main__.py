"""Command line entry point.

    python -m back_office demo                 run on the bundled synthetic account (no API key)
    python -m back_office reconcile            reconcile the live account, print breaks
    python -m back_office statement            reconcile + daily statement (text + HTML)
    python -m back_office capture              append new fills/settlements/orders to the audit log

Options: --db PATH (default ./back_office.db), --out DIR (default ./statements)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="back_office", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["demo", "reconcile", "statement", "capture"])
    ap.add_argument("--db", default="back_office.db")
    ap.add_argument("--out", default="statements")
    args = ap.parse_args(argv)

    if args.command == "demo":
        from .demo import run_demo
        run_demo(args.out if args.out != "statements" else "demo_output")
        return 0

    _load_env()
    from . import events
    from .client import KalshiClient, KalshiError
    from .reconcile import fetch_snapshot, persist, reconcile, render_report
    from .statement import build, render_text, write_html

    try:
        client = KalshiClient()
    except KalshiError as exc:
        print(exc, file=sys.stderr)
        return 2
    conn = sqlite3.connect(args.db)
    try:
        if args.command == "capture":
            added = events.capture(conn, client.get_fills(), client.get_settlements(),
                                   client.get_resting_orders())
            print(f"new events: {added}")
            return 0
        snap = fetch_snapshot(client)
        if args.command == "reconcile":
            r = reconcile(snap)
            persist(conn, r)
            print(render_report(r))
            return 1 if r.breaks else 0
        events.capture(conn, snap.fills, snap.settlements, client.get_resting_orders())
        st = build(snap, conn)
        print(render_text(st))
        print(f"\nHTML statement: {write_html(st, args.out)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
