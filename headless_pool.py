#!/usr/bin/env python3
"""
MT5-Interlink PARALLEL headless backtest pool.

The fastest + most robust + most accurate way to test/compare many algos:
  * Parallel: one portable MT5 worker per CPU lane, N algos run at once.
  * Accurate: each worker emits MT5's OWN native report (ground truth).
  * Robust:   no GUI, no button polling, no file locks. A run yields a report
              (success) or a captured journal error (clean failure).
  * General:  zero EA edits — any compiled .ex5.

Workers are C:\\MT5_Headless and C:\\MT5_Headless_<n> (history shared via junction).
Live GUI terminal + BridgePro are never touched.

Usage:
    # compare several EAs (same symbol/period), ranked by Sharpe:
    python headless_pool.py --period H2 --from 2020.01.01 --to 2025.12.31 \
        --expert NoWick_XAUUSD_Optimized.ex5 --expert NoWick_XAUUSD_Prop.ex5 ...

    --model 0 every-tick (truth) | 1 1-min OHLC | 2 open prices (fastest screen)
    --sort  sharpe | pf | profit | dd
"""
import argparse
import glob
import json
import os
import queue
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import shutil
import webbrowser
import interlink_config as cfg   # single source of truth for all config
import headless_runner as hr     # reuse parse_report
import equity_curve as eq        # high-res dated balance-curve renderer

# Startup stagger (cross-session) so tester agents don't race for port 3000.
# Tunable via MT5_STAGGER_SEC (lower = faster sweeps, higher = safer launches).
STAGGER_SEC = float(os.environ.get("MT5_STAGGER_SEC", "6.0"))
_LEASE_STALE_SEC = 2400        # reclaim a lease whose owner died or is >40min old
_NOREPORT_RETRIES = int(os.environ.get("MT5_NOREPORT_RETRIES", "2"))
# STALL WATCHDOG (2026-06-15): a launched terminal64 can linger ALIVE-but-IDLE after
# its metatester64 backtest agent crashes/dies (RAM pressure, agent failure). The old
# _await_report saw "terminal alive" and blocked for the WHOLE timeout (up to 1h) doing
# nothing — the recurring "nothing is running but it's hung" failure. Now we detect the
# engine going idle (no agent + no report) for this many seconds and force a retry.
# Generous enough for the agent to spawn at startup; short enough to recover fast.
_STALL_SECS = int(os.environ.get("MT5_STALL_SECS", "180"))

# GPU/RAM governor (caps concurrent every-tick at the ENGINE level). Every-tick
# (model 0) spawns a metatester64 agent that grows to ~2-5GB each as it loads tick
# history; lighter models (1/2) are ~1GB and not gated. This semaphore caps how many
# every-tick runs execute CONCURRENTLY regardless of how wide a caller threads
# run_task — so a ThreadPoolExecutor(6) of model-0 tasks can't swamp the machine
# (historical bypass: analysis scripts called run_task with model='0' + their own
# pool, dodging resolve_model). DEFAULT 2: this box has 23.7GB RAM and 3 concurrent
# every-tick agents + live MT5 + browser/IDE peg memory -> Windows swaps -> slow.
# 2 fits comfortably (no swap) and is net FASTER here than 3-that-swaps. Tune with
# MT5_EVERYTICK_MAX. Per-process: for a single multi-threaded sweep script this IS
# the machine-wide every-tick count.
_EVERYTICK_MAX = max(1, int(os.environ.get("MT5_EVERYTICK_MAX", "2")))
_everytick_sem = threading.BoundedSemaphore(_EVERYTICK_MAX)

# Session/run tag: stamped into every result JSON (field "tag") and its
# filename, so MULTIPLE Claude sessions sharing the pool can each identify
# and pull THEIR OWN results from the shared bt_results folder. Set per
# session via --tag or MT5_RUN_TAG; defaults to the launching pid.
RUN_TAG = re.sub(r"[^A-Za-z0-9_-]", "", os.environ.get("MT5_RUN_TAG", "")) \
          or f"pid{os.getpid()}"


