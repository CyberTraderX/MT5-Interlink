"""
Render a balance + equity curve from an MT5 native report.

MT5's headless report exports the running Balance (per deal) but NOT the equity
time-series. We reconstruct the green Equity line ourselves: pair the Deals into
trades (open/close time, price, direction, volume), pull the symbol's price bars
via the MetaTrader5 API, and compute floating P&L through each open trade so the
equity line dips/rises intrabar exactly like the Strategy Tester graph.

General: no EA edits. If the price API isn't available it falls back to a clean
balance-only curve.
"""
import os
import re
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


def _num(s):
    s = s.replace(" ", "").replace("\xa0", "").replace(" ", "").replace(",", "")
    m = re.search(r"-?\d+\.?\d*", s)
    return float(m.group(0)) if m else None


def _deal_rows(html):
    """Yield parsed cell lists for every Deals-table data row (in/out)."""
    for row in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.split(r"</td>", row)]
        if len(cells) < 12:
            continue
        m = re.match(r"\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}", cells[0])
        if not m or cells[4] not in ("in", "out"):
            continue
        yield cells, datetime.strptime(m.group(0), "%Y.%m.%d %H:%M:%S")


def _parse_trades(html):
    """Pair in/out deals into trades and build the balance step series.

    Robust pairing: deals are processed in chronological order, and each close is
    matched to the earliest still-open leg that actually opened at/before it. This
    avoids FIFO desync (e.g. a close + a new stop-fill on the same bar) that could
    otherwise pair an open with a far-later close — producing phantom multi-week
    'open' windows and bogus floating-equity swings.
    """
    bal_t, bal_v = [], []
    opens = []           # queue of open legs: (time, sign, vol, price)
    trades = []          # {open_t, close_t, sign, vol, open_p, base_bal}
    running_base = None   # balance before the currently-closing trade

    # collect then sort chronologically (report order isn't always time order)
    deals = []
    for cells, t in _deal_rows(html):
        deals.append((t, cells[4], cells[3], _num(cells[5]) or 0.0,
                      _num(cells[6]) or 0.0, _num(cells[11])))
    deals.sort(key=lambda d: d[0])

    for t, direction, typ, vol, price, bal in deals:
        if direction == "in":
            sign = 1 if typ == "buy" else -1
            opens.append((t, sign, vol, price))
            if running_base is None and bal is not None:
                running_base = bal          # balance at first entry == start balance
        else:  # out — match earliest leg opened at/before this close (never negative dur)
            idx = next((k for k, o in enumerate(opens) if o[0] <= t), None)
            if idx is not None:
                ot, sign, ovol, op = opens.pop(idx)
                trades.append({"open_t": ot, "close_t": t, "sign": sign,
                               "vol": ovol, "open_p": op,
                               "base_bal": running_base if running_base is not None else bal})
            if bal is not None:
                bal_t.append(t)
                bal_v.append(bal)
                running_base = bal          # next trade's base balance
    return trades, bal_t, bal_v


# MetaTrader5 timeframe map (lazy import so balance-only mode needs no MT5)
def _tf(period):
    import MetaTrader5 as mt5
    return {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H2": mt5.TIMEFRAME_H2,
            "H3": mt5.TIMEFRAME_H3, "H4": mt5.TIMEFRAME_H4, "H6": mt5.TIMEFRAME_H6,
            "H8": mt5.TIMEFRAME_H8, "H12": mt5.TIMEFRAME_H12, "D1": mt5.TIMEFRAME_D1,
            "W1": mt5.TIMEFRAME_W1, "MN1": mt5.TIMEFRAME_MN1}.get(period.upper())


def _equity_series(trades, bal_t, bal_v, initial, symbol, period):
    """Reconstruct (times, equity): realised balance (step) + floating P&L of any
    open trade, valued against the symbol's price bars."""
    if not trades:
        return None
    try:
        import bisect
        import MetaTrader5 as mt5
        if not mt5.initialize():
            return None
        tf = _tf(period)
        info = mt5.symbol_info(symbol)
        contract = info.trade_contract_size if info else 100.0
        start = min(t["open_t"] for t in trades)
        end = max(t["close_t"] for t in trades)
        rates = mt5.copy_rates_range(symbol, tf, start, end)
        if rates is None or len(rates) == 0:
            return None
    except Exception:
        return None

    def realised_at(bt):
        i = bisect.bisect_right(bal_t, bt) - 1   # last close at/<= bt
        return bal_v[i] if i >= 0 else initial

    et, ev = [], []
    for r in rates:
        bt = datetime.utcfromtimestamp(int(r["time"]))
        close_px = float(r["close"])
        floating = 0.0
        for tr in trades:
            if tr["open_t"] <= bt < tr["close_t"]:
                floating += tr["sign"] * (close_px - tr["open_p"]) * tr["vol"] * contract
        et.append(bt)
        ev.append(realised_at(bt) + floating)
    return et, ev


def render_from_report(htm_path, out_png, title="", symbol=None, period=None):
    with open(htm_path, "r", encoding="utf-16", errors="ignore") as f:
        html = f.read()
    trades, bal_t, bal_v = _parse_trades(html)
    if len(bal_t) < 2:
        return None
    initial = trades[0]["base_bal"] if trades else bal_v[0]

    fig, ax = plt.subplots(figsize=(12, 4.2), dpi=110)

    eq = _equity_series(trades, bal_t, bal_v, initial, symbol, period) if (symbol and period) else None
    if eq and len(eq[0]) > 1:
        ax.plot(eq[0], eq[1], color="#11a05a", linewidth=0.8, label="Equity", zorder=1)
    ax.plot(bal_t, bal_v, color="#1f5fd6", linewidth=1.3, label="Balance", zorder=2)
    ax.fill_between(bal_t, bal_v, min(bal_v), color="#1f5fd6", alpha=0.05)

    ax.set_title(title, fontsize=11, loc="left", color="#222")
    ax.grid(True, linestyle=":", linewidth=0.6, color="#bbb")
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y.%m"))
    ax.tick_params(labelsize=8)
    ax.margins(x=0.01)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(out_png, facecolor="white")
    plt.close(fig)
    return out_png if os.path.exists(out_png) else None
