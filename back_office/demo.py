"""Offline demo: run the full pipeline on a bundled synthetic account.

`FixtureClient` has the same read methods as `KalshiClient` but serves records
from a JSON file, so the demo exercises exactly the code path a live run uses.
"""
from __future__ import annotations

import json
import os
import sqlite3
from importlib import resources

from . import events
from .reconcile import Snapshot, init_db
from .statement import build, render_text, write_html


class FixtureClient:
    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def bundled(cls) -> "FixtureClient":
        text = resources.files("back_office").joinpath("fixtures/demo_account.json").read_text()
        return cls(json.loads(text))

    def get_balance(self):
        b = self.data["balance"]
        return b["balance"], b["portfolio_value"]

    def get_fills(self, include_historical: bool = True):
        fills = list(self.data["fills"])
        if include_historical:
            seen = {f["fill_id"] for f in fills}
            fills += [f for f in self.data["historical_fills"] if f["fill_id"] not in seen]
        return fills

    def get_positions(self):
        return self.data["positions"]

    def get_settlements(self):
        return self.data["settlements"]

    def get_historical_markets(self, tickers):
        wanted = set(tickers)
        return [m for m in self.data.get("historical_markets", []) if m["ticker"] in wanted]

    def get_deposits(self):
        return self.data["deposits"]

    def get_withdrawals(self):
        return self.data["withdrawals"]

    def get_resting_orders(self):
        return self.data["resting_orders"]


def snapshot_from(client: FixtureClient) -> Snapshot:
    bal, pv = client.get_balance()
    fills, settlements = client.get_fills(), client.get_settlements()
    older = {f["ticker"] for f in fills} - {s["ticker"] for s in settlements}
    return Snapshot(as_of=client.data["as_of_ts"], balance_cents=bal, portfolio_value_cents=pv,
                    deposits=client.get_deposits(), withdrawals=client.get_withdrawals(),
                    positions=client.get_positions(), fills=fills, settlements=settlements,
                    historical_markets=client.get_historical_markets(older))


def run_demo(out_dir: str = "demo_output") -> str:
    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, "back_office_demo.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    client = FixtureClient.bundled()
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        prior = client.data["prior_run"]          # yesterday's run, for daily P&L
        conn.execute("INSERT INTO recon_runs (run_ts, equity, net_deposits, "
                     "cash_identity_residual) VALUES (?,?,?,?)",
                     (prior["run_ts"], prior["equity"], prior["net_deposits"],
                      prior["cash_identity_residual"]))
        snap = snapshot_from(client)
        added = events.capture(conn, snap.fills, snap.settlements, client.get_resting_orders(),
                               now=snap.as_of)
        st = build(snap, conn)
    finally:
        conn.close()
    print("(demo: synthetic sample account, no API key used)\n")
    print(render_text(st))
    path = write_html(st, out_dir)
    print(f"\naudit log: {sum(added.values())} events captured {added}")
    print(f"HTML statement: {path}")
    return path