def _proc_alive(pid):
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFO
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def _try_lease(worker):
    """Atomically claim a worker via an exclusive lock dir. Cross-session safe.

    The claim is prepared in a private temp dir (owner file already written
    inside) and activated with os.rename — atomic, fails if a lease exists.
    This closes the race where a freshly-created lease briefly had no owner
    file and a concurrent session judged it dead (pid -1) and stole it — the
    historical double-lease / report cross-wiring bug. Stale takeover is also
    an atomic rename, so only ONE contender can ever win a reclaim."""
    d = os.path.join(worker, ".lease")

    def _claim():
        tmp = os.path.join(worker, f".lease_claim_{os.getpid()}_{threading.get_ident()}")
        try:
            os.mkdir(tmp)
            with open(os.path.join(tmp, "owner"), "w") as f:
                f.write(f"{os.getpid()} {time.time()}")
            os.rename(tmp, d)             # atomic: fails if lease already exists
            return True
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            return False

    if _claim():
        return True

    # Lease exists -> inspect owner.
    pid, ts = -1, 0.0
    try:
        parts = open(os.path.join(d, "owner")).read().split()
        pid, ts = int(parts[0]), float(parts[1])
    except Exception:
        try: ts = os.path.getmtime(d)
        except Exception: ts = 0.0
    fresh = (time.time() - ts) < _LEASE_STALE_SEC
    if fresh and _proc_alive(pid):
        return False                      # genuinely busy
    if fresh and pid == -1 and (time.time() - ts) < 30:
        return False                      # young lease, owner unreadable -> claimant mid-write

    # Stale or dead-owner -> atomic takeover: rename aside; only one session wins.
    grave = os.path.join(worker, f".lease_stale_{os.getpid()}_{int(time.time()*1000)}")
    try:
        os.rename(d, grave)
    except OSError:
        return False                      # another session won the takeover (or owner came back)
    shutil.rmtree(grave, ignore_errors=True)
    return _claim()


def acquire_worker(timeout=1200):
    """Lease any free worker (cross-session). Waits up to timeout for one to free.
    If none is free on the first sweep, runs cleanup_workers() ONCE to reclaim any
    worker held by a dead/zombie run (crashed orchestrator, hung terminal) — so a
    pile-up of leftover terminals from killed runs self-heals instead of blocking."""
    deadline = time.time() + timeout
    cleaned = False
    while time.time() < deadline:
        for w in cfg.discover_workers():
            if _try_lease(w):
                return w
        if not cleaned:
            try: cleanup_workers()
            except Exception: pass
            cleaned = True
            continue
        time.sleep(2)
    return None


def release_worker(worker):
    shutil.rmtree(os.path.join(worker, ".lease"), ignore_errors=True)


def cleanup_workers():
    """Recover the pool: for every worker NOT freshly leased by a live run, kill any
    lingering terminal64/metatester64 running under it and clear its stale lease(s).
    Safe across sessions — never touches a worker whose lease owner is alive & fresh.
    Returns (procs_killed, workers_cleared). Call as a pre-flight or to unstick a hang."""
    killed = cleared = 0
    for w in cfg.discover_workers():
        d = os.path.join(w, ".lease")
        fresh = False
        if os.path.isdir(d):
            try:
                parts = open(os.path.join(d, "owner")).read().split()
                pid, ts = int(parts[0]), float(parts[1])
                fresh = (time.time() - ts) < _LEASE_STALE_SEC and _proc_alive(pid)
            except Exception:
                fresh = False
        if fresh:
            continue                         # owned by a live run -> leave it alone
        term, agent = _worker_procs(w)
        for pid in term + agent:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True, timeout=10)
                killed += 1
            except Exception:
                pass
        if os.path.isdir(d) or term or agent:
            cleared += 1
        shutil.rmtree(d, ignore_errors=True)
        for g in glob.glob(os.path.join(w, ".lease_stale_*")):
            shutil.rmtree(g, ignore_errors=True)
    return killed, cleared


def _heartbeat_lease(worker, stop_event, interval=60):
    """Keep an active lease 'fresh' so another session never reclaims it as stale
    mid-run. Rewrites the owner stamp every `interval`s until the run signals stop."""
    owner = os.path.join(worker, ".lease", "owner")
    while not stop_event.wait(interval):
        try:
            with open(owner, "w") as f:
                f.write(f"{os.getpid()} {time.time()}")
        except Exception:
            pass


