"""
MT5-Interlink — single source of truth for all environment-specific config.

Every other module imports from here. Nothing else hardcodes a path, terminal
id, login, or server. All values are env-overridable, with the current machine's
values as defaults, so the stack is a self-contained, relocatable standalone:
no references to the surrounding project folder, backups, or older pipelines.

Override any value by setting the matching env var (e.g. in .mcp.json):
    MT5_TERMINAL_ID, MT5_LOGIN, MT5_SERVER,
    MT5_WORKER_ROOT, MT5_EXPORT_ROOT, MT5_BRIDGE_URL
"""
import glob
import os

# This file's folder == the MT5-Interlink root (self-locating; no absolute paths).
INTERLINK_ROOT = os.path.dirname(os.path.abspath(__file__))
APPDATA = os.environ.get("APPDATA", "")

# ── Live (GUI) terminal that runs BridgePro ───────────────────────────────
TERMINAL_ID = os.environ.get("MT5_TERMINAL_ID", "608AB61EFF9C7B3585EC08B8CF6800E3")
LOGIN       = os.environ.get("MT5_LOGIN",  "7941301")
SERVER      = os.environ.get("MT5_SERVER", "Eightcap-Demo")
BRIDGE_URL  = os.environ.get("MT5_BRIDGE_URL", "http://localhost:8892")

TERMINAL_DATA = os.path.join(APPDATA, "MetaQuotes", "Terminal", TERMINAL_ID)
LIVE_EXPERTS  = os.path.join(TERMINAL_DATA, "MQL5", "Experts")
BT_RESULTS    = os.path.join(APPDATA, "MetaQuotes", "Terminal", "Common", "Files", "bt_results")

# ── Headless worker pool ──────────────────────────────────────────────────
# Worker dirs are WORKER_ROOT, WORKER_ROOT_2, WORKER_ROOT_3, ...
WORKER_ROOT = os.environ.get("MT5_WORKER_ROOT", r"C:\MT5_Headless")

# ── Report export — "Backtest_reports" in the working folder (parent of MT5-Interlink) ──
# Relative to this package, so it stays valid if the project is relocated.
# Override anywhere via MT5_EXPORT_ROOT.
EXPORT_ROOT = os.environ.get(
    "MT5_EXPORT_ROOT",
    os.path.join(os.path.dirname(INTERLINK_ROOT), "Backtest_reports"))


def discover_workers():
    """All ready headless workers under WORKER_ROOT (have terminal64.exe)."""
    ws = []
    if os.path.isdir(WORKER_ROOT):
        ws.append(WORKER_ROOT)
    ws += sorted(glob.glob(WORKER_ROOT + "_*"))
    return [w for w in ws if os.path.exists(os.path.join(w, "terminal64.exe"))]


def find_edge():
    """Locate Edge for headless HTML->PDF (built into Windows; no install)."""
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(p):
            return p
    return None


def find_live_ea(ea):
    """Locate an EA .ex5 anywhere under the live terminal's Experts tree."""
    direct = os.path.join(LIVE_EXPERTS, ea)
    if os.path.exists(direct):
        return direct
    for f in glob.glob(os.path.join(LIVE_EXPERTS, "**", ea), recursive=True):
        return f
    return None
