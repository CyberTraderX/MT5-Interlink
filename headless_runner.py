#!/usr/bin/env python3
"""
MT5-Interlink headless backtest runner — GENERAL, no per-EA code required.

Drives a SEPARATE portable MT5 instance (C:\\MT5_Headless) via /config so the
live GUI terminal + BridgePro are never touched. MT5 writes its OWN native HTML
report; we parse it into the same bt_results JSON schema the bridge already uses.

Works for ANY compiled .ex5 — proven strategies or brand-new algos — with zero
source edits and zero OnTester() include. Authoritative numbers (PF, Sharpe, DD,
best/worst trade) come straight from MT5's report.

Usage:
    python headless_runner.py --expert NoWick_XAUUSD_Optimized.ex5 \
        --symbol XAUUSD --period H2 --from 2020.01.01 --to 2025.12.31

    # batch (sequential): pass several --expert
    python headless_runner.py --expert A.ex5 --expert B.ex5 ...
"""
import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

import interlink_config as cfg   # single source of truth

HEADLESS_DIR = cfg.WORKER_ROOT
TERMINAL     = os.path.join(HEADLESS_DIR, "terminal64.exe")
REPORT_NAME  = "headless_report"
REPORT_HTM   = os.path.join(HEADLESS_DIR, REPORT_NAME + ".htm")
BT_RESULTS   = cfg.BT_RESULTS


# ── report parsing ──────────────────────────────────────────────────────────

def _num(s):
    """'20 954.58' / '-2 256.86' / '4.86%' -> float."""
    if s is None:
        return 0.0
    s = s.replace(" ", "").replace(" ", "").replace(" ", "")
    s = s.replace("%", "").replace(",", "")
    m = re.search(r"-?\d+\.?\d*", s)
    return float(m.group(0)) if m else 0.0


def _field(html, label):
    """Value inside the first <b>..</b> after `label`. Returns raw inner text."""
    m = re.search(re.escape(label) + r".*?<b>(.*?)</b>", html, re.S)
    return m.group(1) if m else None


def _won(raw):
    """'11 (45.45%)' -> (count=11, pct=45.45)."""
    if not raw:
        return 0, 0.0
    m = re.search(r"(-?\d[\d   ]*)\s*\((-?\d+\.?\d*)%?\)", raw)
    if m:
        return int(_num(m.group(1))), float(m.group(2))
    return int(_num(raw)), 0.0


def monthly_grid(html, initial):
    """Month-by-month % returns from the report's Deals table (balance column of
    'out' rows). House rule: every result includes the monthly grid.
    Returns ordered {"YYYY-MM": pct} computed on month-start balance."""
    bal_by_t = []
    for row in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.split(r"</td>", row)]
        if len(cells) < 12 or cells[4] not in ("in", "out"):
            continue
        m = re.match(r"(\d{4})\.(\d{2})\.\d{2} \d{2}:\d{2}:\d{2}", cells[0])
        if not m:
            continue
        bal = _num(cells[11])
        if cells[4] == "out" and bal:
            bal_by_t.append((f"{m.group(1)}-{m.group(2)}", bal))
    if not bal_by_t:
        return {}
    out, start_bal = {}, float(initial) or 1.0
    cur_month, last_bal = bal_by_t[0][0], start_bal
    for month, bal in bal_by_t:
        if month != cur_month:
            out[cur_month] = round((last_bal / start_bal - 1) * 100, 2)
            cur_month, start_bal = month, last_bal
        last_bal = bal
    out[cur_month] = round((last_bal / start_bal - 1) * 100, 2)
    return out


def _years_between(start_date, end_date):
    try:
        a = datetime.strptime(start_date, "%Y.%m.%d")
        b = datetime.strptime(end_date, "%Y.%m.%d")
        return max((b - a).days / 365.25, 1e-9)
    except Exception:
        return None