def _query_procs(names):
    """[(name_lower, pid, path)] for the given exe names, via wmic with a CIM fallback.
    ONE process query covers all names so callers don't issue several per poll."""
    namesel = " or ".join(f"name='{n}'" for n in names)
    out = ""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", f"({namesel})",
             "get", "ExecutablePath,Name,ProcessId", "/format:csv"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        pass
    rows = []
    if out.strip():
        for line in out.splitlines():
            parts = line.strip().split(",")           # Node,ExecutablePath,Name,ProcessId
            if len(parts) < 4:
                continue
            path, name, pid = parts[-3], parts[-2], parts[-1]
            if pid.isdigit() and path:
                rows.append((name.lower(), pid, path))
        if rows:
            return rows
    # wmic deprecated/removed on newer Win11 -> CIM fallback
    try:
        filt = " or ".join(f"Name='{n}'" for n in names)
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process -Filter \"{filt}\" | "
             "ForEach-Object { $_.Name + '|' + $_.ProcessId + '|' + $_.ExecutablePath }"],
            capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    for line in out.splitlines():
        p = line.strip().split("|")
        if len(p) >= 3 and p[1].isdigit() and p[2]:
            rows.append((p[0].lower(), p[1], p[2]))
    return rows


def _worker_procs(worker, names=("terminal64.exe", "metatester64.exe")):
    """(terminal_pids, agent_pids) running from under `worker` — one query, split by exe."""
    wl = os.path.normcase(os.path.abspath(worker))
    term, agent = [], []
    for name, pid, path in _query_procs(names):
        if wl in os.path.normcase(path):
            (term if name == "terminal64.exe" else agent).append(pid)
    return term, agent


def _worker_terminal_pids(worker):
    """PIDs of terminal64.exe instances whose exe path is under `worker`."""
    return _worker_procs(worker, ("terminal64.exe",))[0]


def _kill_in_worker(worker):
    """Terminate any terminal64 already running inside THIS (leased) worker dir —
    a hung/stray leftover. Safe: we hold the lease, so no other session owns it.
    Only ever targets instances whose exe path is under `worker` (never the live
    terminal or other workers)."""
    killed = False
    term, agent = _worker_procs(worker)
    for pid in term + agent:              # kill the terminal AND any (hung) metatester agent
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True, timeout=10)
            killed = True
        except Exception:
            pass
    if killed:
        time.sleep(1.5)


def _report_complete(report):
    """True only when the report htm is FULLY written (has its closing markup).
    Guards against killing/parsing a half-saved file if we catch it mid-write."""
    try:
        with open(report, "r", encoding="utf-16", errors="ignore") as f:
            txt = f.read()
    except Exception:
        return False
    return "</html>" in txt.lower() or "Total Trades" in txt


def _await_report(worker, report, timeout, handoff_grace=25):
    """Wait for the backtest's report, then FORCE-KILL the worker terminal — we
    never wait for the terminal to close itself.

    ROOT-CAUSE FIX (2026-06-11): MT5's 'Saving report: headless_report.htm' modal
    occasionally STICKS instead of auto-closing, which blocks ShutdownTerminal and
    keeps the terminal alive indefinitely. The old code did proc.wait(timeout=...),
    so a stuck modal blocked the whole run for the entire timeout (up to ~50 min =
    the '30-minute hang'). But the report is already complete on disk by then — so
    we detect the finished report and kill the terminal, making the modal a non-issue.

    terminal64 may RE-SPAWN itself on launch (the Popen'd proc exits in ~1s while the
    respawned instance runs the test), so completion is tracked by the report file +
    worker-terminal presence, NOT by the launcher process.
    Returns 'report' | 'no_report' | 'timeout'.

    STALL WATCHDOG (2026-06-15): tracks the backtest ENGINE (metatester64 agent), not
    just the terminal. If the terminal lingers alive but the agent is gone and no report
    has appeared for _STALL_SECS, the engine has died/hung — we tear down and return
    'no_report' (which triggers an automatic retry) instead of blocking the full timeout."""
    deadline = time.time() + timeout
    seen_term, quiet = False, 0
    last_busy = time.time()                     # last time the engine showed life
    poll = 2
    while time.time() < deadline:
        if os.path.exists(report) and _report_complete(report):
            _kill_in_worker(worker)            # modal-proof teardown; report is on disk
            return "report"
        term, agent = _worker_procs(worker)
        report_started = os.path.exists(report)
        if agent or report_started:            # agent running OR report being written = alive
            last_busy = time.time()
        if term:
            seen_term, quiet = True, 0
            # terminal alive but engine idle (no agent, no report) too long -> stalled
            if not agent and not report_started and (time.time() - last_busy) > _STALL_SECS:
                _kill_in_worker(worker)
                return "no_report"
        else:
            quiet += poll
            # terminal ran then vanished without a complete report -> genuine failure
            if seen_term and quiet >= 8 and not report_started:
                return "no_report"
            # launcher never spawned a worker terminal within the handoff grace
            if not seen_term and quiet >= handoff_grace and not report_started:
                return "no_report"
        time.sleep(poll)
    _kill_in_worker(worker)                     # timed out -> force teardown
    return "report" if (os.path.exists(report) and _report_complete(report)) else "timeout"


