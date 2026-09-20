#!/usr/bin/env python3
"""
Walk-forward parameter optimizer for an MT5 EA — disciplined, OOS-validated.

Phase 1: sweep a parameter grid on the IN-SAMPLE period (open-prices, parallel),
         rank by risk-adjusted return.
Phase 2: take the top candidates and re-test them on the OUT-OF-SAMPLE period
         they never saw. Keep only what holds up in BOTH -> guards against
         curve-fitting. The current/default params are run too, as a baseline.

Inputs are injected via the tester [TesterInputs] section (verified to work).
Uses the headless worker pool — no EA edits.
"""
import argparse
import itertools
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

import interlink_config as cfg
import headless_pool as hp


def _months(frm, to):
    fy, fm = int(frm[:4]), int(frm[5:7])
    ty, tm = int(to[:4]), int(to[5:7])
    return max(1, (ty - fy) * 12 + (tm - fm) + 1)


def _enrich(r, frm, to):
    if not r.get("ok"):
        return r
    init = r.get("initial_deposit") or 200000.0
    fin = init + r.get("net_profit", 0)
    n = _months(frm, to)
    r["ret_pct"] = round(r["net_profit"] / init * 100, 1)
    r["cmpd_pct_mo"] = round(((fin / init) ** (1.0 / n) - 1) * 100, 2) if fin > 0 else -99
    return r


def run_set(expert, symbol, period, frm, to, model, param_sets, timeout=400):
    """param_sets: list of (label, inputs_dict_or_None). Runs all in parallel."""
    workers = cfg.discover_workers()
    hp.sync_experts(workers, [expert])
    results, lock = [], threading.Lock()

    def run(item):
        label, inp = item
        task = {"expert": expert, "symbol": symbol, "period": period, "frm": frm,
                "to": to, "model": model, "deposit": "200000", "leverage": "100",
                "export": False, "pdf": False, "open": False, "inputs": inp}
        r = hp.run_task(task, timeout + 1200)   # self-leases a free worker
        r["label"] = label
        r["inputs"] = inp
        _enrich(r, frm, to)
        with lock:
            results.append(r)

    with ThreadPoolExecutor(max_workers=len(workers)) as ex:
        list(ex.map(run, param_sets))
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expert", default="NoWick_XAUUSD_Optimized.ex5")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--period", default="H2")
    ap.add_argument("--is-from", default="2018.01.01")
    ap.add_argument("--is-to", default="2022.12.31")
    ap.add_argument("--oos-from", default="2023.01.01")
    ap.add_argument("--oos-to", default="2025.12.31")
    ap.add_argument("--dd-cap", type=float, default=12.0, help="reject IS candidates above this max DD%%")
    ap.add_argument("--top", type=int, default=6, help="how many IS winners to validate OOS")
    a = ap.parse_args()

    # ── Parameter grid (the edge knobs; sizing/magic left alone) ──────────
    grid = {
        "InpWickTol": [3.0, 5.0, 7.0],
        "InpATRSL":   [1.0, 1.5, 2.0],
        "InpRR":      [1.5, 2.0, 2.5, 3.0],
    }
    combos = [dict(zip(grid.keys(), v)) for v in itertools.product(*grid.values())]
    param_sets = [("default", None)] + [
        (f"W{c['InpWickTol']}_SL{c['InpATRSL']}_RR{c['InpRR']}", c) for c in combos]

    print(f"# IN-SAMPLE sweep: {len(param_sets)} sets on {a.is_from}->{a.is_to} (open prices)")
    is_res = run_set(a.expert, a.symbol, a.period, a.is_from, a.is_to, "2", param_sets)
    ok = [r for r in is_res if r.get("ok")]
    within = [r for r in ok if r.get("max_dd_pct", 99) <= a.dd_cap]
    within.sort(key=lambda r: r.get("sharpe_ratio", 0), reverse=True)

    print(json.dumps({
        "in_sample": [{"label": r["label"], "ret_pct": r["ret_pct"],
                       "pct_mo": r["cmpd_pct_mo"], "dd": r["max_dd_pct"],
                       "pf": r["profit_factor"], "sharpe": r["sharpe_ratio"],
                       "trades": r["trades"], "inputs": r["inputs"]}
                      for r in within[:a.top]],
    }, indent=2, default=str))

    # ── OOS validation of the IS winners (+ default) ──────────────────────
    winners = within[:a.top]
    val_sets = [("default", None)]
    for r in winners:
        if r["label"] != "default":
            val_sets.append((r["label"], r["inputs"]))
    print(f"\n# OUT-OF-SAMPLE: {len(val_sets)} sets on {a.oos_from}->{a.oos_to}")
    oos_res = run_set(a.expert, a.symbol, a.period, a.oos_from, a.oos_to, "2", val_sets)
    oos_by = {r["label"]: r for r in oos_res if r.get("ok")}

    table = []
    for r in [x for x in ([next((w for w in winners if w["label"] == "default"), None)] + winners) if x]:
        lab = r["label"]
        o = oos_by.get(lab, {})
        table.append({"label": lab, "inputs": r["inputs"],
                      "IS_pct_mo": r["cmpd_pct_mo"], "IS_dd": r["max_dd_pct"], "IS_sharpe": r["sharpe_ratio"],
                      "OOS_pct_mo": o.get("cmpd_pct_mo"), "OOS_dd": o.get("max_dd_pct"), "OOS_sharpe": o.get("sharpe_ratio")})
    print(json.dumps({"comparison": table}, indent=2, default=str))


if __name__ == "__main__":
    main()