def parse_report(htm_path, ea, symbol, timeframe, start_date, end_date, deposit):
    with open(htm_path, "r", encoding="utf-16", errors="ignore") as f:
        html = f.read()

    net   = _num(_field(html, "Total Net Profit:"))
    gp    = _num(_field(html, "Gross Profit:"))
    gl    = _num(_field(html, "Gross Loss:"))            # negative
    pf    = _num(_field(html, "Profit Factor:"))
    payoff= _num(_field(html, "Expected Payoff:"))
    recov = _num(_field(html, "Recovery Factor:"))
    sharpe= _num(_field(html, "Sharpe Ratio:"))

    bal_dd_raw = _field(html, "Balance Drawdown Maximal:")
    eq_dd_raw  = _field(html, "Equity Drawdown Maximal:")
    bal_dd_abs = _num(bal_dd_raw)
    bal_dd_pct = _won(bal_dd_raw)[1]
    eq_dd_abs  = _num(eq_dd_raw)
    eq_dd_pct  = _won(eq_dd_raw)[1]

    trades = int(_num(_field(html, "Total Trades:")))
    short_trades, short_pct = _won(_field(html, "Short Trades (won %):"))
    long_trades,  long_pct  = _won(_field(html, "Long Trades (won %):"))
    win_trades,   win_pct   = _won(_field(html, "Profit Trades (% of total):"))
    loss_trades,  _         = _won(_field(html, "Loss Trades (% of total):"))

    avg_win  = _num(_field(html, "Average profit trade:"))
    avg_loss = _num(_field(html, "Average loss trade:"))    # negative
    big_win  = _num(_field(html, "Largest profit trade:"))
    big_loss = _num(_field(html, "Largest loss trade:"))    # negative
    con_wins   = _won(_field(html, "Maximum consecutive wins ($):"))[0]
    con_losses = _won(_field(html, "Maximum consecutive losses ($):"))[0]

    init_dep = _num(_field(html, "Initial Deposit:")) or float(deposit)
    final_bal = init_dep + net

    # CAGR + MAR (house metrics) and the mandatory monthly grid
    ret_pct = net / init_dep * 100 if init_dep else 0.0
    yrs = _years_between(start_date, end_date)
    cagr = (((1 + ret_pct / 100.0) ** (1 / yrs) - 1) * 100
            if yrs and ret_pct > -100 else 0.0)
    mar = round(cagr / eq_dd_pct, 4) if eq_dd_pct > 0 else 0.0

    return {
        "cagr_pct": round(cagr, 4),
        "mar": mar,
        "monthly_pct": monthly_grid(html, init_dep),
        "ea": ea, "symbol": symbol, "timeframe": timeframe,
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y.%m.%d %H:%M:%S"),
        "start_date": start_date, "end_date": end_date,
        "initial_deposit": round(init_dep, 2),
        "final_balance": round(final_bal, 2),
        "net_profit": round(net, 2),
        "return_pct": round(net / init_dep * 100, 4) if init_dep else 0,
        "gross_profit": round(gp, 2),
        "gross_loss": round(gl, 2),
        "profit_factor": round(pf, 4),
        "sharpe_ratio": round(sharpe, 4),
        "recovery_factor": round(recov, 4),
        "expected_payoff": round(payoff, 2),
        "max_dd_abs": round(eq_dd_abs, 2),
        "max_dd_pct": round(eq_dd_pct, 4),
        "balance_dd_abs": round(bal_dd_abs, 2),
        "balance_dd_rel_pct": round(bal_dd_pct, 4),
        "equity_dd_abs": round(eq_dd_abs, 2),
        "equity_dd_rel_pct": round(eq_dd_pct, 4),
        "trades": trades,
        "win_trades": win_trades,
        "loss_trades": loss_trades,
        "win_rate_pct": round(win_pct, 4),
        "long_trades": long_trades,
        "long_wins": round(long_trades * long_pct / 100),
        "short_trades": short_trades,
        "short_wins": round(short_trades * short_pct / 100),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_win_loss_ratio": round(avg_win / abs(avg_loss), 4) if avg_loss else 0,
        "largest_win": round(big_win, 2),
        "largest_loss": round(big_loss, 2),
        "max_con_wins": con_wins,
        "max_con_losses": con_losses,
    }


# ── INI + run ─────────────────────────────────────────────────────────────