def _launch_gate():
    """Cross-session lock dir (in shared bt_results) held briefly during launch
    so only one MT5 instance starts at a time -> no port-3000 race."""
    d = os.path.join(cfg.BT_RESULTS, ".launch_gate")
    os.makedirs(cfg.BT_RESULTS, exist_ok=True)
    for _ in range(120):                  # up to ~60s
        try:
            os.mkdir(d); return d
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(d) > 30:
                    shutil.rmtree(d, ignore_errors=True); continue
            except Exception:
                pass
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    return None


def _release_gate(d):
    if d:
        shutil.rmtree(d, ignore_errors=True)


import urllib.request as _ureq

def _file_url(p):
    # percent-encodes spaces etc. so paths like "Quantitative finance" don't break
    return "file:" + _ureq.pathname2url(os.path.abspath(p))


def _open_path(p):
    """Open a report artifact for viewing. Images (.png) are opened in the
    browser via Edge — the OS image-app association can launch silently/hidden,
    whereas the browser reliably surfaces a window. HTML/PDF use the default app."""
    ext = os.path.splitext(p)[1].lower()
    if ext == ".png":
        edge = cfg.find_edge()
        if edge:
            try:
                subprocess.Popen([edge, _file_url(p)])
                return
            except Exception:
                pass
        webbrowser.open(_file_url(p))
        return
    try:
        os.startfile(p)                 # HTML / PDF → default app
    except Exception:
        webbrowser.open(_file_url(p))


def export_report(worker, ea_name, symbol, period, stamp, make_pdf, open_it):
    """Copy MT5's native HTML report (+ its chart PNGs) out of the worker into the
    project reports folder. Optionally render a PDF via headless Edge and/or open
    it. Must run BEFORE the worker is recycled (the htm gets overwritten)."""
    htm = os.path.join(worker, "headless_report.htm")
    if not os.path.exists(htm):
        return None
    dest = os.path.join(cfg.EXPORT_ROOT, f"{ea_name}_{symbol}_{period}_{stamp}")
    os.makedirs(dest, exist_ok=True)
    # copy htm + sibling PNGs keeping names so the report's relative <img> links resolve
    for f in glob.glob(os.path.join(worker, "headless_report*")):
        try:
            shutil.copy2(f, os.path.join(dest, os.path.basename(f)))
        except Exception:
            pass
    out_htm = os.path.join(dest, "headless_report.htm")
    out_pdf = os.path.join(dest, f"{ea_name}_{symbol}_{period}.pdf")
    pdf_ok = False

    # high-res dated balance curve (closer to the Strategy Tester Graph look)
    equity_png = None
    try:
        equity_png = eq.render_from_report(
            out_htm, os.path.join(dest, "equity_curve.png"),
            title=f"{ea_name}   {symbol} {period}", symbol=symbol, period=period)
    except Exception:
        equity_png = None
    if make_pdf:
        edge = cfg.find_edge()
        if edge:
            try:
                subprocess.run(
                    [edge, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                     f"--print-to-pdf={out_pdf}", _file_url(out_htm)],
                    timeout=90, capture_output=True)
                pdf_ok = os.path.exists(out_pdf)
            except Exception:
                pdf_ok = False
    if open_it:
        _open_path(equity_png or (out_pdf if pdf_ok else out_htm))
    return {"dir": dest, "html": out_htm, "pdf": out_pdf if pdf_ok else None,
            "equity_png": equity_png}


def resolve_model(model, parallel):
    """Policy: single / <=3 concurrent -> every-tick (truth, ~5GB each);
    >3 concurrent -> open-prices-only (light, keeps RAM safe). Explicit model wins."""
    if model != "auto":
        return model
    return "0" if parallel <= 3 else "2"


