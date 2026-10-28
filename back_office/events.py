"""Append-only trade-capture log (audit trail).

Each run snapshots the exchange's immutable records (fills, settlements) and the
current resting orders, and appends only events it has not seen before. Every
event has a UNIQUE natural key, so the job can run on any schedule, be re-run,
or be restarted without double-booking anything.

Order lifecycle reconstructed from polling:

    placed      order id first seen resting           key placed:<order_id>
    fill        an execution                          key <fill_id>
    closed      order no longer resting               key closed:<order_id>
                detail = filled (has >= 1 fill) | canceled (none)
    settlement  market resolved                       key settle:<ticker>:<settled_time>
"""
from __future__ import annotations

import sqlite3
import time

from .fields import fill_qty, fill_side, num


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS order_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_ts INTEGER, event_time TEXT, event_type TEXT,
        order_id TEXT, ticker TEXT, action TEXT, side TEXT,
        count REAL, price REAL, fill_id TEXT, detail TEXT,
        natural_key TEXT UNIQUE)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oe_order ON order_events(order_id)")
    conn.commit()


def _insert(conn, now, *, etype, nkey, etime="", order_id="", ticker="", action="",
            side="", count=0.0, price=0.0, fill_id="", detail="") -> int:
    cur = conn.execute(
        "INSERT OR IGNORE INTO order_events (captured_ts, event_time, event_type, order_id, "
        "ticker, action, side, count, price, fill_id, detail, natural_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, etime, etype, order_id, ticker, action, side, count, price, fill_id, detail, nkey))
    return cur.rowcount            # 1 if new, 0 if already logged


def capture(conn: sqlite3.Connection, fills: list[dict], settlements: list[dict],
            resting_orders: list[dict], now: int | None = None) -> dict:
    """Append new events; returns how many of each type were added."""
    init_db(conn)
    now = int(time.time()) if now is None else now
    added = {"fill": 0, "settlement": 0, "placed": 0, "closed": 0}

    filled_order_ids = set()
    for f in fills:
        filled_order_ids.add(f.get("order_id", ""))
        added["fill"] += _insert(
            conn, now, etype="fill", nkey=f["fill_id"], etime=f.get("created_time", ""),
            order_id=f.get("order_id", ""), ticker=f["ticker"], action=f.get("action", ""),
            side=fill_side(f), count=fill_qty(f), price=num(f.get("yes_price_dollars")),
            fill_id=f["fill_id"], detail="taker" if f.get("is_taker") else "maker")

    for st in settlements:
        added["settlement"] += _insert(
            conn, now, etype="settlement", nkey=f"settle:{st['ticker']}:{st.get('settled_time', '')}",
            etime=st.get("settled_time", ""), ticker=st["ticker"],
            detail=st.get("market_result", ""), price=num(st.get("revenue")) / 100.0)

    resting_ids = set()
    for o in resting_orders:
        oid = o["order_id"]
        resting_ids.add(oid)
        added["placed"] += _insert(
            conn, now, etype="placed", nkey=f"placed:{oid}", etime=o.get("created_time", ""),
            order_id=oid, ticker=o["ticker"], action=o.get("action", ""), side=fill_side(o),
            count=num(o.get("initial_count_fp")), price=num(o.get("yes_price_dollars")),
            detail=o.get("type", ""))

    # placed earlier, never closed, and no longer resting -> closed now
    still_open = {r[0] for r in conn.execute(
        "SELECT order_id FROM order_events WHERE event_type='placed' AND order_id NOT IN "
        "(SELECT order_id FROM order_events WHERE event_type='closed')")}
    for oid in still_open - resting_ids:
        added["closed"] += _insert(conn, now, etype="closed", nkey=f"closed:{oid}", order_id=oid,
                                   detail="filled" if oid in filled_order_ids else "canceled")
    conn.commit()
    return added
