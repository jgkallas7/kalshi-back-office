"""Daily account statement: reconciliation + day-over-day P&L + activity.

Daily P&L is the change in equity minus the change in net deposits since the
stored run from about 24 hours earlier, so funding the account never shows up as
a gain. Anchoring to the run ~24h ago (not simply the previous run) keeps the
number stable when the job is re-run several times in a day.

Output: a plain-text summary on stdout and an HTML statement file.
"""
from __future__ import annotations

import html
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .fields import fill_side, num
from .reconcile import (ReconResult, Snapshot, baseline_run, persist, previous_residual,
                        recheck, reconcile, residual_note)

DAY = 86_400
BASELINE_MIN_AGE = 72_000          # 20h: the baseline must be at least this old
BASELINE_MAX_AGE_H = 30            # older than this, a daily run was missed


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


@dataclass
class Activity:
    n_fills: int
    fees: float
    taker_fees: float
    notional: float
    n_settled: int

    @property
    def taker_pct(self) -> float:
        return 100 * self.taker_fees / self.fees if self.fees else 0.0

    @property
    def fee_rate(self) -> float:
        return 100 * self.fees / self.notional if self.notional else 0.0


def trailing_activity(s: Snapshot, since: int) -> Activity:
    fills = [f for f in s.fills if _ts(f.get("created_time", "1970-01-01T00:00:00Z")) >= since]
    setts = [x for x in s.settlements if _ts(x.get("settled_time", "1970-01-01T00:00:00Z")) >= since]
    fees = sum(num(f.get("fee_cost")) for f in fills)
    taker = sum(num(f.get("fee_cost")) for f in fills if f.get("is_taker"))
    # notional at the price actually paid for the contract traded
    notional = sum(num(f.get("count_fp")) * num(f.get("yes_price_dollars") if fill_side(f) == "yes"
                                                else f.get("no_price_dollars")) for f in fills)
    return Activity(len(fills), fees, taker, notional, len(setts))


@dataclass
class Statement:
    recon: ReconResult
    activity: Activity
    daily_pnl: float | None
    baseline_age_h: float | None = None

    @property
    def window(self) -> str:
        """'vs ~24h ago', or the real span when a missed run stretched it, so a
        two-day move is never presented as one day's P&L."""
        if self.baseline_age_h is None or self.baseline_age_h <= BASELINE_MAX_AGE_H:
            return "vs ~24h ago"
        return f"vs {self.baseline_age_h:.0f}h ago: a daily run was missed"

    @property
    def date(self) -> str:
        return datetime.fromtimestamp(self.recon.as_of, timezone.utc).strftime("%Y-%m-%d")


def build(s: Snapshot, conn: sqlite3.Connection, client=None) -> Statement:
    r = reconcile(s, previous_residual(conn, s.as_of))
    if client is not None:
        r = recheck(client, s, r)
    base = baseline_run(conn, r.as_of - BASELINE_MIN_AGE)
    daily = age_h = None
    if base:
        daily = (r.equity - base["equity"]) - (r.net_deposits - base["net_deposits"])
        age_h = (r.as_of - base["run_ts"]) / 3600
    persist(conn, r)
    return Statement(r, trailing_activity(s, r.as_of - DAY), daily, age_h)


def _z(x: float) -> float:
    """Avoid printing -0.00."""
    return 0.0 if abs(x) < 0.005 else x


def render_text(st: Statement) -> str:
    r, a = st.recon, st.activity
    dp = "n/a (no run ~24h ago)" if st.daily_pnl is None else f"${st.daily_pnl:+,.2f}"
    lines = [
        f"Kalshi Account Statement - {st.date}",
        f"Equity ${r.equity:,.2f}  (cash ${r.cash:,.2f} + positions ${r.portfolio_value:,.2f})",
        f"Daily P&L ({st.window}): {dp}",
        f"Lifetime cash P&L: ${r.cash_pnl:+,.2f}   net deposits ${r.net_deposits:,.2f}",
        f"Engine: realized ${r.engine_realized_net:+,.2f} net of fees, open ${_z(r.engine_open_pnl):+,.2f}",
        f"Non-trading cash (interest, rewards): ${_z(r.cash_identity_residual):+,.2f}  "
        f"({residual_note(r)})",
        f"Open: {r.open_positions} positions, exposure ${r.open_exposure:,.2f}",
        f"Last 24h: {a.n_fills} fills, fees ${a.fees:,.2f}, {a.n_settled} settlements",
        f"Fee drag (24h): {a.taker_pct:.0f}% of fees from taker fills, {a.fee_rate:.2f}% of notional",
    ]
    if r.breaks:
        lines.append(f"\n*** {len(r.breaks)} RECONCILIATION BREAK(S) ***")
        lines += [f"  [{b.kind}] {b.ticker}: {b.detail}" for b in r.breaks[:10]]
    else:
        lines.append("Reconciliation: clean (engine ties to the exchange and to cash)")
    return "\n".join(lines)


_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2328;
     background:#f6f8fa;margin:0;padding:32px}
