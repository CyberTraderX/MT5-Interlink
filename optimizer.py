#!/usr/bin/env python3
"""
MT5-Interlink OPTIMIZER — Claude-powered, robust parameter optimization.

TWO ENGINES:
  * engine="native" — runs ONE in-process MT5 optimization (genetic or complete)
    over the whole space; handles 100k+ combos (genetic prunes). Results come
    from MT5's OFFICIAL all-passes XML (set Report=opt_report -> opt_report.xml
    in optimization mode), parsed cleanly (no binary .opt, no EA changes). Then
    the top finalists are confirmed EVERY-TICK by the Python layer below.
  * engine="python" — runs the SEARCH in Python over the hardened parallel pool
    (one native report per candidate); best for smart search (genetic/coordinate)
    and full multi-metric scoring at every step.

Both give us things the native optimizer alone can't:
  * MULTI-OBJECTIVE: rank on any expression of the metrics
        e.g. "mar if dd<10 and worst<-... " — constraints + composite goals.
  * WALK-FORWARD (IS/OOS): score in-sample, then confirm out-of-sample, and
    penalise configs whose OOS fades vs IS (anti-curve-fit, our core rule).
  * HYBRID SPEED: broad search on the fast open-price model (MT5's quickest),
    then re-confirm finalists EVERY-TICK (the only trustworthy verdict).

Search strategies: grid (exhaustive), random, coordinate (hill-climb per axis),
genetic (tournament + crossover + mutation).

Metric namespace available to objective/constraint expressions (per run):
    net  ret  cagr  pf  sharpe  dd  mar  trades  win  wl  best  worst
(ret=return %, dd=max DD %, mar=cagr/dd, wl=avg win/loss, best/worst=trade $)

CLI example:
    python optimizer.py --ea IntradayNoiseArea_HardStop.ex5 --symbol NDX100 \
      --period H1 --from 2021.01.01 --to 2026.06.01 \
      --param InpVolMultiplier=1.0:1.4:0.1 --param InpRiskPercent=0.30:0.55:0.01 \
      --fixed InpStopMode=1 --objective "mar if dd<10 and trades>200 else -1e9" \
      --search genetic --oos-split 2024.09.01 --confirm-top 5
"""
import argparse, itertools, json, os, random, threading, time
from concurrent.futures import ThreadPoolExecutor
import interlink_config as cfg
import headless_pool as hp

YEAR_DAYS = 365.25


# ── metric extraction ─────────────────────────────────────────────────────
def _years(frm, to):
    from datetime import datetime
    a = datetime.strptime(frm, "%Y.%m.%d"); b = datetime.strptime(to, "%Y.%m.%d")
    return max((b - a).days / YEAR_DAYS, 1e-9)


def metrics_from(res, frm, to):
    """Map a run_task result -> the objective namespace (None if run failed)."""
    if not res or not res.get("ok"):
        return None
    ret = res.get("return_pct", 0.0)
    dd = res.get("max_dd_pct", 0.0) or 0.0
    yrs = _years(frm, to)
    cagr = ((1 + ret / 100.0) ** (1 / yrs) - 1) * 100 if ret > -100 else -100.0
    return {
        "net": res.get("net_profit", 0.0), "ret": ret, "cagr": cagr,
        "pf": res.get("profit_factor", 0.0), "sharpe": res.get("sharpe_ratio", 0.0),
        "dd": dd, "mar": (cagr / dd if dd > 0 else 0.0),
        "trades": res.get("trades", 0), "win": res.get("win_rate_pct", 0.0),
        "wl": res.get("avg_win_loss_ratio", 0.0),
        "best": res.get("largest_win", 0.0), "worst": res.get("largest_loss", 0.0),
    }


def score(metrics, objective):
    """Evaluate the objective expression in the metric namespace. Failures -> -inf."""
    if metrics is None:
        return float("-inf")
    try:
        v = eval(objective, {"__builtins__": {}}, dict(metrics))  # sandboxed names only
        return float(v)
    except Exception:
        return float("-inf")


