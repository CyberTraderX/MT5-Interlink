#!/usr/bin/env python3
"""
MT5-Interlink dev cycle — ONE command rebuilds + deploys the full stack.

Steps:
  1. Bump version in MT5Bridge.cs
  2. dotnet publish (NativeAOT) → bin/.../MT5Bridge.dll
  3. Kill MT5 (releases DLL lock)
  4. Copy DLL + WPF helpers → MQL5/Libraries/
  5. Copy BridgePro.mq5 → MQL5/Experts/  (source reference copy)
  6. Compile BridgePro.mq5 → BridgePro.ex5 via metaeditor64.exe
  7. Print status

User then: reopen MT5, attach BridgePro to any chart with Port=8892, Allow DLL imports.
Verify:    curl http://localhost:8892/version

Usage:
    python dev_cycle.py                  # full cycle (kills MT5)
    python dev_cycle.py --no-kill        # skip MT5 kill (fails if DLL is locked)
    python dev_cycle.py --bump=minor     # bump minor version
    python dev_cycle.py --tid <hex>      # override MT5_TERMINAL_ID for one run
    python dev_cycle.py --deploy-only    # skip build, just copy files
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PUBLISH_DIR  = PROJECT_ROOT / "bin" / "Release" / "net10.0-windows" / "win-x64" / "publish"
CS_SRC       = PROJECT_ROOT / "MT5Bridge.cs"

# BridgePro.mq5 lives in MQL5\Experts — this is the source we copy FROM when deploying.
# The file in MT5-Interlink is the authoritative source; dev_cycle syncs it to MT5.
BRIDGEPRO_SRC = PROJECT_ROOT / "BridgePro.mq5"

TERMINAL_ID = os.environ.get("MT5_TERMINAL_ID", "608AB61EFF9C7B3585EC08B8CF6800E3")

DLL_HELPERS = [
    "MT5Bridge.dll",
    "D3DCompiler_47_cor3.dll",
    "PenImc_cor3.dll",
    "PresentationNative_cor3.dll",
    "vcruntime140_cor3.dll",
    "wpfgfx_cor3.dll",
]


def terminal_paths(tid: str) -> dict:
    appdata = os.environ.get("APPDATA", "")
    base = Path(appdata) / "MetaQuotes" / "Terminal" / tid
    return {
        "base": base,
        "libraries": base / "MQL5" / "Libraries",
        "experts":   base / "MQL5" / "Experts",
    }


def read_cs_version() -> tuple[int, int, int]:
    cs = CS_SRC.read_text(encoding="utf-8")
    m = re.search(r'private const string VERSION = "(\d+)\.(\d+)\.(\d+)"', cs)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def bump_version(component: str = "patch") -> str:
    v = read_cs_version()
    if component == "major":   new_v = (v[0]+1, 0, 0)
    elif component == "minor": new_v = (v[0], v[1]+1, 0)
    else:                      new_v = (v[0], v[1], v[2]+1)
    new_str = f"{new_v[0]}.{new_v[1]}.{new_v[2]}"
    cs = CS_SRC.read_text(encoding="utf-8")
    cs = re.sub(r'private const string VERSION = "[\d\.]+"',
                f'private const string VERSION = "{new_str}"', cs)
    CS_SRC.write_text(cs, encoding="utf-8")
    return new_str


# NativeAOT requires the MSVC linker (link.exe). VsDevCmd.bat must be from a VS install
# that has the C++ workload — on this machine that's BuildTools, not Community.
VS_DEV_CMD_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\Common7\Tools\VsDevCmd.bat",
    r"C:\Program Files\Microsoft Visual Studio\18\Enterprise\Common7\Tools\VsDevCmd.bat",
    r"C:\Program Files\Microsoft Visual Studio\18\Professional\Common7\Tools\VsDevCmd.bat",
    r"C:\Program Files\Microsoft Visual Studio\18\Community\Common7\Tools\VsDevCmd.bat",
]
VS_DEV_CMD = next((p for p in VS_DEV_CMD_CANDIDATES if os.path.exists(p)), VS_DEV_CMD_CANDIDATES[0])

# vswhere.exe lives in the shared Installer folder; VsDevCmd.bat needs it on PATH.
VSWHERE_DIR = r"C:\Program Files (x86)\Microsoft Visual Studio\Installer"


def dotnet_build() -> dict:
    # NativeAOT needs MSVC link.exe + cl.exe (IlcUseEnvironmentalTools=true in .csproj).
    # We write a temp .bat that loads BuildTools VsDevCmd, then runs dotnet publish.
    # vswhere.exe is prepended to PATH so VsDevCmd.bat can resolve the VS install.
    import tempfile
    bat_content = (
        "@echo off\r\n"
        f'set "PATH={VSWHERE_DIR};%PATH%"\r\n'
        f'call "{VS_DEV_CMD}" -arch=amd64 -host_arch=amd64 -no_logo\r\n'
        "if errorlevel 1 exit /b 1\r\n"
        f'cd /d "{PROJECT_ROOT}"\r\n'
        "dotnet publish -c Release\r\n"
        "exit /b %errorlevel%\r\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".bat", delete=False, encoding="ascii") as f:
        f.write(bat_content)
        bat_path = f.name
    try:
        result = subprocess.run(
            [bat_path],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=600, shell=False,
        )
    finally:
        try: os.unlink(bat_path)
        except Exception: pass
    dll = PUBLISH_DIR / "MT5Bridge.dll"
    return {
        "exit":       result.returncode,
        "dll_built":  dll.exists(),
        "dll_size_kb": round(dll.stat().st_size / 1024) if dll.exists() else 0,
        "stderr_tail": (result.stdout + result.stderr)[-800:] if result.returncode != 0 else "",
    }


def kill_mt5() -> dict:
    out = subprocess.run(["taskkill", "/F", "/IM", "terminal64.exe"],
                         capture_output=True, text=True)
    time.sleep(1.5)
    return {"exit": out.returncode, "msg": (out.stdout + out.stderr).strip()}


def deploy_files(tid: str = TERMINAL_ID) -> dict:
    paths = terminal_paths(tid)
    copied, failed = [], []

    # DLL + WPF helpers → Libraries
    for fname in DLL_HELPERS:
        src = PUBLISH_DIR / fname
        dst = paths["libraries"] / fname
        try:
            if src.exists():
                shutil.copy2(src, dst)
                copied.append(f"libraries/{fname}")
            else:
                failed.append(f"libraries/{fname} (source missing from publish output)")
        except Exception as e:
            failed.append(f"libraries/{fname}: {e}")

    # BridgePro.mq5 → Experts (if source exists in this folder)
    if BRIDGEPRO_SRC.exists():
        try:
            shutil.copy2(BRIDGEPRO_SRC, paths["experts"] / "BridgePro.mq5")
            copied.append("experts/BridgePro.mq5")
        except Exception as e:
            failed.append(f"experts/BridgePro.mq5: {e}")
    else:
        failed.append("experts/BridgePro.mq5 (not found in MT5-Interlink — copy it here first)")

    return {"copied": copied, "failed": failed}


def find_metaeditor() -> str:
    # Try to find metaeditor64.exe from the running MT5 process first.
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='terminal64.exe'", "get", "ExecutablePath", "/format:list"],
            capture_output=True, text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            if line.startswith("ExecutablePath="):
                p = line.split("=", 1)[1].strip()
                if p:
                    me = os.path.join(os.path.dirname(p), "metaeditor64.exe")
                    if os.path.exists(me): return me
    except Exception:
        pass
    for guess in [
        r"C:\Program Files\EightCap MetaTrader 5\MetaEditor64.exe",
        r"C:\Program Files\MetaTrader 5\metaeditor64.exe",
        r"C:\Program Files (x86)\MetaTrader 5\metaeditor64.exe",
    ]:
        if os.path.exists(guess): return guess
    return ""


def compile_bridgepro(tid: str = TERMINAL_ID) -> dict:
    paths = terminal_paths(tid)
    me = find_metaeditor()
    if not me:
        return {"error": "metaeditor64.exe not found — add broker path to find_metaeditor()"}
    ea_path = str(paths["experts"] / "BridgePro.mq5")
    subprocess.run([me, f"/compile:{ea_path}"], capture_output=True, timeout=60)
    ex5 = ea_path.replace(".mq5", ".ex5")
    return {"ex5_exists": os.path.exists(ex5), "ex5_path": ex5}


def full_cycle(bump: str = "patch", kill: bool = True,
               tid: str = TERMINAL_ID, deploy_only: bool = False) -> dict:
    out: dict = {"started": time.time()}

    if not deploy_only:
        out["1_bump"]  = {"new_version": bump_version(bump)}
        out["2_build"] = dotnet_build()
        # exit 0 means the DLL is freshly built. A stale DLL on disk from a prior
        # successful build is NOT a green light — we'd deploy an old version.
        if out["2_build"].get("exit") != 0:
            out["status"] = "BUILD_FAILED"
            return out
    else:
        out["1_bump"]  = {"skipped": True}
        out["2_build"] = {"skipped": True}

    out["3_kill"]   = kill_mt5() if kill else {"skipped": True}
    out["4_deploy"] = deploy_files(tid)
    if out["4_deploy"]["failed"]:
        out["status"] = "DEPLOY_PARTIAL"

    out["5_compile"] = compile_bridgepro(tid)
    if not out["5_compile"].get("ex5_exists"):
        out["status"] = out.get("status", "EA_COMPILE_FAILED")

    if "status" not in out:
        out["status"] = "DEPLOYED"

    out["next_steps"] = [
        "Reopen MT5",
        "Drag BridgePro onto any chart",
        "Set Port = 8892 in the Inputs dialog",
        "Tick 'Allow DLL imports' → OK",
        "Verify: curl http://localhost:8892/version",
    ]
    out["elapsed_sec"] = round(time.time() - out["started"], 1)
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="MT5-Interlink build + deploy")
    p.add_argument("--bump",        choices=["patch", "minor", "major"], default="patch")
    p.add_argument("--no-kill",     action="store_true", help="skip killing MT5")
    p.add_argument("--deploy-only", action="store_true", help="skip build, just deploy")
    p.add_argument("--tid",         default=TERMINAL_ID, help="override MT5_TERMINAL_ID")
    args = p.parse_args()
    print(json.dumps(
        full_cycle(bump=args.bump, kill=not args.no_kill,
                   tid=args.tid, deploy_only=args.deploy_only),
        indent=2
    ))