def sync_experts(workers, experts):
    """Best-effort pre-warm: copy each requested EA from the live terminal into
    every worker. Failures are fine — _ensure_ea() re-checks inside the LEASED
    worker right before each launch, which is the authoritative copy."""
    missing = []
    for ea in set(experts):
        src = cfg.find_live_ea(ea)
        if not src:
            missing.append(ea)
            continue
        for w in workers:
            dst_dir = os.path.join(w, "MQL5", "Experts")
            os.makedirs(dst_dir, exist_ok=True)
            try:
                shutil.copy2(src, os.path.join(dst_dir, os.path.basename(src)))
            except Exception:
                pass
    return missing


def _ensure_ea(worker, expert):
    """Guarantee the EA exists (and is current) in THIS leased worker before
    launch. Returns None on success, else a human-readable error. Closes the
    silent-copy-failure hole that produced 'tester EX5 not found' flakes."""
    src = cfg.find_live_ea(expert)
    dst = os.path.join(worker, "MQL5", "Experts", os.path.basename(expert))
    if not src:
        return None if os.path.exists(dst) else \
            f"EA not found in live terminal Experts tree: {expert}"
    try:
        if (not os.path.exists(dst)
                or os.path.getmtime(src) > os.path.getmtime(dst)
                or os.path.getsize(src) != os.path.getsize(dst)):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    except Exception as ex:
        if not os.path.exists(dst):
            return f"failed to copy EA into worker: {ex}"
    return None


def _journal_error(worker, since_hms=None, since_epoch=None):
    """Scan a worker's logs for an OnInit / EA error after a failed/empty run.

    `since_hms` = "HH:MM:SS" of run start; only lines at/after it are kept.
    `since_epoch` = run-start time.time(); only log FILES modified after it are
    scanned — otherwise yesterday's daily log passes the time-of-day filter and
    we report stale errors from runs that happened days ago."""
    hits = []
    for pat in (os.path.join(worker, "logs", "*.log"),
                os.path.join(worker, "Tester", "**", "*.log")):
        for lf in glob.glob(pat, recursive=True):
            if since_epoch:
                try:
                    if os.path.getmtime(lf) < since_epoch - 60:
                        continue          # untouched since before this run -> stale
                except Exception:
                    pass
            try:
                with open(lf, "r", encoding="utf-16", errors="ignore") as f:
                    txt = f.read()
            except Exception:
                continue
            for line in txt.splitlines():
                if since_hms:
                    m = re.search(r"\b(\d{2}:\d{2}:\d{2})\b", line)
                    if m and m.group(1) < since_hms:
                        continue
                low = line.lower()
                if ("not found" in low or "returns non-zero" in low
                        or "didn't start" in low or "error" in low
                        or "not enough" in low or "no history" in low):
                    hits.append(line.strip()[-160:])
    # de-dup, keep last few
    seen, out = set(), []
    for h in reversed(hits):
        if h not in seen:
            seen.add(h); out.append(h)
        if len(out) >= 4:
            break
    return out


def write_worker_ini(worker, expert, symbol, period, frm, to, model, deposit,
                     leverage, inputs=None):
    ini = os.path.join(worker, "headless_test.ini")
    with open(ini, "w", encoding="ascii") as f:
        f.write(
            f"[Common]\nLogin={cfg.LOGIN}\nServer={cfg.SERVER}\n\n"
            "[Tester]\n"
            f"Expert={expert}\nSymbol={symbol}\nPeriod={period}\n"
            f"Model={model}\nOptimization=0\n"
            f"FromDate={frm}\nToDate={to}\n"
            f"Deposit={deposit}\nCurrency=USD\nLeverage={leverage}\n"
            "Report=headless_report\nReplaceReport=1\nShutdownTerminal=1\n"
        )
        # Override specific EA inputs (fixed values) for parameter testing/optimization
        if inputs:
            f.write("\n[TesterInputs]\n")
            for k, v in inputs.items():
                f.write(f"{k}={v}\n")
    return ini