# ── candidate evaluation (cached, retried) ─────────────────────────────────
class Evaluator:
    def __init__(self, ea, symbol, period, deposit, leverage, fixed, timeout=2600, retries=4):
        self.ea, self.symbol, self.period = ea, symbol, period
        self.deposit, self.leverage = deposit, leverage
        self.fixed = fixed or {}
        self.timeout, self.retries = timeout, retries
        self.cache = {}          # (combo_key, frm, to, model) -> metrics
        self.lock = threading.Lock()
        self.n_runs = 0
        hp.sync_experts(cfg.discover_workers(), [ea])

    def _key(self, params, frm, to, model):
        return (tuple(sorted(params.items())), frm, to, model)

    def eval(self, params, frm, to, model):
        k = self._key(params, frm, to, model)
        with self.lock:
            if k in self.cache:
                return self.cache[k]
        inputs = dict(self.fixed); inputs.update(params)
        res = None
        for _ in range(self.retries):
            t = {"expert": self.ea, "symbol": self.symbol, "period": self.period,
                 "frm": frm, "to": to, "model": str(model), "deposit": str(self.deposit),
                 "leverage": str(self.leverage), "export": False, "pdf": False,
                 "open": False, "inputs": inputs}
            res = hp.run_task(t, self.timeout)
            if res and res.get("ok"):
                break
            time.sleep(2)
        m = metrics_from(res, frm, to)
        with self.lock:
            self.cache[k] = m; self.n_runs += 1
        return m


# ── parameter space ────────────────────────────────────────────────────────
def expand_axis(spec):
    """'1.0:1.4:0.1' -> [1.0,1.1,...] ; '0,1,2' -> [0,1,2] ; '1.5' -> [1.5]."""
    spec = str(spec)
    if ":" in spec:
        lo, hi, step = (float(x) for x in spec.split(":"))
        vals, v, i = [], lo, 0
        while v <= hi + 1e-9:
            vals.append(round(v, 10)); i += 1; v = lo + i * step
        return vals
    if "," in spec:
        out = []
        for x in spec.split(","):
            try: out.append(int(x))
            except ValueError: out.append(float(x))
        return out
    try: return [int(spec)]
    except ValueError:
        try: return [float(spec)]
        except ValueError: return [spec]


def _combo_str(params):
    return " ".join(f"{k}={v}" for k, v in params.items())


# ── search strategies (return list of param-dicts to try) ──────────────────
def grid_combos(space):
    keys = list(space)
    return [dict(zip(keys, vals)) for vals in itertools.product(*[space[k] for k in keys])]


def random_combos(space, n):
    keys = list(space)
    seen, out = set(), []
    tries = 0
    while len(out) < n and tries < n * 50:
        c = {k: random.choice(space[k]) for k in keys}
        t = tuple(sorted(c.items()))
        if t not in seen:
            seen.add(t); out.append(c)
        tries += 1
    return out


def _eval_batch(ev, combos, frm, to, model, max_par, log):
    """Evaluate combos in parallel; returns list of (params, metrics)."""
    out = []
    lock = threading.Lock()
    def fn(c):
        m = ev.eval(c, frm, to, model)
        with lock:
            out.append((c, m))
            if log:
                s = "FAIL" if m is None else (f"ret{m['ret']:+.0f}% dd{m['dd']:.1f}% "
                                              f"pf{m['pf']:.2f} mar{m['mar']:.2f} n{m['trades']}")
                print(f"    [{len(out):>3}/{len(combos)}] {_combo_str(c):<48} {s}", flush=True)
    pool = min(max_par, len(combos)) or 1
    with ThreadPoolExecutor(max_workers=pool) as ex:
        list(ex.map(fn, combos))
    return out


