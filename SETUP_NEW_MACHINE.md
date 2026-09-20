# MT5-Interlink — setup on a second machine

This folder is portable. Copy it anywhere (e.g. `D:\MT5-Interlink`) and follow
the steps below. **Nothing here affects the original machine** — the live
MT5-Interlink and BridgePro on your main PC are untouched.

The only things that can't be copied are this machine's *identity* (MT5 terminal
ID, login, install path). You fill those into the env block in step 3.

---

## 1. Install Python dependencies
```powershell
cd D:\MT5-Interlink
pip install -r requirements.txt
```

## 2. Find the NEW machine's values
- **Terminal ID** — open the folder `%APPDATA%\MetaQuotes\Terminal\` and copy the
  long hex folder name that belongs to your MT5 install. (Verify it's the right
  one: it contains `MQL5\Experts`.)
- **Login / Server** — from MT5: File ▸ Login to Trade Account (the number and the
  broker server string, e.g. `Eightcap-Demo`).
- **Terminal / Editor exe paths** — where this MT5 is installed, e.g.
  `C:\Program Files\<broker> MetaTrader 5\terminal64.exe` and `MetaEditor64.exe`.

## 3. Register the MCP server (paste block)
Add this to the new machine's `.mcp.json` (project root) **or** the `mcpServers`
section of `C:\Users\<you>\.claude.json`. Fill in the 6 stubbed values.

```json
"mt5-interconnector": {
  "command": "python",
  "args": [
    "D:/MT5-Interlink/mcp/server.py"
  ],
  "env": {
    "MT5_TERMINAL_ID": "<<NEW_TERMINAL_ID>>",
    "MT5_LOGIN":       "<<NEW_LOGIN>>",
    "MT5_SERVER":      "<<NEW_SERVER>>",
    "MT5_BRIDGE_URL":  "http://localhost:8892",
    "MT5_TERMINAL_EXE": "C:/Program Files/<<BROKER>> MetaTrader 5/terminal64.exe",
    "MT5_EDITOR_EXE":   "C:/Program Files/<<BROKER>> MetaTrader 5/MetaEditor64.exe"
  }
}
```
Adjust the `args` path if you put the folder somewhere other than `D:\MT5-Interlink`.

## 4. Deploy the bridge (DLL + EA) and attach it
The bridge = the **BridgePro EA** + the native **MT5Bridge.dll** it imports.
The DLL is already compiled (NativeAOT, self-contained win-x64) and ships in this
folder — **no rebuild needed** on a 64-bit Windows PC.

Into `%APPDATA%\MetaQuotes\Terminal\<TERMINAL_ID>\MQL5\`:
- **Libraries\** ← copy `bin\Release\net10.0-windows\win-x64\native\MT5Bridge.dll`
  (the EA does `#import "MT5Bridge.dll"` and looks for it here).
- **Experts\** ← copy `BridgePro.mq5`, then compile it in MetaEditor → `BridgePro.ex5`.

Then in MT5:
- Tools ▸ Options ▸ Expert Advisors → tick **Allow DLL imports** (and Allow Algo Trading).
- Drag **BridgePro** onto any chart; enable Algo Trading.
- The Experts log should print `BridgePro v1.00: http://localhost:8892`. That means
  the HTTP server is live on `:8892` (the bridge URL in step 3).

> If MT5 says it can't load `MT5Bridge.dll`, the PC isn't win-x64, or the DLL wasn't
> placed in `MQL5\Libraries`. Re-check the path; rebuild only as a last resort
> (`dotnet publish -c Release` needs the .NET 10 SDK).

## 5. (Optional) Headless backtest pool
Only needed for the parallel backtest pipeline. Create portable MT5 copies at
`C:\MT5_Headless`, `C:\MT5_Headless_2`, … each containing `terminal64.exe`.
Override the base path with env var `MT5_WORKER_ROOT` if you use a different drive.
Live trading and the single-terminal tester do NOT need this.

## 6. Restart Claude Code
Restart so it picks up the new MCP registration, then run an MCP tool
(e.g. account snapshot / interconnect health) to confirm the link is live.

---
### Quick checklist
- [ ] `pip install -r requirements.txt`
- [ ] Terminal ID / login / server / exe paths gathered
- [ ] MCP block pasted with values filled in + correct `args` path
- [ ] BridgePro compiled + attached, listening on :8892
- [ ] (optional) `C:\MT5_Headless` pool created
- [ ] Claude Code restarted, health check passes