def run_task(task, timeout, worker=None):
    """Run one backtest. Leases a free worker (cross-session safe) unless one is
    passed in. Releases the lease when done. Every-tick (model 0) runs are throttled
    through `_everytick_sem` so no more than MT5_EVERYTICK_MAX run at once (GPU-safe);
    the slot is taken BEFORE leasing a worker so throttled tasks don't tie up workers."""
    expert = task["expert"]
    heavy = str(task.get("model")) == "0"        # every-tick = GPU/RAM heavy -> gate it
    if heavy:
        _everytick_sem.acquire()
    try:
        return _run_task_inner(task, timeout, worker)
    finally:
        if heavy:
            _everytick_sem.release()


def _run_task_inner(task, timeout, worker=None):
    expert = task["expert"]
    owned = False
    if worker is None:
        worker = acquire_worker(timeout)
        owned = True
        if worker is None:
            return {"expert": expert, "ok": False,
                    "error": "no free worker (all leased by other runs)"}
    stop_hb = None
    if owned:
        stop_hb = threading.Event()
        threading.Thread(target=_heartbeat_lease, args=(worker, stop_hb),
                         daemon=True).start()
    try:
        # "no report" / agent-port bind errors are almost always a launch race
        # or transient agent failure — retry on the same leased worker before
        # reporting a clean failure.
        def _retryable(r):
            if r.get("ok"):
                return False
            if "no report" in str(r.get("error", "")):
                return True
            return any("bind error" in j for j in r.get("journal", []) or [])
        for attempt in range(_NOREPORT_RETRIES + 1):
            res = _run_on(worker, task, timeout)
            if not _retryable(res):
                if attempt:
                    res["retried"] = attempt
                return res
            # 2026-09-09: MT5 builds >= 6140 open a built-in MCP server on a FIXED local port
            # (127.0.0.1:22346) in every terminal; simultaneous worker launches collide
            # ("bind error ... 10048") and the loser sometimes never tests. A fixed 3 s
            # retry re-collides, so back off with jitter before relaunching.
            import random
            time.sleep(3 + random.uniform(6, 18) * (attempt + 1))
        res["retried"] = _NOREPORT_RETRIES
        return res
    finally:
        if stop_hb:
            stop_hb.set()
        if owned:
            release_worker(worker)


