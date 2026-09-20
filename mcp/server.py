"""
MT5-Interlink MCP Server

Connects Claude to MetaTrader 5 via two channels:
  1. MetaTrader5 Python package — read-only account/market data, no EA needed
  2. MT5Bridge.dll HTTP bridge (port 8892) — Strategy Tester automation + live pushed state

New in MT5-Interlink vs the original MT5 pipeline:
  - mt5_state          → atomic snapshot (1 HTTP call vs 4)
  - mt5_bt_results     → read TesterStats JSON files, return comparison table
  - mt5_compare_run    → run N strategies back-to-back, collect + compare results
  - mt5_interconnect_health → health check for the new bridge

Config (env vars set in .mcp.json):
    MT5_TERMINAL_ID   — hex folder in %APPDATA%\\MetaQuotes\\Terminal\\
    MT5_BRIDGE_URL    — default http://localhost:8892
    MT5_TERMINAL_EXE  — path to terminal64.exe
    MT5_EDITOR_EXE    — path to MetaEditor64.exe
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from fastmcp import FastMCP
import MetaTrader5 as mt5

mcp = FastMCP("mt5-interconnector")

TF_MAP = {
    "M1": mt5.TIMEFRAME_M1,   "M2": mt5.TIMEFRAME_M2,   "M3": mt5.TIMEFRAME_M3,
    "M4": mt5.TIMEFRAME_M4,   "M5": mt5.TIMEFRAME_M5,   "M6": mt5.TIMEFRAME_M6,
    "M10": mt5.TIMEFRAME_M10, "M12": mt5.TIMEFRAME_M12, "M15": mt5.TIMEFRAME_M15,
    "M20": mt5.TIMEFRAME_M20, "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,   "H2": mt5.TIMEFRAME_H2,   "H3": mt5.TIMEFRAME_H3,
    "H4": mt5.TIMEFRAME_H4,   "H6": mt5.TIMEFRAME_H6,   "H8": mt5.TIMEFRAME_H8,
    "H12": mt5.TIMEFRAME_H12, "D1": mt5.TIMEFRAME_D1,   "W1": mt5.TIMEFRAME_W1,
    "MN1": mt5.TIMEFRAME_MN1,
}


# ── Config ────────────────────────────────────────────────────────────────

MT5_TERMINAL_EXE = os.environ.get("MT5_TERMINAL_EXE", r"C:\Program Files\MetaTrader 5\terminal64.exe")
MT5_EDITOR_EXE   = os.environ.get("MT5_EDITOR_EXE",   r"C:\Program Files\MetaTrader 5\MetaEditor64.exe")
MT5_TERMINAL_ID  = os.environ.get("MT5_TERMINAL_ID",  "YOUR_TERMINAL_ID_HERE")
MT5_DATA_FOLDER  = os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Terminal", MT5_TERMINAL_ID)
EXPERTS_DIR      = os.path.join(MT5_DATA_FOLDER, "MQL5", "Experts")
BT_RESULTS_DIR   = os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Terminal", "Common", "Files", "bt_results")

BRIDGE_URL = os.environ.get("MT5_BRIDGE_URL", "http://localhost:8892")


# ── Helpers ───────────────────────────────────────────────────────────────

def _ensure_init() -> Optional[str]:
    if not mt5.initialize():
        return f"mt5.initialize() failed: {mt5.last_error()}"
    return None

def _ok(data: Any) -> str:
    return json.dumps(data, default=str, indent=2)

def _err(msg: str, detail: Any = None) -> str:
    return json.dumps({"error": msg, "detail": str(detail) if detail else None})


import urllib.request as _ureq
import urllib.error as _uerr

def _bridge_get(path: str, timeout: int = 5) -> Dict[str, Any]:
    try:
        with _ureq.urlopen(f"{BRIDGE_URL}{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e), "detail": type(e).__name__}

def _bridge_post(path: str, body: Optional[Dict[str, Any]] = None, timeout: int = 120) -> Dict[str, Any]:
    data = json.dumps(body or {}).encode("utf-8")
    req = _ureq.Request(f"{BRIDGE_URL}{path}", data=data,
                        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _ureq.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except _uerr.HTTPError as e:
        return {"error": f"HTTP {e.code}", "detail": e.read().decode("utf-8", errors="ignore")}
    except Exception as e:
        return {"error": str(e), "detail": type(e).__name__}


# ── Bridge / health ───────────────────────────────────────────────────────

@mcp.tool()
def mt5_interconnect_health() -> str:
    """Check if MT5-Interlink bridge (BridgePro EA + MT5Bridge.dll) is running on port 8892.

    Returns version, features, and port. If error → bridge is not running; attach BridgePro to a chart.
    """
    return _ok(_bridge_get("/version"))


# ── Account / state ───────────────────────────────────────────────────────

@mcp.tool()
def mt5_account() -> str:
    """Current account: balance, equity, margin, free margin, profit, leverage, login, company."""
    if (e := _ensure_init()): return _err(e)
    info = mt5.account_info()
    if info is None: return _err("account_info returned None", mt5.last_error())
    return _ok(info._asdict())


@mcp.tool()
def mt5_terminal() -> str:
    """Terminal info: broker, build, community, connection state, path."""
    if (e := _ensure_init()): return _err(e)
    info = mt5.terminal_info()
    if info is None: return _err("terminal_info returned None", mt5.last_error())
    return _ok(info._asdict())


@mcp.tool()
def mt5_state() -> str:
    """Atomic full state: account + positions + orders + terminal in ONE bridge call.

    Faster than calling mt5_account/positions/orders separately — single HTTP request.
    Requires BridgePro EA running (use mt5_interconnect_health to check).
    """
    return _ok(_bridge_get("/state"))


@mcp.tool()
def mt5_positions(symbol: Optional[str] = None) -> str:
    """Open positions. Optional symbol filter (e.g. 'XAUUSD')."""
    if (e := _ensure_init()): return _err(e)
    pos = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    if pos is None: return _err("positions_get returned None", mt5.last_error())
    return _ok([p._asdict() for p in pos])


@mcp.tool()
def mt5_orders(symbol: Optional[str] = None) -> str:
    """Pending orders. Optional symbol filter."""
    if (e := _ensure_init()): return _err(e)
    orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
    if orders is None: return _err("orders_get returned None", mt5.last_error())
    return _ok([o._asdict() for o in orders])


@mcp.tool()
def mt5_symbols(pattern: str = "") -> str:
    """List available symbols. Optional substring filter."""
    if (e := _ensure_init()): return _err(e)
    syms = mt5.symbols_get(pattern) if pattern else mt5.symbols_get()
    if syms is None: return _err("symbols_get returned None", mt5.last_error())
    return _ok([{"name": s.name, "description": s.description, "path": s.path,
                 "visible": s.visible, "volume_min": s.volume_min} for s in syms])


# ── Market data ───────────────────────────────────────────────────────────

@mcp.tool()
def mt5_tick(symbol: str) -> str:
    """Latest bid/ask/last/volume/time for a symbol."""
    if (e := _ensure_init()): return _err(e)
    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None: return _err(f"symbol_info_tick({symbol}) returned None", mt5.last_error())
    return _ok({"symbol": symbol,
                "time": datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat(),
                "bid": tick.bid, "ask": tick.ask, "last": tick.last, "volume": tick.volume})


@mcp.tool()
def mt5_symbol(symbol: str) -> str:
    """Full symbol spec: contract size, margin, tick value, trading hours, etc."""
    if (e := _ensure_init()): return _err(e)
    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)
    if info is None: return _err(f"symbol_info({symbol}) returned None", mt5.last_error())
    return _ok(info._asdict())


@mcp.tool()
def mt5_rates(symbol: str, timeframe: str = "H1", count: int = 100,
              start_date: Optional[str] = None, end_date: Optional[str] = None) -> str:
    """Historical OHLCV bars.

    Args:
        symbol: e.g. "XAUUSD"
        timeframe: M1 M5 M15 M30 H1 H4 D1 W1 MN1
        count: bars to return when no date range
        start_date / end_date: ISO "YYYY-MM-DD" (returns range when both provided)
    """
    if (e := _ensure_init()): return _err(e)
    tf = TF_MAP.get(timeframe.upper())
    if tf is None: return _err(f"bad timeframe: {timeframe}")
    mt5.symbol_select(symbol, True)
    if start_date and end_date:
        sd = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
        ed = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
        rates = mt5.copy_rates_range(symbol, tf, sd, ed)
    else:
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None: return _err("copy_rates returned None", mt5.last_error())
    return _ok({
        "symbol": symbol, "timeframe": timeframe, "count": len(rates),
        "bars": [{"time": datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).isoformat(),
                  "open": float(r["open"]), "high": float(r["high"]),
                  "low": float(r["low"]),  "close": float(r["close"]),
                  "volume": int(r["tick_volume"])} for r in rates[-500:]]
    })


# ── History ───────────────────────────────────────────────────────────────

@mcp.tool()
def mt5_deals(days: int = 30, symbol: Optional[str] = None) -> str:
    """Closed deals over last N days. Optional symbol filter."""
    if (e := _ensure_init()): return _err(e)
    frm = datetime.now(timezone.utc) - timedelta(days=days)
    to  = datetime.now(timezone.utc) + timedelta(days=1)
    deals = mt5.history_deals_get(frm, to, group=f"*{symbol}*") if symbol else mt5.history_deals_get(frm, to)
    if deals is None: return _err("history_deals_get returned None", mt5.last_error())
    return _ok([d._asdict() for d in deals])


@mcp.tool()
def mt5_history(days: int = 30, symbol: Optional[str] = None) -> str:
    """Historical orders over last N days. Optional symbol filter."""
    if (e := _ensure_init()): return _err(e)
    frm = datetime.now(timezone.utc) - timedelta(days=days)
    to  = datetime.now(timezone.utc) + timedelta(days=1)
    orders = mt5.history_orders_get(frm, to, group=f"*{symbol}*") if symbol else mt5.history_orders_get(frm, to)
    if orders is None: return _err("history_orders_get returned None", mt5.last_error())
    return _ok([o._asdict() for o in orders])


# ── Analytics ─────────────────────────────────────────────────────────────

@mcp.tool()
def mt5_account_snapshot() -> str:
    """Full account snapshot: account + terminal + open positions + pending orders + recent deals."""
    if (e := _ensure_init()): return _err(e)
    acc  = mt5.account_info()
    term = mt5.terminal_info()
    pos  = mt5.positions_get()
    pend = mt5.orders_get()
    frm  = datetime.now(timezone.utc) - timedelta(days=7)
    to   = datetime.now(timezone.utc) + timedelta(days=1)
    deals = mt5.history_deals_get(frm, to)
    return _ok({
        "account": acc._asdict() if acc else None,
        "terminal": term._asdict() if term else None,
        "open_positions": [p._asdict() for p in (pos or [])],
        "pending_orders": [o._asdict() for o in (pend or [])],
        "open_position_count": len(pos or []),
        "pending_order_count": len(pend or []),
        "total_open_profit": sum(p.profit for p in (pos or [])),
        "deals_last_7d": len(deals or []),
        "ts": datetime.now(timezone.utc).isoformat(),
    })


@mcp.tool()
def mt5_portfolio_exposure() -> str:
    """Current net exposure by symbol: net volume, gross long/short, unrealized P&L."""
    if (e := _ensure_init()): return _err(e)
    pos = mt5.positions_get() or []
    by_sym: Dict[str, Any] = {}
    for p in pos:
        s = p.symbol; side = 1 if p.type == mt5.POSITION_TYPE_BUY else -1
        if s not in by_sym:
            by_sym[s] = {"net_volume": 0.0, "gross_long": 0.0, "gross_short": 0.0,
                         "unrealized_pnl": 0.0, "tickets": []}
        by_sym[s]["net_volume"]     += side * p.volume
        by_sym[s]["gross_long"]     += p.volume if side > 0 else 0
        by_sym[s]["gross_short"]    += p.volume if side < 0 else 0
        by_sym[s]["unrealized_pnl"] += p.profit
        by_sym[s]["tickets"].append(p.ticket)
    return _ok(by_sym)


# ── Strategy code management ──────────────────────────────────────────────

import subprocess as _sp

@mcp.tool()
def mt5_compile(file_path: str) -> str:
    """Compile an MQL5 file via MetaEditor64.exe /compile.

    Args:
        file_path: absolute path OR filename inside MQL5\\Experts\\ (e.g. "MyStrategy.mq5")
    """
    if not os.path.isabs(file_path):
        file_path = os.path.join(EXPERTS_DIR, file_path)
    if not os.path.exists(file_path):
        return _err(f"File not found: {file_path}")
    log_path = file_path.rsplit(".", 1)[0] + ".log"
    try:
        _sp.run([MT5_EDITOR_EXE, f"/compile:{file_path}", f"/log:{log_path}"],
                capture_output=True, timeout=120)
    except Exception as ex:
        return _err(f"metaeditor invocation failed: {ex}")
    log_content = ""
    try:
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-16-le", errors="ignore") as f:
                log_content = f.read()
    except Exception:
        pass
    ex5 = file_path.replace(".mq5", ".ex5")
    errors   = [l for l in log_content.splitlines() if ": error"   in l.lower()]
    warnings = [l for l in log_content.splitlines() if ": warning" in l.lower()]
    return _ok({
        "success":       os.path.exists(ex5) and len(errors) == 0,
        "ex5_path":      ex5 if os.path.exists(ex5) else None,
        "errors":        errors[:10],
        "warnings":      warnings[:10],
        "error_count":   len(errors),
        "warning_count": len(warnings),
        "log_tail":      log_content[-800:] if log_content else "(no log)",
    })


@mcp.tool()
def mt5_list_strategies() -> str:
    """List all .mq5 and .ex5 files in MQL5\\Experts\\."""
    experts = []
    for f in sorted(os.listdir(EXPERTS_DIR)):
        full = os.path.join(EXPERTS_DIR, f)
        if os.path.isfile(full) and f.endswith((".mq5", ".ex5")):
            experts.append({
                "name": f,
                "size": os.path.getsize(full),
                "modified": datetime.fromtimestamp(os.path.getmtime(full), tz=timezone.utc).isoformat(),
            })
    return _ok({"count": len(experts), "experts": experts})


@mcp.tool()
def mt5_read_strategy(filename: str) -> str:
    """Read MQL5 source code from MQL5\\Experts\\."""
    path = os.path.join(EXPERTS_DIR, filename)
    if not os.path.exists(path): return _err(f"Not found: {path}")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return _ok({"filename": filename, "path": path, "lines": content.count("\n"), "content": content})
    except Exception as ex:
        return _err(f"read failed: {ex}")


@mcp.tool()
def mt5_write_strategy(filename: str, content: str) -> str:
    """Write MQL5 source to MQL5\\Experts\\<filename>. Call mt5_compile after to produce .ex5."""
    path = os.path.join(EXPERTS_DIR, filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return _ok({"written": True, "path": path, "size": len(content),
                    "next_step": f"Call mt5_compile('{filename}') to produce .ex5"})
    except Exception as ex:
        return _err(f"write failed: {ex}")


# ── Backtest results (TesterStats.mqh output) ─────────────────────────────

@mcp.tool()
def mt5_bt_results(symbol: Optional[str] = None, ea: Optional[str] = None,
                   sort_by: str = "sharpe") -> str:
    """Read TesterStats JSON files from bt_results folder and return a comparison table.

    TesterStats.mqh (included in each EA) auto-saves 35+ metrics after every backtest
    to Common\\Files\\bt_results\\{EA}_{Symbol}_{TF}_{timestamp}.json

    Args:
        symbol:  filter by symbol substring (e.g. "XAUUSD")
        ea:      filter by EA name substring (e.g. "NoWick")
        sort_by: "sharpe" | "pf" (profit factor) | "profit" | "dd" (lowest drawdown first)

    Returns: sorted list of backtest results with key metrics for each run.
    """
    if not os.path.exists(BT_RESULTS_DIR):
        return _err(f"bt_results folder not found. Run a backtest first.",
                    f"Expected: {BT_RESULTS_DIR}")

    files = [f for f in os.listdir(BT_RESULTS_DIR) if f.endswith(".json")]
    if not files:
        return _ok({"count": 0, "results": [], "path": BT_RESULTS_DIR})

    results = []
    for fname in files:
        try:
            with open(os.path.join(BT_RESULTS_DIR, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
            if symbol and symbol.lower() not in str(data.get("symbol", "")).lower():
                continue
            if ea and ea.lower() not in str(data.get("ea", "")).lower():
                continue
            data["_file"] = fname
            results.append(data)
        except Exception:
            continue

    sort_key = {
        "pf":     lambda r: r.get("profit_factor", 0),
        "profit": lambda r: r.get("net_profit", 0),
        "dd":     lambda r: -(r.get("max_dd_pct", 999)),  # negate so lowest DD sorts first
    }.get(sort_by, lambda r: r.get("sharpe_ratio", 0))

    results.sort(key=sort_key, reverse=True)

    # Build a compact comparison summary for quick reading
    summary = []
    for r in results:
        summary.append({
            "ea":           r.get("ea", "?"),
            "symbol":       r.get("symbol", "?"),
            "timeframe":    r.get("timeframe", "?"),
            "period":       f"{r.get('start_date','?')} → {r.get('end_date','?')}",
            "net_profit":   r.get("net_profit"),
            "profit_factor":r.get("profit_factor"),
            "sharpe_ratio": r.get("sharpe_ratio"),
            "max_dd_pct":   r.get("max_dd_pct"),
            "trades":       r.get("trades"),
            "win_rate":     r.get("win_rate_pct"),
            "recovery":     r.get("recovery_factor"),
            "expected_payoff": r.get("expected_payoff"),
            "_file":        r.get("_file"),
        })

    return _ok({
        "count":    len(results),
        "sort_by":  sort_by,
        "path":     BT_RESULTS_DIR,
        "summary":  summary,
        "full":     results,
    })


# ── Strategy Tester automation ────────────────────────────────────────────

@mcp.tool()
def mt5_tester_configure(
    expert: Optional[str]    = None,
    symbol: Optional[str]    = None,
    timeframe: Optional[str] = None,
    start_date: Optional[str]= None,
    end_date: Optional[str]  = None,
    modelling: Optional[str] = None,
    delays: Optional[str]    = None,
    deposit: Optional[str]   = None,
    currency: Optional[str]  = None,
    leverage: Optional[str]  = None,
) -> str:
    """Configure Strategy Tester fields via Win32 SendMessage (MT5 stays alive, no focus stealing).

    Args:
        expert:     EA filename as shown in MT5 dropdown, e.g. "NoWick_XAUUSD_Optimized.ex5"
        symbol:     e.g. "XAUUSD"
        timeframe:  "M1" "M5" "M15" "M30" "H1" "H4" "D1" "W1" "MN1"
        start_date: "YYYY-MM-DD"
        end_date:   "YYYY-MM-DD"
        modelling:  "1 minute OHLC" | "Every tick" | "Open prices only"
        deposit:    "100000"
        currency:   "USD"
        leverage:   "1:100"
    """
    # Build query string — GET avoids the urllib POST issue in long-running MCP processes
    import urllib.parse
    qs = urllib.parse.urlencode({k: v for k, v in {
        "expert": expert, "symbol": symbol, "timeframe": timeframe,
        "start_date": start_date, "end_date": end_date,
        "modelling": modelling, "delays": delays,
        "deposit": deposit, "currency": currency, "leverage": leverage,
    }.items() if v is not None})
    return _ok(_bridge_get(f"/tester/configure?{qs}", timeout=15))


@mcp.tool()
def mt5_tester_run(timeout: int = 1800, focus_tab: str = "Graph") -> str:
    """Click Strategy Tester Start, auto-switch to tab, poll until complete.

    Args:
        timeout:   max seconds to wait (default 1800 = 30 min)
        focus_tab: tab to switch to after Start click (default "Graph"; "none" to skip)

    Returns: {"status": "completed"|"timeout", "elapsed_seconds": float}
    """
    return _ok(_bridge_get(f"/tester/run?timeout={timeout}&focus_tab={focus_tab}",
                           timeout=timeout + 30))


@mcp.tool()
def mt5_tester_show_tab(name: str = "Graph") -> str:
    """Switch Strategy Tester active tab by name (Graph, Backtest, Journal, Settings)."""
    return _ok(_bridge_get(f"/tester/show_tab?name={name}", timeout=10))


@mcp.tool()
def mt5_tester_read_settings() -> str:
    """Read every field currently visible in the Strategy Tester settings panel.

    Returns the live state of all tester controls — Expert, Symbol, Timeframe,
    Date range, Modelling, Delays, Deposit, Currency, Leverage, Optimization,
    Forward, and whether a test is currently running.

    Use this BEFORE calling mt5_tester_configure or mt5_tester_smart_configure
    so you know exactly what's already set and what needs changing.
    Also useful for verifying settings were applied correctly after a configure call.
    """
    return _ok(_bridge_get("/tester/settings"))


@mcp.tool()
def mt5_tester_smart_configure(
    expert: Optional[str]       = None,
    symbol: Optional[str]       = None,
    timeframe: Optional[str]    = None,
    start_date: Optional[str]   = None,
    end_date: Optional[str]     = None,
    modelling: Optional[str]    = None,
    delays: Optional[str]       = None,
    deposit: Optional[str]      = None,
    currency: Optional[str]     = None,
    leverage: Optional[str]     = None,
    optimization: Optional[str] = None,
    forward_type: Optional[str] = None,
) -> str:
    """Read current Strategy Tester settings, then only change fields that differ
    from what you requested. Skips fields that are already correct.

    Smarter than mt5_tester_configure because it:
    - Reads the live panel state first
    - Reports what was already correct (no unnecessary Win32 calls)
    - Reports what it changed and what it skipped
    - Aborts if a test is currently running (avoids mid-run corruption)

    Args: same as mt5_tester_configure — only pass the fields you want to set.
    Dates accept "YYYY-MM-DD" or MT5 format "YYYY.MM.DD".
    """
    # Step 1: read current state
    current = _bridge_get("/tester/settings")
    if "error" in current:
        return _err("Could not read tester settings", current.get("error"))

    if current.get("running"):
        return _err(
            "A test is currently running — stop it first with mt5_tester_stop() "
            "before changing settings.",
            f"button_state: {current.get('button_state')}"
        )

    # Step 2: build the diff — only fields that are requested AND differ from current
    def normalize_date(d: str) -> str:
        return d.replace("-", ".") if d else d

    desired = {
        "expert":       expert,
        "symbol":       symbol,
        "timeframe":    timeframe,
        "start_date":   normalize_date(start_date) if start_date else None,
        "end_date":     normalize_date(end_date)   if end_date   else None,
        "modelling":    modelling,
        "delays":       delays,
        "deposit":      deposit,
        "currency":     currency,
        "leverage":     leverage,
        "optimization": optimization,
        "forward_type": forward_type,
    }

    already_correct = {}
    to_change = {}

    # Map from our param names to what the bridge /settings endpoint returns
    current_key_map = {
        "expert":       "expert",
        "symbol":       "symbol",
        "timeframe":    "timeframe",
        "start_date":   "start_date",
        "end_date":     "end_date",
        "modelling":    "modelling",
        "delays":       "delays",
        "deposit":      "deposit",
        "currency":     "currency",
        "leverage":     "leverage",
        "optimization": "optimization",
        "forward_type": "forward",
    }

    for param, value in desired.items():
        if value is None:
            continue
        cur_val = current.get(current_key_map.get(param, param), "")
        # For delays, current value includes ping info ("211 ms (last ping...)") —
        # if user requests a specific delay mode by keyword, do a contains check
        if param == "delays":
            if value.lower() in cur_val.lower():
                already_correct[param] = cur_val
            else:
                to_change[param] = value
        else:
            if cur_val.strip() == value.strip():
                already_correct[param] = cur_val
            else:
                to_change[param] = value

    if not to_change:
        return _ok({
            "status":          "no_changes_needed",
            "already_correct": already_correct,
            "current":         current,
        })

    # Step 3: apply only the diff
    cfg_body = {k: v for k, v in to_change.items() if k != "forward_type"}
    if "forward_type" in to_change:
        cfg_body["forward_type"] = to_change["forward_type"]

    import urllib.parse
    result = _bridge_get(f"/tester/configure?{urllib.parse.urlencode(cfg_body)}", timeout=15)

    # Step 4: re-read to verify
    verified = _bridge_get("/tester/settings")

    return _ok({
        "status":          result.get("status", "?"),
        "changed":         to_change,
        "already_correct": already_correct,
        "set_ok":          result.get("set_ok", []),
        "set_failed":      result.get("set_failed", []),
        "verified":        verified,
    })


@mcp.tool()
def mt5_tester_status() -> str:
    """Check if a backtest or optimization is currently running — instant, non-blocking.

    Returns: {"running": bool, "button_state": "Start"|"Stop"}
    Use this to poll during a long test without blocking a full mt5_tester_run call.
    """
    return _ok(_bridge_get("/tester/status"))


@mcp.tool()
def mt5_tester_stop() -> str:
    """Force-stop a running backtest or optimization immediately.

    Use when a test is stuck, taking too long, or you want to abort and try different params.
    Returns: {"status": "stop_clicked"} or {"status": "not_running"}.
    """
    return _ok(_bridge_get("/tester/stop", timeout=10))


@mcp.tool()
def mt5_tester_go(
    expert: str,
    symbol: str       = "XAUUSD",
    timeframe: str    = "H1",
    start_date: str   = "2018-01-01",
    end_date: str     = "2026-01-01",
    modelling: str    = "1 minute OHLC",
    timeout: int      = 1800,
) -> str:
    """One-shot: configure + run a single backtest. MT5 stays alive throughout.

    Args:
        expert:     EA filename (e.g. "NoWick_XAUUSD_Optimized.ex5")
        symbol:     defaults "XAUUSD"
        timeframe:  defaults "H1"
        start_date / end_date: ISO "YYYY-MM-DD"
        modelling:  "1 minute OHLC" (default, fast) | "Every tick"
        timeout:    max seconds

    Returns: {"configure": {...}, "run": {"status", "elapsed_seconds"}}
    """
    import urllib.parse
    qs = urllib.parse.urlencode({"expert": expert, "symbol": symbol, "timeframe": timeframe,
                                 "start_date": start_date, "end_date": end_date, "modelling": modelling})
    cfg = _bridge_get(f"/tester/configure?{qs}", timeout=15)
    if "error" in cfg:
        return _ok({"configure": cfg, "run": {"skipped": "configure failed"}})
    run = _bridge_get(f"/tester/run?timeout={timeout}", timeout=timeout + 30)
    return _ok({"configure": cfg, "run": run})


@mcp.tool()
def mt5_compare_run(
    experts: str,
    symbol: str       = "XAUUSD",
    timeframe: str    = "H1",
    start_date: str   = "2020-01-01",
    end_date: str     = "2026-01-01",
    modelling: str    = "1 minute OHLC",
    timeout: int      = 1800,
    sort_by: str      = "sharpe",
) -> str:
    """Run multiple EAs back-to-back on the same parameters, then return a side-by-side comparison.

    Each EA must have TesterStats.mqh included — it auto-saves results after each run.
    This tool runs them sequentially, then reads the results and returns a ranked table.

    Args:
        experts:    comma-separated list of .ex5 filenames,
                    e.g. "NoWick_XAUUSD_Optimized.ex5,GoldTimeBased_Moderate.ex5"
        symbol:     e.g. "XAUUSD"
        timeframe:  "H1"
        start_date: "YYYY-MM-DD"
        end_date:   "YYYY-MM-DD"
        modelling:  "1 minute OHLC" | "Every tick"
        timeout:    max seconds per backtest
        sort_by:    "sharpe" | "pf" | "profit" | "dd"

    Returns: per-run results + ranked comparison table.
    """
    expert_list = [e.strip() for e in experts.split(",") if e.strip()]
    if not expert_list:
        return _err("experts must be a comma-separated list of .ex5 filenames")

    import urllib.parse
    runs = []
    for expert in expert_list:
        t0 = time.time()
        qs = urllib.parse.urlencode({"expert": expert, "symbol": symbol, "timeframe": timeframe,
                                     "start_date": start_date, "end_date": end_date, "modelling": modelling})
        cfg = _bridge_get(f"/tester/configure?{qs}", timeout=15)
        if "error" in cfg:
            runs.append({"expert": expert, "configure": cfg, "run": {"skipped": True}})
            continue
        run = _bridge_get(f"/tester/run?timeout={timeout}&focus_tab=Backtest",
                          timeout=timeout + 30)
        # Small pause so TesterStats.mqh has time to flush the JSON file
        time.sleep(1.5)
        runs.append({
            "expert":       expert,
            "configure":    cfg,
            "run":          run,
            "wall_seconds": round(time.time() - t0, 1),
        })

    # Read bt_results filtered to this symbol (each EA writes its own file)
    bt = {}
    try:
        import json as _json
        if os.path.exists(BT_RESULTS_DIR):
            for fname in os.listdir(BT_RESULTS_DIR):
                if not fname.endswith(".json"): continue
                try:
                    with open(os.path.join(BT_RESULTS_DIR, fname), "r", encoding="utf-8") as f:
                        data = _json.load(f)
                    ea_name = data.get("ea", "")
                    sym     = data.get("symbol", "")
                    if symbol.lower() in sym.lower():
                        key = ea_name
                        if key not in bt or data.get("timestamp", "") > bt[key].get("timestamp", ""):
                            bt[key] = data  # keep most recent per EA
                except Exception:
                    continue
    except Exception:
        pass

    sort_fn = {
        "pf":     lambda r: r.get("profit_factor", 0),
        "profit": lambda r: r.get("net_profit", 0),
        "dd":     lambda r: -(r.get("max_dd_pct", 999)),
    }.get(sort_by, lambda r: r.get("sharpe_ratio", 0))

    comparison = sorted(bt.values(), key=sort_fn, reverse=True)
    table = [{
        "rank":         i + 1,
        "ea":           r.get("ea"),
        "net_profit":   r.get("net_profit"),
        "profit_factor":r.get("profit_factor"),
        "sharpe_ratio": r.get("sharpe_ratio"),
        "max_dd_pct":   r.get("max_dd_pct"),
        "trades":       r.get("trades"),
        "win_rate":     r.get("win_rate_pct"),
        "recovery":     r.get("recovery_factor"),
    } for i, r in enumerate(comparison)]

    return _ok({
        "params":     {"symbol": symbol, "timeframe": timeframe,
                       "start_date": start_date, "end_date": end_date, "modelling": modelling},
        "runs":       runs,
        "comparison": table,
        "sort_by":    sort_by,
    })


@mcp.tool()
def mt5_quick_compare(
    experts: str,
    symbol: str    = "XAUUSD",
    timeframe: str = "H1",
    start_date: str = "2020-01-01",
    end_date: str   = "2026-01-01",
    sort_by: str    = "sharpe",
) -> str:
    """Ultra-fast sequential comparison of multiple EAs using 'Open prices only' modelling.

    'Open prices only' is 10-20x faster than '1 minute OHLC' and gives identical results
    for EAs that enter/exit at bar opens or use time-based logic. A full XAUUSD H1 backtest
    completes in 1-5 seconds instead of 30-60 seconds.

    Tab switching is skipped (saves 3 seconds per test). Results are collected immediately
    from TesterStats files with no artificial sleep.

    Args:
        experts:    comma-separated .ex5 filenames, e.g.
                    "NoWick_XAUUSD_Optimized.ex5,GoldTimeBased_Moderate.ex5"
        symbol:     "XAUUSD"
        timeframe:  "H1"
        start_date: "YYYY-MM-DD"
        end_date:   "YYYY-MM-DD"
        sort_by:    "sharpe" | "pf" | "profit" | "dd"

    Returns: ranked comparison table + per-run timing.
    """
    expert_list = [e.strip() for e in experts.split(",") if e.strip()]
    if not expert_list:
        return _err("experts must be a comma-separated list of .ex5 filenames")

    # Snapshot files already in bt_results before we start, to detect new ones
    existing_files: set = set()
    if os.path.exists(BT_RESULTS_DIR):
        existing_files = {f for f in os.listdir(BT_RESULTS_DIR) if f.endswith(".json")}

    runs = []
    for expert in expert_list:
        t0 = time.time()

        import urllib.parse
        qs = urllib.parse.urlencode({"expert": expert, "symbol": symbol, "timeframe": timeframe,
                                     "start_date": start_date, "end_date": end_date,
                                     "modelling": "Open prices only"})
        cfg = _bridge_get(f"/tester/configure?{qs}", timeout=15)
        if "error" in cfg:
            runs.append({"expert": expert, "error": cfg["error"], "elapsed_sec": 0})
            continue

        run = _bridge_get("/tester/run?timeout=300&focus_tab=none", timeout=330)
        elapsed = round(time.time() - t0, 2)

        # Find the new result file this EA wrote (no sleep needed — file is written synchronously
        # by OnTester() before the Start button text changes back to "Start")
        new_result = None
        if os.path.exists(BT_RESULTS_DIR):
            for fname in os.listdir(BT_RESULTS_DIR):
                if fname.endswith(".json") and fname not in existing_files:
                    ea_slug = expert.replace(".ex5", "").replace(" ", "_")
                    if any(part.lower() in fname.lower() for part in ea_slug.split("_")[:2]):
                        try:
                            with open(os.path.join(BT_RESULTS_DIR, fname), "r", encoding="utf-8") as f:
                                new_result = json.load(f)
                            existing_files.add(fname)
                            break
                        except Exception:
                            continue

        runs.append({
            "expert":      expert,
            "run_status":  run.get("status", "?"),
            "elapsed_sec": elapsed,
            "result":      new_result,
        })

    # Build ranked table from collected results
    valid = [r for r in runs if r.get("result")]
    sort_fn = {
        "pf":     lambda r: r["result"].get("profit_factor", 0),
        "profit": lambda r: r["result"].get("net_profit", 0),
        "dd":     lambda r: -(r["result"].get("max_dd_pct", 999)),
    }.get(sort_by, lambda r: r["result"].get("sharpe_ratio", 0))
    valid.sort(key=sort_fn, reverse=True)

    table = []
    for i, r in enumerate(valid):
        res = r["result"]
        table.append({
            "rank":          i + 1,
            "ea":            r["expert"],
            "elapsed_sec":   r["elapsed_sec"],
            "net_profit":    res.get("net_profit"),
            "profit_factor": res.get("profit_factor"),
            "sharpe_ratio":  res.get("sharpe_ratio"),
            "max_dd_pct":    res.get("max_dd_pct"),
            "win_rate_pct":  res.get("win_rate_pct"),
            "trades":        res.get("trades"),
            "recovery":      res.get("recovery_factor"),
            "period":        f"{res.get('start_date','?')} → {res.get('end_date','?')}",
        })

    total_elapsed = sum(r["elapsed_sec"] for r in runs)
    return _ok({
        "modelling":     "Open prices only",
        "params":        {"symbol": symbol, "timeframe": timeframe,
                          "start_date": start_date, "end_date": end_date},
        "total_elapsed_sec": round(total_elapsed, 1),
        "eas_tested":    len(runs),
        "sort_by":       sort_by,
        "ranking":       table,
        "raw_runs":      runs,
    })


@mcp.tool()
def mt5_sweep(
    expert: str,
    symbol: str    = "XAUUSD",
    timeframe: str = "H1",
    start_date: str = "2020-01-01",
    end_date: str   = "2026-01-01",
    modelling: str  = "Open prices only",
    optimization: str = "fast",
    timeout: int    = 3600,
    sort_by: str    = "sharpe",
) -> str:
    """Run MT5's built-in parallel optimization sweep on one EA — all parameter combinations
    run simultaneously across all CPU cores (agents). This is the fastest way to sweep one EA.

    BEFORE calling this:
      1. Open MT5 Strategy Tester → Inputs tab for the EA
      2. Double-click each parameter you want to vary → set Start / Step / Stop ranges
      3. Then call mt5_sweep — it sets the mode and clicks Start

    The bridge clicks Start once and waits. MT5 distributes parameter passes across all CPU
    cores in parallel. TesterStats.mqh writes one JSON file per pass with unique tick-count
    filenames so no passes collide.

    Args:
        expert:       EA filename (e.g. "NoWick_XAUUSD_Optimized.ex5")
        symbol:       "XAUUSD"
        timeframe:    "H1"
        start_date:   "YYYY-MM-DD"
        end_date:     "YYYY-MM-DD"
        modelling:    "Open prices only" (default, fastest) | "1 minute OHLC"
        optimization: "fast" (genetic algorithm) | "full" (exhaustive)
        timeout:      max seconds to wait for all passes (default 3600 = 1 hour)
        sort_by:      "sharpe" | "pf" | "profit" | "dd"

    Returns: all pass results ranked by sort_by metric.
    """
    opt_mode = "Fast genetic algorithm" if optimization == "fast" else "Slow complete algorithm"

    # Snapshot existing bt_results files
    existing_files: set = set()
    if os.path.exists(BT_RESULTS_DIR):
        existing_files = {f for f in os.listdir(BT_RESULTS_DIR) if f.endswith(".json")}

    t0 = time.time()

    import urllib.parse
    qs = urllib.parse.urlencode({"expert": expert, "symbol": symbol, "timeframe": timeframe,
                                 "start_date": start_date, "end_date": end_date,
                                 "modelling": modelling, "optimization": opt_mode})
    cfg = _bridge_get(f"/tester/configure?{qs}", timeout=15)
    if "error" in cfg:
        return _ok({"error": "configure failed", "detail": cfg})

    run = _bridge_get(f"/tester/run?timeout={timeout}&focus_tab=none", timeout=timeout + 30)
    elapsed = round(time.time() - t0, 1)

    # Collect all new result files written during the sweep
    ea_slug = expert.replace(".ex5", "").replace(".mq5", "")
    new_results = []
    if os.path.exists(BT_RESULTS_DIR):
        for fname in sorted(os.listdir(BT_RESULTS_DIR)):
            if not fname.endswith(".json") or fname in existing_files:
                continue
            try:
                with open(os.path.join(BT_RESULTS_DIR, fname), "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["_file"] = fname
                new_results.append(data)
            except Exception:
                continue

    sort_fn = {
        "pf":     lambda r: r.get("profit_factor", 0),
        "profit": lambda r: r.get("net_profit", 0),
        "dd":     lambda r: -(r.get("max_dd_pct", 999)),
    }.get(sort_by, lambda r: r.get("sharpe_ratio", 0))

    new_results.sort(key=sort_fn, reverse=True)

    table = []
    for i, r in enumerate(new_results):
        table.append({
            "rank":          i + 1,
            "ea":            r.get("ea"),
            "net_profit":    r.get("net_profit"),
            "profit_factor": r.get("profit_factor"),
            "sharpe_ratio":  r.get("sharpe_ratio"),
            "max_dd_pct":    r.get("max_dd_pct"),
            "win_rate_pct":  r.get("win_rate_pct"),
            "trades":        r.get("trades"),
            "recovery":      r.get("recovery_factor"),
            "_file":         r.get("_file"),
        })

    return _ok({
        "expert":            expert,
        "optimization_mode": opt_mode,
        "modelling":         modelling,
        "params":            {"symbol": symbol, "timeframe": timeframe,
                              "start_date": start_date, "end_date": end_date},
        "run_status":        run.get("status"),
        "total_elapsed_sec": elapsed,
        "passes_collected":  len(new_results),
        "sort_by":           sort_by,
        "ranking":           table,
    })


# ── Headless parallel backtesting (general, no EA edits, no GUI) ───────────
# Drives the separate portable MT5 worker pool (C:\MT5_Headless*) and parses
# MT5's own native report — the fastest + most robust + most accurate path.
# Live GUI terminal + BridgePro are never touched.

import sys as _sys
_POOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _POOL_DIR not in _sys.path:
    _sys.path.insert(0, _POOL_DIR)


@mcp.tool()
def mt5_headless_compare(
    experts: str,
    symbol: str     = "XAUUSD",
    period: str     = "H2",
    start_date: str = "2020.01.01",
    end_date: str   = "2025.12.31",
    model: str      = "auto",
    deposit: str    = "200000",
    leverage: str   = "100",
    sort_by: str    = "sharpe",
    timeout: int    = 900,
    max_workers: Optional[int] = None,
    export: bool    = True,
    pdf: bool       = False,
    open_report: bool = True,
) -> str:
    """Backtest/compare one or more EAs in PARALLEL via the headless worker pool.

    The general extractor: NO EA source changes, NO OnTester include, NO GUI.
    Each EA runs in its own portable MT5 instance and we parse MT5's OWN native
    report (authoritative PF, Sharpe, DD, best/worst trade). Results also land in
    Common\\Files\\bt_results as JSON.

    Args:
        experts:    comma-separated .ex5 filenames, e.g.
                    "NoWick_XAUUSD_Optimized.ex5,GoldTimeStrategy.ex5"
        symbol:     e.g. "XAUUSD"
        period:     M1 M5 M15 M30 H1 H2 H4 D1 W1 MN1
        start_date / end_date: "YYYY.MM.DD" or "YYYY-MM-DD"
        model:      "0" every-tick (truth) | "1" 1-min OHLC | "2" open prices (fastest)
        sort_by:    "sharpe" | "pf" | "profit" | "dd"
        timeout:    max seconds per EA
        max_workers: cap parallel workers (default = all available)

    Returns: ranked comparison table (incl. best/worst trade) + any failed/missing EAs.
    """
    import headless_pool as hp
    ea_list = [e.strip() for e in experts.split(",") if e.strip()]
    if not ea_list:
        return _err("experts must be a comma-separated list of .ex5 filenames")
    frm = start_date.replace("-", ".")
    to  = end_date.replace("-", ".")
    out = hp.run_pool(ea_list, symbol, period, frm, to, model,
                      deposit, leverage, timeout, max_workers, sort_by,
                      export, pdf, open_report)
    return _ok(out)


if __name__ == "__main__":
    mcp.run()