def write_ini(expert, symbol, period, frm, to, model, deposit, leverage,
              workdir=HEADLESS_DIR):
    ini = os.path.join(workdir, "headless_test.ini")
    with open(ini, "w", encoding="ascii") as f:
        f.write(
            f"[Common]\nLogin={cfg.LOGIN}\nServer={cfg.SERVER}\n\n"
            "[Tester]\n"
            f"Expert={expert}\nSymbol={symbol}\nPeriod={period}\n"
            f"Model={model}\nOptimization=0\n"
            f"FromDate={frm}\nToDate={to}\n"
            f"Deposit={deposit}\nCurrency=USD\nLeverage={leverage}\n"
            f"Report={REPORT_NAME}\nReplaceReport=1\nShutdownTerminal=1\n"
        )
    return ini


def run_one(expert, symbol, period, frm, to, model, deposit, leverage, timeout):
    """Run one backtest on a LEASED worker (cross-session safe).

    Historically this ran directly in C:\\MT5_Headless with no lease — if a
    pool in another session had that worker leased, this stomped it (deleted
    its report, launched a second terminal in the same dir -> cross-wired
    results). Now it leases any free worker via the pool's primitives.
    Lazy import avoids a module-load cycle (headless_pool imports this file)."""
    import headless_pool as hp

    worker = hp.acquire_worker(timeout=600)
    if worker is None:
        return {"expert": expert,
                "error": "no free worker (all leased by other sessions)"}
    try:
        hp._kill_in_worker(worker)      # clear lingering terminal (else our launch hands off and dies)
        ea_err = hp._ensure_ea(worker, expert)
        if ea_err:
            return {"expert": expert, "error": ea_err}
        report_htm = os.path.join(worker, REPORT_NAME + ".htm")
        for ext in (".htm", ".html"):
            p = os.path.join(worker, REPORT_NAME + ext)
            if os.path.exists(p):
                os.remove(p)
        ini = write_ini(expert, symbol, period, frm, to, model, deposit,
                        leverage, workdir=worker)
        # Launch through the same cross-session gate + stagger as the pool —
        # otherwise this terminal races a pool-launched one for the tester
        # agent port (bind error 10048 -> empty/no report).
        gate = hp._launch_gate()
        t0 = time.time()
        try:
            proc = subprocess.Popen([os.path.join(worker, "terminal64.exe"),
                                     "/portable", f"/config:{ini}"])
            time.sleep(hp.STAGGER_SEC)
        finally:
            hp._release_gate(gate)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"expert": expert, "error": f"timeout after {timeout}s"}
        # terminal64 may hand off to a respawned instance that writes the
        # report seconds after our child exits — settle before judging.
        hp._settle_report(worker, report_htm, grace=min(90, timeout))
        elapsed = round(time.time() - t0, 1)

        if not os.path.exists(report_htm):
            return {"expert": expert, "error": "no report produced",
                    "elapsed_sec": elapsed}

        ea_name = expert.replace(".ex5", "")
        result = parse_report(report_htm, ea_name, symbol, period, frm, to, deposit)
        result["elapsed_sec"] = elapsed
        result["worker"] = worker
        result["tag"] = hp.RUN_TAG          # session ownership marker

        # persist to bt_results (same folder the bridge /bt_results endpoint reads)
        os.makedirs(BT_RESULTS, exist_ok=True)
        stamp = datetime.now().strftime("%Y.%m.%d_%H-%M-%S")
        out = os.path.join(BT_RESULTS,
                           f"{ea_name}_{symbol}_{period}_{stamp}_{hp.RUN_TAG}_headless.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        result["_file"] = out
        return result
    finally:
        hp.release_worker(worker)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expert", action="append", required=True, help="EA .ex5 (repeatable)")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--period", default="H2")
    ap.add_argument("--from", dest="frm", default="2020.01.01")
    ap.add_argument("--to", default="2025.12.31")
    ap.add_argument("--model", default="0", help="0=every tick (default) 1=1min OHLC 2=open 4=real ticks")
    ap.add_argument("--deposit", default="200000")
    ap.add_argument("--leverage", default="100")
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args()

    runs = []
    for ea in a.expert:
        runs.append(run_one(ea, a.symbol, a.period, a.frm, a.to,
                            a.model, a.deposit, a.leverage, a.timeout))
    print(json.dumps({"runs": runs}, indent=2))


if __name__ == "__main__":
    main()