.card{max-width:760px;margin:0 auto;background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:28px 32px}
h1{font-size:20px;margin:0 0 4px} .sub{color:#656d76;font-size:13px;margin-bottom:20px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:22px}
.kpi{border:1px solid #d0d7de;border-radius:8px;padding:10px 12px}
.kpi .l{font-size:11px;color:#656d76;text-transform:uppercase;letter-spacing:.04em}
.kpi .v{font-size:18px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
h2{font-size:14px;margin:22px 0 8px;text-transform:uppercase;letter-spacing:.04em;color:#656d76}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
td,th{padding:6px 8px;border-bottom:1px solid #eaeef2;text-align:left} th{color:#656d76;font-weight:500}
td.n,th.n{text-align:right} .pos{color:#1a7f37} .neg{color:#cf222e}
.ok{background:#dafbe1;color:#116329;border-radius:6px;padding:10px 12px;font-size:13px}
.bad{background:#ffebe9;color:#a40e26;border-radius:6px;padding:10px 12px;font-size:13px;margin-bottom:8px}
.foot{color:#8c959f;font-size:11px;margin-top:22px}
"""


def _money(x: float, signed: bool = False) -> str:
    x = 0.0 if abs(x) < 0.005 else x
    if signed:
        s = ("+" if x > 0 else "-" if x < 0 else "") + f"${abs(x):,.2f}"
    else:
        s = f"${x:,.2f}"
    cls = "" if not signed else ("pos" if x > 0 else "neg" if x < 0 else "")
    return f'<span class="{cls}">{s}</span>'


def render_html(st: Statement) -> str:
    r, a = st.recon, st.activity
    e = html.escape
    kpis = [
        ("Equity", _money(r.equity)),
        ("Daily P&amp;L" if st.window == "vs ~24h ago" else "P&amp;L since last run",
         "n/a" if st.daily_pnl is None else _money(st.daily_pnl, True)),
        ("Lifetime P&amp;L", _money(r.cash_pnl, True)),
        ("Breaks", str(len(r.breaks))),
    ]
    kpi_html = "".join(f'<div class="kpi"><div class="l">{label}</div><div class="v">{v}</div></div>'
                       for label, v in kpis)

    tie = [
        ("Cash (balance)", _money(r.cash)),
        ("Open positions at mark", _money(r.portfolio_value)),
        ("Net deposits", _money(r.net_deposits)),
        ("Lifetime cash P&amp;L", _money(r.cash_pnl, True)),
        ("Engine realized P&amp;L, net of fees", _money(r.engine_realized_net, True)),
        ("Engine open P&amp;L at mark", _money(r.engine_open_pnl, True)),
        ("Non-trading cash (interest, rewards)", _money(r.cash_identity_residual, True)),
    ]
    tie_html = "".join(f'<tr><td>{k}</td><td class="n">{v}</td></tr>' for k, v in tie)

    if r.breaks:
        rows = "".join(f"<tr><td>{e(b.kind)}</td><td>{e(b.ticker)}</td><td>{e(b.detail)}</td></tr>"
                       for b in r.breaks[:20])
        breaks_html = (f'<div class="bad">{len(r.breaks)} reconciliation break(s) need review</div>'
                       f"<table><tr><th>Type</th><th>Market</th><th>Detail</th></tr>{rows}</table>")
    else:
        breaks_html = '<div class="ok">Clean: the engine ties to the exchange and to cash.</div>'

    mkts = sorted(r.markets.values(), key=lambda m: -abs(m.realized_net))[:12]
    mrows = "".join(
        f"<tr><td>{e(m.ticker)}</td><td>{'settled' if m.settled else 'open'}</td>"
        f'<td class="n">{m.position:,.2f}</td><td class="n">{m.n_fills}</td>'
        f'<td class="n">{_money(m.fees)}</td><td class="n">{_money(m.realized_net, True)}</td></tr>'
        for m in mkts)

    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Account Statement {st.date}</title>
<style>{_CSS}</style></head><body><div class="card">
<h1>Kalshi Account Statement</h1>
<div class="sub">{st.date} &middot; {r.markets_reconciled} markets rebuilt from fills &middot;
{r.open_positions} open positions</div>
<div class="kpis">{kpi_html}</div>
<h2>Reconciliation</h2>{breaks_html}
<h2>Cash tie-out</h2><table>{tie_html}</table>
<h2>Last 24 hours</h2><table>
<tr><td>Fills</td><td class="n">{a.n_fills}</td></tr>
<tr><td>Fees paid</td><td class="n">{_money(a.fees)}</td></tr>
<tr><td>Share of fees from taker fills</td><td class="n">{a.taker_pct:.0f}%</td></tr>
<tr><td>Fees as % of notional</td><td class="n">{a.fee_rate:.2f}%</td></tr>
<tr><td>Markets settled</td><td class="n">{a.n_settled}</td></tr></table>
<h2>Markets by realized P&amp;L</h2><table>
<tr><th>Market</th><th>Status</th><th class="n">Position</th><th class="n">Fills</th>
<th class="n">Fees</th><th class="n">Realized (net)</th></tr>{mrows}</table>
<div class="foot">Generated by back_office.statement</div>
</div></body></html>"""


def write_html(st: Statement, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"statement_{st.date}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_html(st))
    return path