def genetic_search(ev, space, objective, frm, to, model, max_par, log,
                   pop=12, gens=5, elite=3, mut=0.3):
    keys = list(space)
    population = random_combos(space, pop)
    best_hist = []
    for g in range(gens):
        if log: print(f"  [genetic] generation {g+1}/{gens} (pop {len(population)})", flush=True)
        scored = [(c, score(m, objective), m) for c, m in
                  _eval_batch(ev, population, frm, to, model, max_par, log)]
        scored.sort(key=lambda x: x[1], reverse=True)
        best_hist.append(scored[0])
        parents = [c for c, s, m in scored[:max(elite, pop // 2)] if s > float("-inf")]
        if not parents:
            parents = [c for c, s, m in scored[:max(elite, 2)]]
        nxt = [c for c, s, m in scored[:elite]]            # carry elites
        while len(nxt) < pop:
            a, b = random.choice(parents), random.choice(parents)
            child = {k: (a[k] if random.random() < 0.5 else b[k]) for k in keys}
            if random.random() < mut:                       # mutate one gene
                gk = random.choice(keys); child[gk] = random.choice(space[gk])
            nxt.append(child)
        population = nxt
    best_hist.sort(key=lambda x: x[1], reverse=True)
    return best_hist[0]    # (params, score, metrics)


# ── NATIVE in-process optimization (MT5 genetic/complete) ──────────────────
# Runs ALL passes inside ONE terminal (no per-combo launch) -> handles 100k+
# spaces. Results come from the official all-passes XML (Report=opt_report.xml),
# parsed below. This is the broad-search front end for huge parameter grids.
import subprocess as _sp
_OPT_STD_COLS = {"Pass", "Result", "Profit", "Expected Payoff", "Profit Factor",
                 "Recovery Factor", "Sharpe Ratio", "Custom", "Equity DD %",
                 "Equity DD", "Drawdown", "Trades", "Back Result", "Forward Result"}


def parse_opt_xml(path, deposit, frm, to):
    """Parse MT5's optimization Report XML (SpreadsheetML) -> [(params, metrics)]."""
    import re
    txt = open(path, encoding="utf-8", errors="ignore").read()
    rows = re.findall(r"<Row[^>]*>(.*?)</Row>", txt, re.S)
    if not rows:
        return []
    def cells(r): return [re.sub(r"\s+", " ", c).strip()
                          for c in re.findall(r"<Data[^>]*>(.*?)</Data>", r, re.S)]
    header = cells(rows[0])
    dep = float(deposit); yrs = _years(frm, to)
    param_cols = [h for h in header if h not in _OPT_STD_COLS]
    out = []
    for r in rows[1:]:
        v = cells(r)
        if len(v) < len(header):
            continue
        d = dict(zip(header, v))
        def num(key, dflt=0.0):
            try: return float(d.get(key, dflt))
            except Exception: return dflt
        profit = num("Profit")
        dd = num("Equity DD %", num("Drawdown"))
        trades = int(num("Trades"))
        ret = profit / dep * 100.0
        cagr = ((1 + ret / 100.0) ** (1 / yrs) - 1) * 100 if ret > -100 else -100.0
        m = {"net": profit, "ret": ret, "cagr": cagr, "pf": num("Profit Factor"),
             "sharpe": num("Sharpe Ratio"), "dd": dd, "mar": (cagr / dd if dd > 0 else 0.0),
             "trades": trades, "recovery": num("Recovery Factor"),
             "custom": num("Custom"), "win": 0.0, "wl": 0.0, "best": 0.0, "worst": 0.0}
        params = {}
        for k in param_cols:
            s = d.get(k, "")
            try: params[k] = int(s) if s and float(s) == int(float(s)) else float(s)
            except Exception: params[k] = s
        out.append((params, m))
    return out


def _axis_range(vals):
    """List of grid values -> (start, step, stop) for the native ||start||step||stop|| form."""
    if len(vals) == 1:
        return vals[0], 0, vals[0]
    step = round(vals[1] - vals[0], 10)
    return vals[0], step, vals[-1]


def write_opt_ini(worker, ea, symbol, period, frm, to, model, deposit, leverage,
                  space, fixed, optimization=2, criterion=0, report="opt_report"):
    ini = os.path.join(worker, "opt_run.ini")
    with open(ini, "w", encoding="ascii") as f:
        f.write(
            f"[Common]\nLogin={cfg.LOGIN}\nServer={cfg.SERVER}\n\n"
            "[Tester]\n"
            f"Expert={ea}\nSymbol={symbol}\nPeriod={period}\n"
            f"Model={model}\nOptimization={optimization}\nOptimizationCriterion={criterion}\n"
            f"FromDate={frm}\nToDate={to}\nForwardMode=0\n"
            f"Report={report}\nReplaceReport=1\nShutdownTerminal=1\n"
            f"Deposit={deposit}\nCurrency=USD\nLeverage={leverage}\n"
            "\n[TesterInputs]\n")
        for k, v in (fixed or {}).items():
            f.write(f"{k}={v}\n")
        for k, vals in space.items():
            start, step, stop = _axis_range(vals)
            f.write(f"{k}={start}||{start}||{step}||{stop}||Y\n")
    return ini


def run_native_opt(ea, symbol, period, frm, to, space, fixed, deposit, leverage,
                   optimization=2, criterion=0, model=1, timeout=5400, log=True):
    """Lease one worker, run a native optimization (all passes in-process), return
    parsed passes [(params, metrics)] from the official Report XML."""
    hp.sync_experts(cfg.discover_workers(), [ea])
    w = hp.acquire_worker(1800)
    if not w:
        return []
    stop_hb = threading.Event()
    threading.Thread(target=hp._heartbeat_lease, args=(w, stop_hb), daemon=True).start()
    try:
        hp._kill_in_worker(w)
        xml = os.path.join(w, "opt_report.xml")
        try: os.remove(xml)
        except Exception: pass
        ini = write_opt_ini(w, ea, symbol, period, frm, to, model, deposit, leverage,
                            space, fixed, optimization, criterion)
        gate = hp._launch_gate()
        try:
            p = _sp.Popen([os.path.join(w, "terminal64.exe"), "/portable", f"/config:{ini}"])
            time.sleep(hp.STAGGER_SEC)
        finally:
            hp._release_gate(gate)
        try:
            p.wait(timeout=timeout)
        except _sp.TimeoutExpired:
            p.kill();
            if log: print("  [native] TIMEOUT", flush=True)
            return []
        if not os.path.exists(xml):
            if log: print("  [native] no opt_report.xml produced", flush=True)
            return []
        passes = parse_opt_xml(xml, deposit, frm, to)
        if log: print(f"  [native] {len(passes)} passes parsed from XML", flush=True)
        return passes
    finally:
        stop_hb.set()
        hp.release_worker(w)


# ── top-level optimize (hybrid: broad search -> every-tick + OOS confirm) ──
def optimize(ea, symbol, period, frm, to, space, objective, fixed=None,
             search="genetic", screen_model=2, confirm_model=0,
             oos_split=None, confirm_top=5, max_par=3, deposit="200000",
             leverage="100", log=True, gpop=12, ggens=5,
             engine="python", optimization=2, criterion=0):
    ev = Evaluator(ea, symbol, period, deposit, leverage, fixed)
    n_space = 1
    for k in space: n_space *= len(space[k])
    if log:
        print(f"\n=== OPTIMIZE {ea} {symbol} {period} {frm}..{to} ===")
        print(f"space={n_space} combos  engine={engine}  search={search}  "
              f"confirm=model{confirm_model}  objective='{objective}'", flush=True)

    # ---- ENGINE = NATIVE: one in-process MT5 optimization over the whole space.
    # Handles 100k+ spaces (genetic prunes). Ranks all passes from the official XML.
    if engine == "native":
        nmodel = int(screen_model) if str(screen_model) != "2" else 1   # model2 breaks intraday
        opt_mode = optimization if n_space > 200 else 1                 # tiny space -> complete
        # TRUE walk-forward: search the IS window only; OOS stays genuinely held out.
        search_to = oos_split if oos_split else to
        if log: print(f"  [native] launching MT5 {'genetic' if opt_mode==2 else 'complete'} "
                      f"optimization on model{nmodel} (criterion {criterion}) "
                      f"over IS {frm}..{search_to}...", flush=True)
        passes = run_native_opt(ea, symbol, period, frm, search_to, space, fixed, deposit,
                                leverage, opt_mode, criterion, nmodel, log=log)
        if not passes and nmodel != 0:                                  # escalate if nothing
            if log: print("  [native] retry on model0 (every-tick)...", flush=True)
            passes = run_native_opt(ea, symbol, period, frm, search_to, space, fixed, deposit,
                                    leverage, opt_mode, criterion, 0, log=log)
        scored = [(p, score(m, objective), m) for p, m in passes]
        scored.sort(key=lambda x: x[1], reverse=True)
        finalists = [p for p, s, m in scored if s > float("-inf")][:confirm_top]
        if log:
            print(f"\n  Native broad-search top {min(confirm_top,len(scored))} "
                  f"(of {len(scored)} passes):", flush=True)
            for p, s, m in scored[:confirm_top]:
                print(f"    {s:>8.3f}  {_combo_str(p)}", flush=True)
        return _confirm(ev, ea, symbol, period, frm, to, objective, finalists,
                        confirm_model, oos_split, n_space, engine, search, log)

    # ---- Layer 1: broad search (fast model), auto-escalating if it yields nothing.
    # Open-price (2) is fastest but won't advance intraday M1 EAs -> escalate 2->1->0.
    def run_layer1(model):
        if search == "grid":
            combos = grid_combos(space)
            if log: print(f"  [grid] {len(combos)} combos on model{model}", flush=True)
            sc = [(c, score(m, objective), m) for c, m in
                  _eval_batch(ev, combos, frm, to, model, max_par, log)]
        elif search == "random":
            combos = random_combos(space, min(n_space, max(gpop * ggens, 20)))
            sc = [(c, score(m, objective), m) for c, m in
                  _eval_batch(ev, combos, frm, to, model, max_par, log)]
        else:  # genetic (default)
            genetic_search(ev, space, objective, frm, to, model, max_par, log, gpop, ggens)
            sc = []
            for (combo_key, f2, t2, mdl), m in ev.cache.items():
                if f2 == frm and t2 == to and mdl == model:
                    sc.append((dict(combo_key), score(m, objective), m))
        sc.sort(key=lambda x: x[1], reverse=True)
        return sc

    seen_m = set()
    screen_chain = [m for m in [int(screen_model), 1, 0]
                    if not (m in seen_m or seen_m.add(m))]
    scored = []
    for mi, model in enumerate(screen_chain):
        scored = run_layer1(model)
        n_trades_any = any(m and m["trades"] > 0 for c, s, m in scored)
        finalists_try = [c for c, s, m in scored if s > float("-inf")]
        if finalists_try:
            screen_model = model; break
        if log and mi + 1 < len(screen_chain):
            why = "0 trades (EA likely intraday/M1)" if not n_trades_any else "all failed objective"
            print(f"  [escalate] model{model} yielded no valid candidates ({why}) "
                  f"-> retry on model{screen_chain[mi+1]}", flush=True)
    finalists = [c for c, s, m in scored if s > float("-inf")][:confirm_top]
    if log:
        print(f"\n  Layer-1 top {len(finalists)} (screen model{screen_model}):", flush=True)
        for c, s, m in scored[:confirm_top]:
            print(f"    {s:>8.3f}  {_combo_str(c)}", flush=True)

    return _confirm(ev, ea, symbol, period, frm, to, objective, finalists,
                    confirm_model, oos_split, n_space, engine, search, log)


def _confirm(ev, ea, symbol, period, frm, to, objective, finalists,
             confirm_model, oos_split, n_space, engine, search, log):
    """Layer 2 — re-run each finalist EVERY-TICK (+ optional IS/OOS), rank, return."""
    if log: print(f"\n  Layer-2 confirm {len(finalists)} finalists every-tick"
                  f"{' + OOS' if oos_split else ''}...", flush=True)
    rows = []
    for c in finalists:
        full = ev.eval(c, frm, to, confirm_model)
        row = {"params": c, "full": full, "full_score": score(full, objective)}
        if oos_split:
            isr = ev.eval(c, frm, oos_split, confirm_model)
            oos = ev.eval(c, oos_split, to, confirm_model)
            row["is"], row["oos"] = isr, oos
            row["is_score"], row["oos_score"] = score(isr, objective), score(oos, objective)
            # anti-curve-fit flag: OOS keeps >=60% of IS objective and stays positive
            row["robust"] = bool(isr is not None and oos is not None and row["oos_score"] > 0
                                 and row["oos_score"] >= 0.6 * max(row["is_score"], 1e-9))
        rows.append(row)
    key = (lambda r: (bool(r.get("robust", False)), r["full_score"])) if oos_split else (lambda r: r["full_score"])
    rows.sort(key=key, reverse=True)
    return {"ea": ea, "symbol": symbol, "period": period, "from": frm, "to": to,
            "objective": objective, "engine": engine, "search": search, "space_size": n_space,
            "runs_executed": ev.n_runs, "results": rows}


def _fmt(m):
    if not m: return "FAIL"
    return (f"ret{m['ret']:+.1f}% cagr{m['cagr']:.1f}% dd{m['dd']:.2f}% mar{m['mar']:.2f} "
            f"pf{m['pf']:.2f} n{m['trades']} win{m['win']:.0f}% worst${m['worst']:.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ea", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--period", default="H1")
    ap.add_argument("--from", dest="frm", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--param", action="append", default=[],
                    help="NAME=lo:hi:step  or  NAME=v1,v2,v3 (repeatable, the search axes)")
    ap.add_argument("--fixed", action="append", default=[], help="NAME=value (repeatable)")
    ap.add_argument("--objective", default="mar if dd<10 and trades>100 else -1e9")
    ap.add_argument("--engine", default="python", choices=["python", "native"],
                    help="native = one in-process MT5 optimization (handles 100k+ combos) "
                         "then every-tick confirm; python = search over the pool")
    ap.add_argument("--optimization", type=int, default=2,
                    help="native mode: 1=complete(slow) 2=genetic (default; prunes huge spaces)")
    ap.add_argument("--criterion", type=int, default=0,
                    help="native opt criterion: 0=Balance 1=PF 2=ExpectedPayoff 3=DD 4=Recovery 5=Sharpe 6=Custom")
    ap.add_argument("--search", default="genetic", choices=["grid", "random", "genetic"])
    ap.add_argument("--screen-model", default="2")
    ap.add_argument("--confirm-model", default="0")
    ap.add_argument("--oos-split", default=None, help="YYYY.MM.DD split for IS/OOS confirm")
    ap.add_argument("--confirm-top", type=int, default=5)
    ap.add_argument("--max-par", type=int, default=3)
    ap.add_argument("--deposit", default="200000")
    ap.add_argument("--gpop", type=int, default=12)
    ap.add_argument("--ggens", type=int, default=5)
    a = ap.parse_args()
    space = {}
    for p in a.param:
        k, v = p.split("=", 1); space[k] = expand_axis(v)
    fixed = {}
    for p in a.fixed:
        k, v = p.split("=", 1); fixed[k] = v
    out = optimize(a.ea, a.symbol, a.period, a.frm, a.to, space, a.objective, fixed,
                   a.search, a.screen_model, a.confirm_model, a.oos_split,
                   a.confirm_top, a.max_par, a.deposit, gpop=a.gpop, ggens=a.ggens,
                   engine=a.engine, optimization=a.optimization, criterion=a.criterion)
    print(f"\n=== RESULT ({out['runs_executed']} runs, space {out['space_size']}) ===")
    for i, r in enumerate(out["results"]):
        tag = "" if not a.oos_split else ("  [ROBUST]" if r.get("robust") else "  [fades OOS]")
        print(f"\n#{i+1}  {_combo_str(r['params'])}{tag}")
        print(f"    FULL : {_fmt(r['full'])}")
        if a.oos_split:
            print(f"    IS   : {_fmt(r.get('is'))}")
            print(f"    OOS  : {_fmt(r.get('oos'))}")
    print("\n(JSON follows)\n" + json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
