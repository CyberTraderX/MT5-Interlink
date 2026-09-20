# MT5 Strategy Tester — Control Map

Discovered via `inspect_mt5.ps1` against a live MT5 terminal (build inferred from MFC `Afx:ControlBar`). Tested on MetaQuotes-branded and broker-branded MT5 (control IDs are the same across brokers).

## Architecture note

The Strategy Tester panel is **NOT WPF**. It's built with Microsoft Foundation Classes (MFC):

- Outer panel: `Afx:ControlBar` (id=32846, name="Strategy Tester")
- Inner dialog: `#32770` (Win32 dialog class, id=10476)
- Fields: standard Win32 `ComboBox`, `SysDateTimePick32`, `Button` controls

Win32 control IDs are compiled into `terminal64.exe` — they don't drift between releases the way WPF AutomationIds do. **This is why we can hardcode them.**

## Field map (current as of MT5 build active April 19, 2026)

```
Strategy Tester panel  (Afx:ControlBar, id=32846)
└── Inner dialog       (#32770, id=10476)
    ├── Expert         ComboBox       id=10485
    ├── Symbol         ComboBox       id=10486
    ├── Timeframe      ComboBox       id=10487
    ├── Date type      ComboBox       id=10123   ("Custom period", "All history", "Last month", etc.)
    ├── Start date     SysDateTimePick32  id=10550
    ├── End date       SysDateTimePick32  id=10551
    ├── Forward type   ComboBox       id=10492   ("No", "1/2", "1/3", "Custom")
    ├── Forward date   SysDateTimePick32  id=10505
    ├── Delays         ComboBox       id=10488
    ├── Modelling      ComboBox       id=10515   ("Every tick", "1 minute OHLC", "Open prices only", "Real ticks")
    ├── Profit-in-pips Button (chk)   id=11003
    ├── Deposit        ComboBox       id=10489
    ├── Currency       ComboBox       id=10559
    ├── Leverage       ComboBox       id=10473
    ├── Visual mode    Button (chk)   id=10490
    └── Optimization   ComboBox       id=10491   ("Disabled", "Slow complete algorithm", "Fast genetic algorithm", "Forward")
└── Tab control        SysTabControl32 (id=10002)
    └── Start button   Button         id=16790
```

## Auxiliary buttons (not yet mapped)

- id=11000, 11001 — IDE / Settings buttons next to Expert
- id=10885 — $ button next to Symbol (commission/contract settings)
- id=11040 — settings cog next to Delays

## Ways to drive these from C# (in order of preference)

### Option A — Direct Win32 SendMessage (fastest, most stable)
```csharp
[DllImport("user32.dll")] static extern IntPtr GetDlgItem(IntPtr hDlg, int nIDDlgItem);
[DllImport("user32.dll")] static extern int SendMessage(IntPtr hWnd, uint Msg, IntPtr wParam, string lParam);

// Set Symbol field
IntPtr hSymbol = GetDlgItem(hStInner, 10486);
SendMessage(hSymbol, WM_SETTEXT, IntPtr.Zero, "XAUUSD");
```

### Option B — UIAutomation via AutomationId (cleaner, ~10x slower)
```csharp
var symbol = stPane.FindFirst(TreeScope.Descendants,
    new PropertyCondition(AutomationElement.AutomationIdProperty, "10486"));
((ValuePattern)symbol.GetCurrentPattern(ValuePattern.Pattern)).SetValue("XAUUSD");
```

For our use case (fire backtests, not live order entry), **Option B is fine** — we only set fields ~7-8 times per backtest, the latency is negligible vs the actual test runtime.

For batch sweeps with 1000s of small param changes, switch to Option A.

## Patterns required per control type

| Control | UIAutomation Pattern | Win32 Message |
|---|---|---|
| ComboBox (text editable) | `ValuePattern.SetValue` | `WM_SETTEXT` |
| ComboBox (selection) | `ExpandCollapsePattern` + click ListItem | `CB_SELECTSTRING` |
| SysDateTimePick32 | None — UIAutomation doesn't expose date directly | `DTM_SETSYSTEMTIME` (Win32) |
| Button (push) | `InvokePattern.Invoke` | `BM_CLICK` |
| Button (checkbox) | `TogglePattern.Toggle` | `BM_CLICK` if state read |

**Date pickers are the trick** — they have no UIAutomation pattern for date setting. Either:
(a) Send Win32 `DTM_SETSYSTEMTIME` message with a `SYSTEMTIME` struct
(b) Click the field, send keystrokes (`{F4}` opens calendar, type date)

Approach (a) is what `MT5Bridge.cs` will use — single P/Invoke, deterministic, no focus race conditions.