def _run_on(worker, task, timeout):
    expert = task["expert"]
    _kill_in_worker(worker)               # clear any hung leftover in this leased worker
    ea_err = _ensure_ea(worker, expert)   # authoritative EA sync into the LEASED worker
    if ea_err:
        return {"expert": expert, "worker": worker, "ok": False, "error": ea_err}
    # Purge the tester's per-EA parameter cache: MT5 stores last-used inputs in
    # Profiles\Tester\<EA>.set and silently OVERRIDES freshly compiled defaults
    # for the same EA name (bit Concretum 2026-06-02 and RANGE BREAK OUT RV
    # 2026-06-11). Explicit [TesterInputs] still apply afterwards as intended.
    ea_base = os.path.splitext(os.path.basename(expert))[0]
    for cache in glob.glob(os.path.join(worker, "MQL5", "Profiles", "Tester",
                                        ea_base + "*.set")):
        try:
            os.remove(cache)
        except Exception:
            pass
    report = os.path.join(worker, "headless_report.htm")
    # 2026-09-09 INTEGRITY FIX: a locked stale report used to survive this purge silently and
    # _await_report then accepted it as the NEW task's result (cross-wired results seen in the
    # revalidation: two different tasks returned one report). Now: remove, else move aside,
    # else refuse to run on this worker.
    for ext in (".htm", ".html"):
        p = os.path.join(worker, "headless_report" + ext)
        if os.path.exists(p):
            gone = False
            for _i in range(5):
                try:
                    os.remove(p); gone = True; break
                except Exception:
                    try:
                        os.replace(p, os.path.join(worker, f"headless_report_stale_{int(time.time())}{ext}")); gone = True; break
                    except Exception:
                        time.sleep(2)
            if not gone:
                return {"expert": expert, "worker": worker, "ok": False,
                        "error": "stale report locked — refused to run (integrity guard)"}

    ini = write_worker_ini(worker, expert, task["symbol"], task["period"],
                           task["frm"], task["to"], task["model"],
                           task["deposit"], task["leverage"], task.get("inputs"))
    # Cross-session launch gate: only one MT5 instance starts at a time, with a
    # short stagger, so their tester agents don't race for port 3000.
    gate = _launch_gate()
    try:
        since = datetime.now().strftime("%H:%M:%S")
        since_ep = time.time()
        proc = subprocess.Popen([os.path.join(worker, "terminal64.exe"),
                                 "/portable", f"/config:{ini}"])
        time.sleep(STAGGER_SEC)           # let it bind its agent port before next launch
    finally:
        _release_gate(gate)
    t0 = time.time()
    # Wait for the COMPLETE report, then force-kill the terminal — modal-proof.
    # (Never block on proc.wait(): MT5's 'Saving report' modal can stick and keep
    #  the terminal alive for the whole timeout = the historical 30-min hang.)
    status = _await_report(worker, report, timeout)
    try:
        proc.kill()                       # reap the launcher handle if still around
    except Exception:
        pass
    elapsed = round(time.time() - t0, 1)
    if status == "timeout":
        return {"expert": expert, "worker": worker, "ok": False,
                "error": f"timeout {timeout}s",
                "journal": _journal_error(worker, since, since_ep),
                "elapsed_sec": elapsed}

    if not os.path.exists(report) or os.path.getmtime(report) < since_ep - 5:
        # missing, OR older than this launch = stale file from a previous task -> never parse it
        return {"expert": expert, "worker": worker, "ok": False,
                "error": "no report (EA missing or failed OnInit)" if not os.path.exists(report)
                         else "stale report (older than launch) — rejected by integrity guard",
                "journal": _journal_error(worker, since, since_ep),
                "elapsed_sec": elapsed}

    ea_name = expert.replace(".ex5", "")
    res = hr.parse_report(report, ea_name, task["symbol"], task["period"],
                          task["frm"], task["to"], task["deposit"])
    res["elapsed_sec"] = elapsed
    res["worker"] = worker
    res["ok"] = res["trades"] > 0
    if res["trades"] == 0:
        res["error"] = "0 trades — check params/timeframe guard"
        res["journal"] = _journal_error(worker, since, since_ep)

    # persist to shared bt_results, tagged with the owning session
    res["tag"] = RUN_TAG
    os.makedirs(cfg.BT_RESULTS, exist_ok=True)
    stamp = datetime.now().strftime("%Y.%m.%d_%H-%M-%S-%f")
    out = os.path.join(cfg.BT_RESULTS,
                       f"{ea_name}_{task['symbol']}_{task['period']}_{stamp}_{RUN_TAG}_pool.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)
    res["_file"] = out

    # export the native HTML/PDF report (before the worker is recycled).
    # Opening is handled once in run_pool (winner only) — not per task.
    if res["ok"] and task.get("export"):
        try:
            res["report"] = export_report(
                worker, ea_name, task["symbol"], task["period"], stamp,
                task.get("pdf", False), open_it=False)
        except Exception as ex:
            res["report"] = {"error": str(ex)}
    return res


def run_pool(experts, symbol, period, frm, to, model, deposit, leverage,
             timeout, max_workers=None, sort_by="pf",
             export=False, pdf=False, open_report=False):
    workers = cfg.discover_workers()
    if not workers:
        return {"error": "no headless workers found"}
    pool = min(max_workers or len(workers), len(workers))

    # Adaptive model: every-tick when <=3 run concurrently, else open-prices.
    effective_parallel = min(pool, len(experts))
    model = resolve_model(model, effective_parallel)

    # Make every requested EA available in EVERY worker (a task may lease any).
    missing = sync_experts(workers, experts)

    tasks = [{"expert": e, "symbol": symbol, "period": period, "frm": frm,
              "to": to, "model": model, "deposit": deposit, "leverage": leverage,
              "export": export, "pdf": pdf, "open": open_report}
             for e in experts]

    results = []
    lock = threading.Lock()

    def worker_fn(task):
        # run_task self-leases a free worker (cross-session safe) and releases it.
        r = run_task(task, timeout + 1200)
        with lock:
            results.append(r)
        return r

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=pool) as ex:  # cap concurrency for THIS session
        list(ex.map(worker_fn, tasks))
    wall = round(time.time() - t0, 1)

    sort_key = {
        "pf":     lambda r: r.get("profit_factor", 0),
        "mar":    lambda r: r.get("mar", 0),
        "profit": lambda r: r.get("net_profit", 0),
        "dd":     lambda r: -(r.get("max_dd_pct", 999)),
        "sharpe": lambda r: r.get("sharpe_ratio", 0),
    }.get(sort_by, lambda r: r.get("profit_factor", 0))
    ranked = sorted([r for r in results if r.get("ok")], key=sort_key, reverse=True)
    failed = [r for r in results if not r.get("ok")]

    # Auto-open the top-ranked equity curve (single run → that one; batch → winner).
    if open_report and ranked:
        rep = ranked[0].get("report") or {}
        target = rep.get("equity_png") or rep.get("pdf") or rep.get("html")
        if target and os.path.exists(target):
            _open_path(target)

    table = [{
        "rank": i + 1, "ea": r["ea"], "net_profit": r["net_profit"],
        "return_pct": r.get("return_pct", 0), "cagr_pct": r.get("cagr_pct", 0),
        "profit_factor": r["profit_factor"], "mar": r.get("mar", 0),
        "sharpe_ratio": r["sharpe_ratio"],
        "max_dd_pct": r["max_dd_pct"], "trades": r["trades"],
        "win_rate_pct": r["win_rate_pct"], "best_trade": r["largest_win"],
        "worst_trade": r["largest_loss"], "elapsed_sec": r["elapsed_sec"],
        "monthly_pct": r.get("monthly_pct", {}),
    } for i, r in enumerate(ranked)]

    return {
        "pool_size": pool, "workers_available": len(workers),
        "model": model, "wall_seconds": wall, "sort_by": sort_by,
        "params": {"symbol": symbol, "period": period, "from": frm, "to": to},
        "missing_eas": missing,
        "ranking": table, "failed": failed, "raw": results,
    }


def pool_status():
    """Show every worker + lease state (which session holds it, alive or stale)."""
    rows = []
    for w in cfg.discover_workers():
        lease = os.path.join(w, ".lease")
        if not os.path.isdir(lease):
            rows.append({"worker": w, "state": "free"})
            continue
        pid, ts = -1, 0.0
        try:
            parts = open(os.path.join(lease, "owner")).read().split()
            pid, ts = int(parts[0]), float(parts[1])
        except Exception:
            try: ts = os.path.getmtime(lease)
            except Exception: pass
        age = round(time.time() - ts)
        alive = _proc_alive(pid)
        rows.append({"worker": w,
                     "state": "leased" if alive and age < _LEASE_STALE_SEC else "stale",
                     "owner_pid": pid, "owner_alive": alive, "lease_age_sec": age})
    return {"workers": rows,
            "free": sum(1 for r in rows if r["state"] == "free"),
            "stagger_sec": STAGGER_SEC}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true",
                    help="show worker/lease state and exit")
    ap.add_argument("--expert", action="append")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--period", default="H2")
    ap.add_argument("--from", dest="frm", default="2020.01.01")
    ap.add_argument("--to", default="2025.12.31")
    ap.add_argument("--model", default="auto",
                    help="auto (default): every-tick if <=3 concurrent, else open-prices | "
                         "0=every tick | 1=1min OHLC | 2=open prices")
    ap.add_argument("--deposit", default="200000")
    ap.add_argument("--leverage", default="100")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--sort", default="pf",
                    help="pf (default) | mar | profit | dd | sharpe "
                         "(house rule: PF/MAR over MT5's inflated intraday Sharpe)")
    ap.add_argument("--max-workers", type=int, default=None)
    ap.add_argument("--no-export", action="store_false", dest="export", default=True,
                    help="skip exporting the HTML report (export is ON by default)")
    ap.add_argument("--pdf", action="store_true", help="also render a PDF (headless Edge)")
    ap.add_argument("--no-open", action="store_false", dest="open", default=True,
                    help="don't auto-open the report (auto-open is ON; opens winner only)")
    ap.add_argument("--tag", default=None,
                    help="session/run tag stamped into result JSONs + filenames "
                         "(default: MT5_RUN_TAG env or pid<N>) — lets multiple "
                         "Claude sessions pull their own results from bt_results")
    a = ap.parse_args()
    if a.tag:
        global RUN_TAG
        RUN_TAG = re.sub(r"[^A-Za-z0-9_-]", "", a.tag) or RUN_TAG
    if a.status:
        print(json.dumps(pool_status(), indent=2))
        return
    if not a.expert:
        ap.error("--expert is required (or use --status)")
    out = run_pool(a.expert, a.symbol, a.period, a.frm, a.to, a.model,
                   a.deposit, a.leverage, a.timeout, a.max_workers, a.sort,
                   a.export, a.pdf, a.open)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
