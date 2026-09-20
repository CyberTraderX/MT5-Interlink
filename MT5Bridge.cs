// MT5Bridge.cs  —  MT5-Interlink v1.0
// In-process bridge for MetaTrader 5. Same DLL interface as v0.43.2 so BridgePro.mq5
// works without changes.
//
// Improvements over v0.43.2:
//   1. Full RFC-8259 Escape() — \n \r \t \b \f \uXXXX (was only " and \)
//   2. /state endpoint — all state atomic in ONE call (account+positions+orders+terminal)
//   3. /bt_results endpoint — reads TesterStats .json files, returns comparison data
//   4. CORS headers on every response
//   5. Removed stubbed /backtest job system (was TODO, added dead weight)
//   6. Updated service identity (MT5-Interlink v1.0)
//   7. Default port 8892 (avoids HTTP.sys conflict on 8889/8891)
//
// Build:  dotnet publish -c Release -r win-x64   (in this folder)
// Output: bin/Release/net10.0-windows/win-x64/publish/MT5Bridge.dll
// Deploy: run dev_cycle.py  (kills MT5, copies DLL, compiles BridgePro)

using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Automation;

namespace MT5Bridge;

public static class Bridge
{
    private static HttpListener? _listener;
    private static CancellationTokenSource? _cts;
    private static Task? _serverTask;
    private static int _port;

    private static readonly ConcurrentDictionary<string, string> _state = new();

    private const string VERSION = "1.0.23";
    private static readonly string BUILD_TIMESTAMP = DateTime.UtcNow.ToString("O");
    private static readonly string BUILD_FEATURES =
        "state-endpoint,bt-results,cors,full-json-escape,win32-tester,sta-threading,bridgepro-ea";

    private const int STA_TIMEOUT_MS = 30_000;

    // ── STA infrastructure (UIAutomation requires STA) ────────────────────

    private static T RunOnSta<T>(Func<T> func, int timeoutMs = STA_TIMEOUT_MS)
    {
        T? result = default;
        Exception? error = null;
        var thread = new Thread(() =>
        {
            try { result = func(); }
            catch (Exception ex) { error = ex; }
        })
        {
            IsBackground = true,
            Name = "MT5Interlink.STA"
        };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        if (!thread.Join(timeoutMs))
            throw new TimeoutException($"STA timed out after {timeoutMs}ms");
        if (error != null) throw error;
        return result!;
    }

    private static string RunOnStaSafe(Func<string> func, int timeoutMs = STA_TIMEOUT_MS)
    {
        try { return RunOnSta(func, timeoutMs); }
        catch (TimeoutException tex) { return Err(tex.Message, "TimeoutException"); }
        catch (AggregateException aex) when (aex.InnerException != null)
            { return Err(aex.InnerException.Message, aex.InnerException.GetType().Name); }
        catch (Exception ex) { return Err(ex.Message, ex.GetType().Name); }
    }

    // ── DLL exports (called from BridgePro.mq5 via #import) ──────────────

    [UnmanagedCallersOnly(EntryPoint = "BridgeStart", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int BridgeStart(int port)
    {
        try
        {
            _port = port > 0 ? port : 8892;
            _listener = new HttpListener();
            _listener.Prefixes.Add($"http://localhost:{_port}/");
            _listener.Start();
            _cts = new CancellationTokenSource();
            _serverTask = Task.Run(() => HandleLoop(_cts.Token));
            return 1;
        }
        catch
        {
            return 0;
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "BridgeStop", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static void BridgeStop()
    {
        try
        {
            _cts?.Cancel();
            if (_listener?.IsListening == true)
            {
                _listener.Stop();
                _listener.Close();
            }
        }
        catch { }
    }

    [UnmanagedCallersOnly(EntryPoint = "BridgePushAccount", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static void BridgePushAccount(double balance, double equity, double margin, double freeMargin, double profit)
    {
        var inv = System.Globalization.CultureInfo.InvariantCulture;
        _state["account"] = "{"
            + "\"balance\":"     + balance.ToString(inv)    + ","
            + "\"equity\":"      + equity.ToString(inv)     + ","
            + "\"margin\":"      + margin.ToString(inv)     + ","
            + "\"free_margin\":" + freeMargin.ToString(inv) + ","
            + "\"profit\":"      + profit.ToString(inv)     + ","
            + "\"ts\":\""        + DateTime.UtcNow.ToString("O") + "\""
            + "}";
    }

    [UnmanagedCallersOnly(EntryPoint = "BridgePushPositions", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static void BridgePushPositions(IntPtr jsonPtr)
    {
        var json = Marshal.PtrToStringUni(jsonPtr);
        if (json != null) _state["positions"] = json;
    }

    [UnmanagedCallersOnly(EntryPoint = "BridgePushOrders", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static void BridgePushOrders(IntPtr jsonPtr)
    {
        var json = Marshal.PtrToStringUni(jsonPtr);
        if (json != null) _state["orders"] = json;
    }

    [UnmanagedCallersOnly(EntryPoint = "BridgePushTerminal", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static void BridgePushTerminal(IntPtr jsonPtr)
    {
        var json = Marshal.PtrToStringUni(jsonPtr);
        if (json != null) _state["terminal"] = json;
    }

    // ── HTTP server ───────────────────────────────────────────────────────

    private static async Task HandleLoop(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested && _listener?.IsListening == true)
        {
            try
            {
                var ctx = await _listener.GetContextAsync();
                _ = Task.Run(() => Dispatch(ctx));
            }
            catch (HttpListenerException) { break; }
            catch (ObjectDisposedException) { break; }
            catch { }
        }
    }

    private static void Dispatch(HttpListenerContext ctx)
    {
        string body = "{}";
        int status = 200;
        try
        {
            string path   = (ctx.Request.Url?.AbsolutePath ?? "/").TrimEnd('/').ToLowerInvariant();
            string method = ctx.Request.HttpMethod;
            if (path == "") path = "/";

            body = (method, path) switch
            {
                ("GET",  "/")                  => HealthJson(),
                ("GET",  "/health")            => HealthJson(),
                ("GET",  "/version")           => VersionJson(),
                ("GET",  "/account")           => _state.TryGetValue("account",   out var a)  ? a  : """{"error":"no account — is BridgePro running?"}""",
                ("GET",  "/positions")         => _state.TryGetValue("positions", out var p)  ? p  : "[]",
                ("GET",  "/orders")            => _state.TryGetValue("orders",    out var o)  ? o  : "[]",
                ("GET",  "/terminal")          => _state.TryGetValue("terminal",  out var tt) ? tt : "{}",
                ("GET",  "/state")             => StateJson(),
                ("GET",  "/bt_results")        => BtResultsJson(ctx.Request),
                ("GET",  "/tester/log_result") => TesterLogResult(),
                ("GET",  "/tester/settings")    => TesterReadSettings(),
                ("GET",  "/tester/status")     => TesterStatus(),
                // POST kept for backward-compat; GET aliases added so Python GET-only clients work
                ("POST", "/tester/configure")  => TesterConfigure(ctx.Request),
                ("GET",  "/tester/configure")  => TesterConfigure(ctx.Request),
                ("POST", "/tester/run")        => TesterRunSync(ctx.Request),
                ("GET",  "/tester/run")        => TesterRunSync(ctx.Request),
                ("POST", "/tester/stop")       => TesterStop(),
                ("GET",  "/tester/stop")       => TesterStop(),
                ("POST", "/tester/show_tab")   => TesterShowTab(ctx.Request),
                ("GET",  "/tester/show_tab")   => TesterShowTab(ctx.Request),
                ("GET",  "/tester/diag")       => TesterDiag(),
                ("GET",  "/tester/win32diag")  => TesterWin32Diag(),
                ("GET",  "/tester/btn_state")    => TesterBtnState(),
                ("GET",  "/tester/list_experts") => TesterListExperts(),
                ("POST", "/tester/switch_expert") => TesterSwitchExpert(ctx.Request),
                ("GET",  "/tester/switch_expert") => TesterSwitchExpert(ctx.Request),
                _ => Fail(ref status, 404, $"unknown: {method} {path}")
            };
        }
        catch (Exception ex)
        {
            status = 500;
            body = Err(ex.Message, ex.GetType().Name);
        }
        finally
        {
            try
            {
                var bytes = Encoding.UTF8.GetBytes(body);
                ctx.Response.StatusCode = status;
                ctx.Response.ContentType = "application/json";
                ctx.Response.ContentLength64 = bytes.Length;
                // CORS — allows browser devtools and any local client to call the bridge
                ctx.Response.Headers["Access-Control-Allow-Origin"]  = "*";
                ctx.Response.Headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS";
                ctx.Response.Headers["Access-Control-Allow-Headers"] = "Content-Type";
                ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
                ctx.Response.OutputStream.Close();
            }
            catch { }
        }
    }

    // ── Endpoint builders ─────────────────────────────────────────────────

    private static string Fail(ref int status, int code, string msg)
    {
        status = code;
        return Err(msg);
    }

    private static string HealthJson() =>
        "{\"status\":\"ok\",\"service\":\"MT5-Interlink\",\"version\":\""
        + VERSION + "\",\"build_timestamp\":\"" + BUILD_TIMESTAMP + "\"}";

    private static string VersionJson() =>
        "{\"service\":\"MT5-Interlink\","
        + "\"version\":\"" + VERSION + "\","
        + "\"build_timestamp\":\"" + BUILD_TIMESTAMP + "\","
        + "\"features\":\"" + BUILD_FEATURES + "\","
        + "\"port\":" + _port + ","
        + "\"runtime\":\".NET NativeAOT\""
        + "}";

    // /state — all cached state in one atomic response
    private static string StateJson()
    {
        _state.TryGetValue("account",   out var acc);
        _state.TryGetValue("positions", out var pos);
        _state.TryGetValue("orders",    out var ord);
        _state.TryGetValue("terminal",  out var term);
        return "{"
            + "\"account\":"   + (acc  ?? "{}")  + ","
            + "\"positions\":" + (pos  ?? "[]")  + ","
            + "\"orders\":"    + (ord  ?? "[]")  + ","
            + "\"terminal\":"  + (term ?? "{}")  + ","
            + "\"ts\":\""      + DateTime.UtcNow.ToString("O") + "\""
            + "}";
    }

    // /bt_results — reads TesterStats JSON files from Common\Files\bt_results\
    // Query params: ?symbol=XAUUSD  ?ea=NoWick  ?sort_by=sharpe|pf|profit|dd
    private static string BtResultsJson(HttpListenerRequest req)
    {
        try
        {
            var btDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "MetaQuotes", "Terminal", "Common", "Files", "bt_results"
            );

            if (!Directory.Exists(btDir))
                return "{\"error\":\"bt_results folder not found — run a backtest first\",\"path\":\""
                       + Escape(btDir) + "\"}";

            var symbolFilter = req.QueryString["symbol"] ?? "";
            var eaFilter     = req.QueryString["ea"]     ?? "";
            var sortBy       = req.QueryString["sort_by"] ?? "sharpe";

            var files = Directory.GetFiles(btDir, "*.json");
            if (files.Length == 0)
                return "{\"count\":0,\"results\":[],\"path\":\"" + Escape(btDir) + "\"}";

            var results = new System.Collections.Generic.List<JsonNode>();

            foreach (var f in files)
            {
                try
                {
                    var txt = File.ReadAllText(f, Encoding.UTF8);
                    var node = JsonNode.Parse(txt);
                    if (node == null) continue;

                    // Apply filters
                    if (!string.IsNullOrEmpty(symbolFilter))
                    {
                        var sym = node["symbol"]?.ToString() ?? "";
                        if (!sym.Contains(symbolFilter, StringComparison.OrdinalIgnoreCase)) continue;
                    }
                    if (!string.IsNullOrEmpty(eaFilter))
                    {
                        var ea = node["ea"]?.ToString() ?? "";
                        if (!ea.Contains(eaFilter, StringComparison.OrdinalIgnoreCase)) continue;
                    }

                    // Inject filename for reference
                    if (node is JsonObject obj)
                        obj["_file"] = Path.GetFileName(f);

                    results.Add(node);
                }
                catch { /* skip malformed files */ }
            }

            // Sort
            IEnumerable<JsonNode> sorted = sortBy switch
            {
                "pf"     => results.OrderByDescending(n => n["profit_factor"]?.GetValue<double?>() ?? 0),
                "profit" => results.OrderByDescending(n => n["net_profit"]?.GetValue<double?>()    ?? 0),
                "dd"     => results.OrderBy(n => n["max_dd_pct"]?.GetValue<double?>()             ?? 999),
                _        => results.OrderByDescending(n => n["sharpe_ratio"]?.GetValue<double?>() ?? 0),
            };

            var sb = new StringBuilder();
            sb.Append("{\"count\":").Append(results.Count).Append(",\"sort_by\":\"").Append(Escape(sortBy)).Append("\",\"results\":[");
            bool first = true;
            foreach (var n in sorted)
            {
                if (!first) sb.Append(',');
                sb.Append(n.ToJsonString());
                first = false;
            }
            sb.Append("]}");
            return sb.ToString();
        }
        catch (Exception ex)
        {
            return Err(ex.Message, ex.GetType().Name);
        }
    }

    // ── Strategy Tester field IDs (MT5 build 5800, stable across brokers) ─
    private const string ID_EXPERT     = "10485";
    private const string ID_SYMBOL     = "10486";
    private const string ID_TIMEFRAME  = "10487";
    private const string ID_DATE_TYPE  = "10123";
    private const string ID_START_DATE = "10550";
    private const string ID_END_DATE   = "10551";
    private const string ID_FORWARD    = "10492";
    private const string ID_DELAYS     = "10488";
    private const string ID_MODELLING  = "10515";
    private const string ID_DEPOSIT    = "10489";
    private const string ID_CURRENCY   = "10559";
    private const string ID_LEVERAGE   = "10473";
    private const string ID_OPTIMIZE   = "10491";
    private const string ID_START_BTN  = "16790";

    // ── Win32 P/Invoke declarations ───────────────────────────────────────

    [DllImport("user32.dll")] private static extern IntPtr GetWindow(IntPtr hWnd, uint uCmd);
    [DllImport("user32.dll")] private static extern int    GetDlgCtrlID(IntPtr hwndCtl);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowTextW(IntPtr hWnd, [Out] StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassNameW(IntPtr hWnd, [Out] StringBuilder lpClassName, int nMaxCount);
    [DllImport("user32.dll")] private static extern IntPtr GetParent(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool IsWindowEnabled(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool EnableWindow(IntPtr hWnd, bool bEnable);
    [DllImport("user32.dll")] private static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr hWnd, uint gaFlags);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hWnd, ref RECT lpRect);
    [DllImport("user32.dll")] private static extern int  GetSystemMetrics(int nIndex);
    [DllImport("user32.dll")] private static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int SendMessage(IntPtr hWnd, uint Msg, IntPtr wParam, StringBuilder lParam);

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    private struct MOUSEINPUT
    {
        public int dx, dy;
        public uint mouseData, dwFlags, time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct KEYBDINPUT
    {
        public ushort wVk;
        public ushort wScan;
        public uint dwFlags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct INPUT
    {
        [FieldOffset(0)]  public uint type;       // 0 = mouse, 1 = keyboard
        [FieldOffset(8)]  public MOUSEINPUT mi;   // 8-byte offset for 64-bit alignment
        [FieldOffset(8)]  public KEYBDINPUT ki;
    }

    private const uint INPUT_MOUSE    = 0;
    private const uint INPUT_KEYBOARD = 1;
    private const uint MOUSEEVENTF_MOVE     = 0x0001;
    private const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    private const uint MOUSEEVENTF_LEFTUP   = 0x0004;
    private const uint MOUSEEVENTF_ABSOLUTE = 0x8000;
    private const uint KEYEVENTF_KEYUP    = 0x0002;
    private const uint KEYEVENTF_UNICODE  = 0x0004;
    private const uint KEYEVENTF_SCANCODE = 0x0008;

    // Physical click via SendInput — works regardless of focus/foreground state.
    private static void PhysicalClick(IntPtr hwnd, IntPtr mt5Root)
    {
        var r = new RECT();
        if (!GetWindowRect(hwnd, ref r)) return;
        int cx = (r.Left + r.Right)  / 2;
        int cy = (r.Top  + r.Bottom) / 2;
        // Use virtual desktop dimensions so coords are correct on multi-monitor setups.
        int vx = GetSystemMetrics(76);  // SM_XVIRTUALSCREEN
        int vy = GetSystemMetrics(77);  // SM_YVIRTUALSCREEN
        int vw = GetSystemMetrics(78);  // SM_CXVIRTUALSCREEN
        int vh = GetSystemMetrics(79);  // SM_CYVIRTUALSCREEN
        if (vw <= 0 || vh <= 0) return;
        int ax = (cx - vx) * 65535 / vw;
        int ay = (cy - vy) * 65535 / vh;

        SetForegroundWindow(mt5Root);
        Thread.Sleep(200);

        var inputs = new INPUT[3];
        inputs[0].type = 0; inputs[0].mi = new MOUSEINPUT { dx = ax, dy = ay, dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE };
        inputs[1].type = 0; inputs[1].mi = new MOUSEINPUT { dx = ax, dy = ay, dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTDOWN };
        inputs[2].type = 0; inputs[2].mi = new MOUSEINPUT { dx = ax, dy = ay, dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTUP };
        SendInput(3, inputs, Marshal.SizeOf(typeof(INPUT)));
    }

    // Non-blocking fire-and-forget (for BM_CLICK, CB_SETCURSEL after we know the index)
    [DllImport("user32.dll", EntryPoint = "PostMessageW")]
    private static extern bool PostMessageInt(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

    // Timed SendMessage — SMTO_ABORTIFHUNG ensures we never block past the timeout
    [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "SendMessageTimeoutW")]
    private static extern IntPtr SendMsgTStr(IntPtr hWnd, uint Msg, IntPtr wParam, string lParam,
        uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
    [DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW")]
    private static extern IntPtr SendMsgTInt(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam,
        uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
    [DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW")]
    private static extern IntPtr SendMsgTSysTime(IntPtr hWnd, uint Msg, IntPtr wParam, ref SYSTEMTIME lParam,
        uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);

    // Tab-specific SendMessage (still timed via the helpers below)
    [DllImport("user32.dll", EntryPoint = "SendMessageW")]
    private static extern IntPtr SendMessageTcItem(IntPtr hWnd, uint Msg, IntPtr wParam, ref TCITEMW lParam);
    [DllImport("user32.dll", EntryPoint = "PostMessageW")]
    private static extern bool PostMessageNMHDR(IntPtr hWnd, uint Msg, IntPtr wParam, ref NMHDR lParam);

    [StructLayout(LayoutKind.Sequential)]
    private struct SYSTEMTIME
    {
        public ushort wYear, wMonth, wDayOfWeek, wDay, wHour, wMinute, wSecond, wMilliseconds;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct TCITEMW
    {
        public uint mask, dwState, dwStateMask;
        public IntPtr pszText;
        public int cchTextMax, iImage;
        public IntPtr lParam;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct NMHDR
    {
        public IntPtr hwndFrom, idFrom;
        public int code;
    }

    private const uint GW_CHILD        = 5;
    private const uint GW_HWNDNEXT     = 2;
    private const uint WM_SETTEXT      = 0x000C;
    private const uint WM_LBUTTONDOWN  = 0x0201;
    private const uint WM_LBUTTONUP    = 0x0202;
    private const uint MK_LBUTTON      = 0x0001;
    private const uint WM_COMMAND      = 0x0111;
    private const uint BN_CLICKED      = 0x0000;
    private const uint BM_CLICK        = 0x00F5;
    private const uint CB_SHOWDROPDOWN = 0x014F;
    private const uint CB_FINDSTRING      = 0x014C;
    private const uint CB_FINDSTRINGEXACT = 0x0158;
    private const uint CB_SETCURSEL       = 0x014E;
    private const uint CB_GETCOUNT        = 0x0146;
    private const uint CB_GETLBTEXT       = 0x0148;
    private const uint CB_GETLBTEXTLEN    = 0x0149;
    private const int  CBN_SELCHANGE      = 1;
    private const uint DTM_SETSYSTEMTIME = 0x1002;
    private const int  CB_ERR          = -1;
    private const uint TCM_FIRST       = 0x1300;
    private const uint TCM_SETCURSEL   = TCM_FIRST + 12;
    private const uint TCM_GETITEMCOUNT= TCM_FIRST + 4;
    private const uint TCM_GETITEMW    = TCM_FIRST + 60;
    private const uint TCIF_TEXT       = 0x0001;
    private const uint WM_NOTIFY       = 0x004E;
    private const int  TCN_SELCHANGE   = -551;
    private const uint SMTO_ABORTIFHUNG = 0x0002;
    private const uint MSG_TIMEOUT_MS   = 3000;

    // ── Timed SendMessage wrappers — cap every inter-thread message at 3 s ─

    private static long SendStr(IntPtr h, uint msg, int wp, string lp)
    {
        SendMsgTStr(h, msg, new IntPtr(wp), lp, SMTO_ABORTIFHUNG, MSG_TIMEOUT_MS, out var r);
        return (long)r;
    }
    private static long SendInt(IntPtr h, uint msg, int wp, int lp)
    {
        SendMsgTInt(h, msg, new IntPtr(wp), new IntPtr(lp), SMTO_ABORTIFHUNG, MSG_TIMEOUT_MS, out var r);
        return (long)r;
    }
    private static void SendSysTime(IntPtr h, ref SYSTEMTIME st)
    {
        SendMsgTSysTime(h, DTM_SETSYSTEMTIME, IntPtr.Zero, ref st, SMTO_ABORTIFHUNG, MSG_TIMEOUT_MS, out _);
    }

    // ── Win32 helpers ─────────────────────────────────────────────────────

    private static IntPtr FindMt5Hwnd()
    {
        try
        {
            var proc = System.Diagnostics.Process.GetProcessesByName("terminal64")
                .FirstOrDefault(p => p.MainWindowHandle != IntPtr.Zero);
            return proc?.MainWindowHandle ?? IntPtr.Zero;
        }
        catch { return IntPtr.Zero; }
    }

    // Narrow the search root to the Strategy Tester panel (the subtree that owns
    // the Start/Stop button). Avoids scanning ALL charts + toolbars — a big speedup.
    private static IntPtr FindTesterHwnd(IntPtr mt5)
    {
        if (mt5 == IntPtr.Zero) return IntPtr.Zero;
        int btnId = int.Parse(ID_START_BTN);
        // Check immediate children first
        var child = GetWindow(mt5, GW_CHILD);
        while (child != IntPtr.Zero)
        {
            if (FindDescendantById(child, btnId) != IntPtr.Zero) return child;
            child = GetWindow(child, GW_HWNDNEXT);
        }
        // One level deeper
        child = GetWindow(mt5, GW_CHILD);
        while (child != IntPtr.Zero)
        {
            var gc = GetWindow(child, GW_CHILD);
            while (gc != IntPtr.Zero)
            {
                if (FindDescendantById(gc, btnId) != IntPtr.Zero) return gc;
                gc = GetWindow(gc, GW_HWNDNEXT);
            }
            child = GetWindow(child, GW_HWNDNEXT);
        }
        return mt5; // last resort — fall back to full tree
    }

    private static IntPtr FindDescendantById(IntPtr root, int id)
    {
        if (root == IntPtr.Zero) return IntPtr.Zero;
        var stack = new System.Collections.Generic.Stack<IntPtr>();
        stack.Push(root);
        while (stack.Count > 0)
        {
            var current = stack.Pop();
            var child = GetWindow(current, GW_CHILD);
            while (child != IntPtr.Zero)
            {
                if (GetDlgCtrlID(child) == id) return child;
                stack.Push(child);
                child = GetWindow(child, GW_HWNDNEXT);
            }
        }
        return IntPtr.Zero;
    }

    // Dumps up to maxDepth levels of the window tree — used by /tester/win32diag
    private static string Win32DiagDump(IntPtr root, int maxDepth = 5)
    {
        var sb = new StringBuilder();
        sb.Append("[");
        var classBuf = new StringBuilder(128);
        var textBuf  = new StringBuilder(64);
        bool first = true;
        void Walk(IntPtr h, int depth)
        {
            if (depth > maxDepth || h == IntPtr.Zero) return;
            var child = GetWindow(h, GW_CHILD);
            while (child != IntPtr.Zero)
            {
                int cid = GetDlgCtrlID(child);
                classBuf.Clear(); GetClassNameW(child, classBuf, 128);
                textBuf.Clear();  GetWindowTextW(child, textBuf, 64);
                if (!first) sb.Append(',');
                sb.Append("{\"id\":").Append(cid)
                  .Append(",\"d\":").Append(depth)
                  .Append(",\"cls\":\"").Append(Escape(classBuf.ToString())).Append('"')
                  .Append(",\"txt\":\"").Append(Escape(textBuf.ToString())).Append('"')
                  .Append('}');
                first = false;
                Walk(child, depth + 1);
                child = GetWindow(child, GW_HWNDNEXT);
            }
        }
        Walk(root, 1);
        sb.Append("]");
        return sb.ToString();
    }

    private static System.Collections.Generic.List<IntPtr> FindAllByClass(IntPtr root, string className)
    {
        var result = new System.Collections.Generic.List<IntPtr>();
        if (root == IntPtr.Zero) return result;
        var stack = new System.Collections.Generic.Stack<IntPtr>();
        stack.Push(root);
        var sb = new StringBuilder(64);
        while (stack.Count > 0)
        {
            var current = stack.Pop();
            var child = GetWindow(current, GW_CHILD);
            while (child != IntPtr.Zero)
            {
                sb.Clear(); GetClassNameW(child, sb, 64);
                if (sb.ToString() == className) result.Add(child);
                stack.Push(child);
                child = GetWindow(child, GW_HWNDNEXT);
            }
        }
        return result;
    }

    private static bool SetComboValueWin32(IntPtr root, int id, string value)
    {
        var h = FindDescendantById(root, id);
        if (h == IntPtr.Zero) return false;

        // Exact match first, then prefix fallback
        var idx = SendStr(h, CB_FINDSTRINGEXACT, -1, value);
        if (idx == CB_ERR)
            idx = SendStr(h, CB_FINDSTRING, -1, value);

        if (idx != CB_ERR)
        {
            SendInt(h, CB_SETCURSEL, (int)idx, 0);
            // Notify parent via WM_COMMAND/CBN_SELCHANGE so MT5 processes the new selection
            var parent = GetParent(h);
            int ctlId  = GetDlgCtrlID(h);
            PostMessageInt(parent, WM_COMMAND,
                new IntPtr((CBN_SELCHANGE << 16) | (ctlId & 0xFFFF)), h);
            Thread.Sleep(150);
            return true;
        }
        // EA not in combo list yet — write to edit field as fallback
        SendStr(h, WM_SETTEXT, 0, value);
        return true;
    }

    // Returns all items currently listed in the Expert combo box.
    private static System.Collections.Generic.List<string> GetComboItems(IntPtr root, int id)
    {
        var items = new System.Collections.Generic.List<string>();
        var h = FindDescendantById(root, id);
        if (h == IntPtr.Zero) return items;
        int count = (int)SendInt(h, CB_GETCOUNT, 0, 0);
        for (int i = 0; i < count; i++)
        {
            int len = (int)SendInt(h, CB_GETLBTEXTLEN, i, 0);
            if (len <= 0) continue;
            var sb = new StringBuilder(len + 2);
            SendMessage(h, CB_GETLBTEXT, new IntPtr(i), sb);
            items.Add(sb.ToString());
        }
        return items;
    }

    // ─── Owner-drawn expert combo handling (AOT-safe, no UIAutomation) ────────
    // MT5's Expert combo is owner-drawn — Win32 CB_GETCOUNT/CB_FINDSTRINGEXACT
    // don't see its items, and UIA patterns crash under NativeAOT.
    // We use two AOT-safe strategies:
    //   • Listing: filesystem scan of MQL5\Experts\*.ex5 (authoritative source)
    //   • Switching: keystroke simulation via PostMessage into the combo's edit child

    private const uint WM_KEYDOWN  = 0x0100;
    private const uint WM_KEYUP    = 0x0101;
    private const uint WM_CHAR     = 0x0102;
    private const int  VK_RETURN   = 0x0D;
    private const int  VK_DELETE   = 0x2E;
    private const int  VK_HOME     = 0x24;

    [DllImport("user32.dll")] private static extern IntPtr SetFocus(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr FindWindowExW(IntPtr hwndParent, IntPtr hwndChildAfter,
                                               string? lpszClass, string? lpszWindow);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr ProcessId);
    [DllImport("user32.dll")] private static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    private const int SW_RESTORE = 9;

    private static IntPtr FindChildByClass(IntPtr parent, string className)
        => FindWindowExW(parent, IntPtr.Zero, className, null);

    // Attach our thread's input to the target's thread (and the current
    // foreground thread, to bypass the foreground-lock restriction), then
    // raise the window. Caller is responsible for detaching via DetachThreads.
    private struct AttachState
    {
        public uint OurThread, FgThread, TgtThread;
        public bool A1, A2;
    }

    private static (bool ok, AttachState s) AttachAndForeground(IntPtr targetHwnd)
    {
        var s = new AttachState();
        var fg = GetForegroundWindow();
        s.OurThread = GetCurrentThreadId();
        s.FgThread  = fg != IntPtr.Zero ? GetWindowThreadProcessId(fg, IntPtr.Zero) : 0;
        s.TgtThread = GetWindowThreadProcessId(targetHwnd, IntPtr.Zero);
        if (s.FgThread != 0 && s.FgThread != s.OurThread)
            s.A1 = AttachThreadInput(s.OurThread, s.FgThread, true);
        if (s.TgtThread != 0 && s.TgtThread != s.OurThread && s.TgtThread != s.FgThread)
            s.A2 = AttachThreadInput(s.OurThread, s.TgtThread, true);
        ShowWindow(targetHwnd, SW_RESTORE);
        BringWindowToTop(targetHwnd);
        SetForegroundWindow(targetHwnd);
        Thread.Sleep(80);
        return (GetForegroundWindow() == targetHwnd, s);
    }

    private static void DetachThreads(AttachState s)
    {
        if (s.A1) AttachThreadInput(s.OurThread, s.FgThread, false);
        if (s.A2) AttachThreadInput(s.OurThread, s.TgtThread, false);
    }

    // Filesystem listing of installed EAs — independent of UI state.
    private static System.Collections.Generic.List<string> ListEx5Files()
    {
        var result = new System.Collections.Generic.List<string>();
        try
        {
            var appdata = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            var termRoot = Path.Combine(appdata, "MetaQuotes", "Terminal");
            if (!Directory.Exists(termRoot)) return result;
            foreach (var d in Directory.GetDirectories(termRoot))
            {
                var candidate = Path.Combine(d, "MQL5", "Experts");
                if (!Directory.Exists(candidate)) continue;
                var files = Directory.GetFiles(candidate, "*.ex5", SearchOption.AllDirectories);
                foreach (var f in files) result.Add(Path.GetFileName(f));
            }
            result.Sort();
        }
        catch { }
        return result;
    }

    private static string TesterListExperts()
    {
        try
        {
            var mt5    = FindMt5Hwnd();
            var tester = FindTesterHwnd(mt5);
            string cur = "";
            if (tester != IntPtr.Zero) cur = ReadFieldText(tester, ID_EXPERT);
            var items = ListEx5Files();
            var sb = new StringBuilder();
            sb.Append("{\"current\":\"").Append(Escape(cur))
              .Append("\",\"count\":").Append(items.Count).Append(",\"items\":[");
            for (int i = 0; i < items.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append('"').Append(Escape(items[i])).Append('"');
            }
            sb.Append("]}");
            return sb.ToString();
        }
        catch (Exception ex) { return Err(ex.Message, ex.GetType().Name); }
    }

    // Send a single Unicode char via SendInput (real keyboard injection).
    private static void SendUnicodeChar(char c)
    {
        var inputs = new INPUT[2];
        inputs[0].type = INPUT_KEYBOARD;
        inputs[0].ki = new KEYBDINPUT { wVk = 0, wScan = c, dwFlags = KEYEVENTF_UNICODE };
        inputs[1].type = INPUT_KEYBOARD;
        inputs[1].ki = new KEYBDINPUT { wVk = 0, wScan = c, dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP };
        SendInput(2, inputs, Marshal.SizeOf<INPUT>());
    }

    private static void SendVKey(ushort vk)
    {
        var inputs = new INPUT[2];
        inputs[0].type = INPUT_KEYBOARD;
        inputs[0].ki = new KEYBDINPUT { wVk = vk };
        inputs[1].type = INPUT_KEYBOARD;
        inputs[1].ki = new KEYBDINPUT { wVk = vk, dwFlags = KEYEVENTF_KEYUP };
        SendInput(2, inputs, Marshal.SizeOf<INPUT>());
    }

    // Switch the Expert by simulating real keyboard input. For CBS_DROPDOWNLIST
    // combos, MT5 jumps to the first item starting with the typed character.
    // We open the dropdown (Alt+Down), type the EA name as a Unicode stream,
    // then confirm with Enter.
    private static (bool ok, string actual, string method) SwitchExpertCore(string target)
    {
        var mt5 = FindMt5Hwnd();
        if (mt5 == IntPtr.Zero) return (false, "", "no_mt5");
        var tester = FindTesterHwnd(mt5);
        if (tester == IntPtr.Zero) return (false, "", "no_tester");
        var combo = FindDescendantById(tester, int.Parse(ID_EXPERT));
        if (combo == IntPtr.Zero) return (false, "", "no_combo");

        // SendInput is system-wide — must keep MT5 foreground AND keep our thread
        // attached to MT5's input queue so SetFocus works cross-process.
        var (fgOk, attachState) = AttachAndForeground(mt5);
        try
        {
            if (!fgOk)
                return (false, ReadFieldText(tester, ID_EXPERT), "no_foreground");

            // Now SetFocus works because we're attached to MT5's input thread
            SetFocus(combo);
            Thread.Sleep(80);

            if (GetForegroundWindow() != mt5)
                return (false, ReadFieldText(tester, ID_EXPERT), "foreground_lost");

            // Open dropdown (F4)
            SendVKey(0x73);
            Thread.Sleep(250);
            if (GetForegroundWindow() != mt5)
                return (false, ReadFieldText(tester, ID_EXPERT), "foreground_lost_after_f4");

            // Type each character. CBS_DROPDOWNLIST + owner-drawn → incremental search.
            foreach (char c in target)
            {
                SendUnicodeChar(c);
                Thread.Sleep(20);
            }
            Thread.Sleep(250);

            // Enter to commit
            SendVKey(0x0D);
            Thread.Sleep(350);

            var actual = ReadFieldText(tester, ID_EXPERT);
            return (string.Equals(actual, target, StringComparison.OrdinalIgnoreCase),
                    actual, "sendinput");
        }
        finally { DetachThreads(attachState); }
    }

    private static string TesterSwitchExpert(HttpListenerRequest req)
    {
        string target;
        try
        {
            if (req.HttpMethod == "POST")
            {
                using var sr = new StreamReader(req.InputStream, req.ContentEncoding);
                var body = JsonNode.Parse(sr.ReadToEnd())?.AsObject();
                target = body?["expert"]?.ToString() ?? "";
            }
            else
            {
                target = req.QueryString["expert"] ?? "";
            }
        }
        catch (Exception ex) { return Err("bad request: " + ex.Message); }
        if (string.IsNullOrEmpty(target)) return Err("missing 'expert' field");

        try
        {
            var (ok, actual, method) = SwitchExpertCore(target);
            var sb = new StringBuilder();
            sb.Append("{\"ok\":").Append(ok ? "true" : "false");
            sb.Append(",\"requested\":\"").Append(Escape(target)).Append("\"");
            sb.Append(",\"actual\":\"").Append(Escape(actual)).Append("\"");
            sb.Append(",\"method\":\"").Append(method).Append("\"");
            sb.Append("}");
            return sb.ToString();
        }
        catch (Exception ex) { return Err(ex.Message, ex.GetType().Name); }
    }

    private static bool SetDateFieldWin32(IntPtr root, int id, DateTime date)
    {
        var h = FindDescendantById(root, id);
        if (h == IntPtr.Zero) return false;
        var st = new SYSTEMTIME { wYear = (ushort)date.Year, wMonth = (ushort)date.Month, wDay = (ushort)date.Day };
        SendSysTime(h, ref st);
        return true;
    }

    private static bool ClickButtonWin32(IntPtr root, int id)
    {
        var h = FindDescendantById(root, id);
        if (h == IntPtr.Zero) return false;
        PostMessageInt(h, BM_CLICK, IntPtr.Zero, IntPtr.Zero);
        return true;
    }

    private static string GetWindowTextWin32(IntPtr hwnd)
    {
        var sb = new StringBuilder(256);
        GetWindowTextW(hwnd, sb, 256);
        return sb.ToString();
    }

    private static string ReadFieldText(IntPtr root, string id)
    {
        var h = FindDescendantById(root, int.Parse(id));
        if (h == IntPtr.Zero) return "";
        return GetWindowTextWin32(h);
    }

    private static int GetTabIndexByName(IntPtr tabHwnd, string tabName)
    {
        var count = (int)SendInt(tabHwnd, TCM_GETITEMCOUNT, 0, 0);
        if (count <= 0) return -1;
        var buf = Marshal.AllocHGlobal(256 * 2);
        try
        {
            for (int i = 0; i < count; i++)
            {
                var item = new TCITEMW { mask = TCIF_TEXT, pszText = buf, cchTextMax = 256 };
                SendMessageTcItem(tabHwnd, TCM_GETITEMW, new IntPtr(i), ref item);
                var text = Marshal.PtrToStringUni(buf) ?? "";
                if (string.Equals(text, tabName, StringComparison.OrdinalIgnoreCase)) return i;
            }
        }
        finally { Marshal.FreeHGlobal(buf); }
        return -1;
    }

    private static bool ShowTesterTab(IntPtr mt5, string tabName)
    {
        var tester = FindTesterHwnd(mt5);
        foreach (var tabHwnd in FindAllByClass(tester, "SysTabControl32"))
        {
            int idx = GetTabIndexByName(tabHwnd, tabName);
            if (idx >= 0)
            {
                SendInt(tabHwnd, TCM_SETCURSEL, idx, 0);
                var parent = GetParent(tabHwnd);
                if (parent != IntPtr.Zero)
                {
                    var ctrlId = GetDlgCtrlID(tabHwnd);
                    var nm = new NMHDR { hwndFrom = tabHwnd, idFrom = new IntPtr(ctrlId), code = TCN_SELCHANGE };
                    PostMessageNMHDR(parent, WM_NOTIFY, new IntPtr(ctrlId), ref nm);
                }
                return true;
            }
        }
        return false;
    }

    // ── UIAutomation diagnostic (find MT5 window for inspection) ─────────

    private static AutomationElement? FindMt5Window()
    {
        try
        {
            var proc = System.Diagnostics.Process.GetProcessesByName("terminal64")
                .FirstOrDefault(p => p.MainWindowHandle != IntPtr.Zero);
            if (proc != null && proc.MainWindowHandle != IntPtr.Zero)
            {
                var el = AutomationElement.FromHandle(proc.MainWindowHandle);
                if (el != null) return el;
            }
        }
        catch { }
        var root = AutomationElement.RootElement;
        return root.FindFirst(TreeScope.Children,
            new PropertyCondition(AutomationElement.ClassNameProperty, "MetaQuotes::MetaTrader::5.00"))
            ?? root.FindFirst(TreeScope.Children,
                new PropertyCondition(AutomationElement.ClassNameProperty, "MetaQuotes::MetaTrader::5::Wnd"));
    }

    private static string TesterDiag() => RunOnStaSafe(() =>
    {
        try
        {
            var mt5 = FindMt5Window();
            if (mt5 == null) return """{"error":"MT5 main window not found"}""";
            var sb = new StringBuilder();
            sb.Append("{\"mt5\":{\"name\":\"").Append(Escape(mt5.Current.Name))
              .Append("\",\"class\":\"").Append(Escape(mt5.Current.ClassName))
              .Append("\"},\"children\":[");
            var walker = TreeWalker.ControlViewWalker;
            var child = walker.GetFirstChild(mt5);
            bool first = true; int count = 0;
            while (child != null && count < 50)
            {
                if (!first) sb.Append(',');
                sb.Append("{\"name\":\"").Append(Escape(child.Current.Name))
                  .Append("\",\"class\":\"").Append(Escape(child.Current.ClassName))
                  .Append("\",\"type\":\"").Append(child.Current.ControlType.ProgrammaticName)
                  .Append("\",\"id\":\"").Append(Escape(child.Current.AutomationId)).Append("\"}");
                first = false; child = walker.GetNextSibling(child); count++;
            }
            sb.Append("]}");
            return sb.ToString();
        }
        catch (Exception ex) { return Err(ex.Message); }
    });

    // ── /tester/log_result — parse the MT5 agent tester log for the most recent run ─
    // Reads from MetaQuotes\Tester\<TID>\Agent-127.0.0.1-3000\logs\<today>.log
    // Returns key stats without requiring TesterStats.mqh in the EA.
    private static string TesterLogResult()
    {
        try
        {
            var tid = System.Environment.GetEnvironmentVariable("MT5_TERMINAL_ID")
                      ?? "608AB61EFF9C7B3585EC08B8CF6800E3";
            var appdata = System.Environment.GetFolderPath(System.Environment.SpecialFolder.ApplicationData);
            var logDir = System.IO.Path.Combine(appdata, "MetaQuotes", "Tester", tid,
                                                "Agent-127.0.0.1-3000", "logs");
            if (!System.IO.Directory.Exists(logDir))
                return "{\"error\":\"agent log dir not found\",\"path\":\"" + Escape(logDir) + "\"}";

            var logFiles = System.IO.Directory.GetFiles(logDir, "*.log")
                           .OrderByDescending(f => f).ToArray();
            if (logFiles.Length == 0) return "{\"error\":\"no agent log files found\"}";

            var logText = System.IO.File.ReadAllText(logFiles[0],
                          System.Text.Encoding.Unicode);

            // Extract key values
            double initialDeposit = 0, finalBalance = 0;
            int wins = 0, losses = 0;
            double grossProfit = 0, grossLoss = 0;
            double peakBalance = 0, maxDdAbs = 0;
            string ea = "", symbol = "", tf = "", startDate = "", endDate = "";
            int totalBars = 0; double testSeconds = 0;

            foreach (var raw in logText.Split('\n'))
            {
                var line = raw.Trim();
                System.Text.RegularExpressions.Match m;

                if ((m = System.Text.RegularExpressions.Regex.Match(line,
                    @"initial deposit ([0-9.]+) USD")).Success)
                    initialDeposit = double.Parse(m.Groups[1].Value, System.Globalization.CultureInfo.InvariantCulture);

                if ((m = System.Text.RegularExpressions.Regex.Match(line,
                    @"final balance ([0-9.]+) USD")).Success)
                    finalBalance = double.Parse(m.Groups[1].Value, System.Globalization.CultureInfo.InvariantCulture);

                if ((m = System.Text.RegularExpressions.Regex.Match(line,
                    @"testing of (.+?) from (\d{4}\.\d{2}\.\d{2}) .+ to (\d{4}\.\d{2}\.\d{2})")).Success)
                { ea = m.Groups[1].Value; startDate = m.Groups[2].Value; endDate = m.Groups[3].Value; }

                if ((m = System.Text.RegularExpressions.Regex.Match(line,
                    @"([\w]+),([\w]+): .* (\d+) bars generated.*Test passed in 0:00:([0-9.]+)")).Success)
                { symbol = m.Groups[1].Value; tf = m.Groups[2].Value;
                  totalBars = int.Parse(m.Groups[3].Value);
                  testSeconds = double.Parse(m.Groups[4].Value, System.Globalization.CultureInfo.InvariantCulture); }

                if (line.Contains("take profit triggered")) wins++;
                if (line.Contains("stop loss triggered"))  losses++;

                // TP trade PnL: "take profit triggered #N dir lots ENTRY sl: S tp: T [#N dir lots at EXIT]"
                if ((m = System.Text.RegularExpressions.Regex.Match(line,
                    @"take profit triggered #\d+ (buy|sell) ([0-9.]+) \w+ ([0-9.]+) sl: [0-9.]+ tp: [0-9.]+ \[#\d+ \w+ [0-9.]+ \w+ at ([0-9.]+)\]")).Success)
                {
                    double lots = double.Parse(m.Groups[2].Value, System.Globalization.CultureInfo.InvariantCulture);
                    double entry = double.Parse(m.Groups[3].Value, System.Globalization.CultureInfo.InvariantCulture);
                    double exit  = double.Parse(m.Groups[4].Value, System.Globalization.CultureInfo.InvariantCulture);
                    double pnl   = m.Groups[1].Value == "buy" ? (exit - entry) * lots * 100
                                                              : (entry - exit) * lots * 100;
                    grossProfit += pnl;
                }
                if ((m = System.Text.RegularExpressions.Regex.Match(line,
                    @"stop loss triggered #\d+ (buy|sell) ([0-9.]+) \w+ ([0-9.]+) sl: [0-9.]+ tp: [0-9.]+ \[#\d+ \w+ [0-9.]+ \w+ at ([0-9.]+)\]")).Success)
                {
                    double lots = double.Parse(m.Groups[2].Value, System.Globalization.CultureInfo.InvariantCulture);
                    double entry = double.Parse(m.Groups[3].Value, System.Globalization.CultureInfo.InvariantCulture);
                    double exit  = double.Parse(m.Groups[4].Value, System.Globalization.CultureInfo.InvariantCulture);
                    double pnl   = m.Groups[1].Value == "buy" ? Math.Abs((exit - entry) * lots * 100)
                                                              : Math.Abs((entry - exit) * lots * 100);
                    grossLoss += pnl;
                }

                // Track peak for drawdown
                if ((m = System.Text.RegularExpressions.Regex.Match(line,
                    @"balance reset to ([0-9.]+)")).Success)
                {
                    double b = double.Parse(m.Groups[1].Value, System.Globalization.CultureInfo.InvariantCulture);
                    if (b > peakBalance) peakBalance = b;
                    double dd = peakBalance - b;
                    if (dd > maxDdAbs) maxDdAbs = dd;
                }
            }
            if (finalBalance > peakBalance) peakBalance = finalBalance;

            double netProfit = finalBalance - initialDeposit;
            double returnPct = initialDeposit > 0 ? netProfit / initialDeposit * 100 : 0;
            int totalTrades = wins + losses;
            double winRate   = totalTrades > 0 ? (double)wins / totalTrades * 100 : 0;
            double pf        = grossLoss > 0 ? grossProfit / grossLoss : 0;
            double avgWin    = wins   > 0 ? grossProfit / wins   : 0;
            double avgLoss   = losses > 0 ? grossLoss   / losses : 0;
            double maxDdPct  = peakBalance > 0 ? maxDdAbs / peakBalance * 100 : 0;

            var inv = System.Globalization.CultureInfo.InvariantCulture;
            return "{" +
                "\"ea\":\""             + Escape(ea)                              + "\"," +
                "\"symbol\":\""         + Escape(symbol)                          + "\"," +
                "\"timeframe\":\""      + Escape(tf)                              + "\"," +
                "\"start_date\":\""     + Escape(startDate)                       + "\"," +
                "\"end_date\":\""       + Escape(endDate)                         + "\"," +
                "\"initial_deposit\":"  + initialDeposit.ToString("F2", inv)      + "," +
                "\"final_balance\":"    + finalBalance  .ToString("F2", inv)      + "," +
                "\"net_profit\":"       + netProfit     .ToString("F2", inv)      + "," +
                "\"return_pct\":"       + returnPct     .ToString("F4", inv)      + "," +
                "\"gross_profit\":"     + grossProfit   .ToString("F2", inv)      + "," +
                "\"gross_loss\":"       + grossLoss     .ToString("F2", inv)      + "," +
                "\"profit_factor\":"    + pf            .ToString("F4", inv)      + "," +
                "\"total_trades\":"     + totalTrades                             + "," +
                "\"wins\":"             + wins                                    + "," +
                "\"losses\":"           + losses                                  + "," +
                "\"win_rate_pct\":"     + winRate       .ToString("F2", inv)      + "," +
                "\"avg_win\":"          + avgWin        .ToString("F2", inv)      + "," +
                "\"avg_loss\":"         + avgLoss       .ToString("F2", inv)      + "," +
                "\"peak_balance\":"     + peakBalance   .ToString("F2", inv)      + "," +
                "\"max_dd_abs\":"       + maxDdAbs      .ToString("F2", inv)      + "," +
                "\"max_dd_pct\":"       + maxDdPct      .ToString("F4", inv)      + "," +
                "\"total_bars\":"       + totalBars                               + "," +
                "\"test_seconds\":"     + testSeconds   .ToString("F3", inv)      +
                "}";
        }
        catch (Exception ex) { return Err(ex.Message, ex.GetType().Name); }
    }

    // ── /tester/settings — read every field currently showing in the tester ─
    // Uses GetWindowTextW on each control (works for ComboBox + DateTimePicker).
    // DateTimePicker returns its display text e.g. "2020.05.01".
    private static string TesterReadSettings()
    {
        var mt5 = FindMt5Hwnd();
        if (mt5 == IntPtr.Zero) return Err("MT5 main window not found");
        var tester = FindTesterHwnd(mt5);

        string ReadField(string id)
        {
            var h = FindDescendantById(tester, int.Parse(id));
            if (h == IntPtr.Zero) return "(not found)";
            var sb = new StringBuilder(512);
            GetWindowTextW(h, sb, 512);
            return sb.ToString();
        }

        var btnH  = FindDescendantById(tester, int.Parse(ID_START_BTN));
        var btnTxt = btnH != IntPtr.Zero ? GetWindowTextWin32(btnH) : "unknown";

        var s = new StringBuilder();
        s.Append("{");
        s.Append("\"running\":")        .Append(btnTxt == "Stop" ? "true" : "false").Append(',');
        s.Append("\"button_state\":\"") .Append(Escape(btnTxt)).Append("\",");
        s.Append("\"expert\":\"")       .Append(Escape(ReadField(ID_EXPERT))).Append("\",");
        s.Append("\"symbol\":\"")       .Append(Escape(ReadField(ID_SYMBOL))).Append("\",");
        s.Append("\"timeframe\":\"")    .Append(Escape(ReadField(ID_TIMEFRAME))).Append("\",");
        s.Append("\"date_type\":\"")    .Append(Escape(ReadField(ID_DATE_TYPE))).Append("\",");
        s.Append("\"start_date\":\"")   .Append(Escape(ReadField(ID_START_DATE))).Append("\",");
        s.Append("\"end_date\":\"")     .Append(Escape(ReadField(ID_END_DATE))).Append("\",");
        s.Append("\"forward\":\"")      .Append(Escape(ReadField(ID_FORWARD))).Append("\",");
        s.Append("\"delays\":\"")       .Append(Escape(ReadField(ID_DELAYS))).Append("\",");
        s.Append("\"modelling\":\"")    .Append(Escape(ReadField(ID_MODELLING))).Append("\",");
        s.Append("\"deposit\":\"")      .Append(Escape(ReadField(ID_DEPOSIT))).Append("\",");
        s.Append("\"currency\":\"")     .Append(Escape(ReadField(ID_CURRENCY))).Append("\",");
        s.Append("\"leverage\":\"")     .Append(Escape(ReadField(ID_LEVERAGE))).Append("\",");
        s.Append("\"optimization\":\"") .Append(Escape(ReadField(ID_OPTIMIZE))).Append("\"");
        s.Append("}");
        return s.ToString();
    }

    // ── /tester/status — instant non-blocking check (is a test running?) ──
    private static string TesterStatus()
    {
        var mt5 = FindMt5Hwnd();
        if (mt5 == IntPtr.Zero)
            return "{\"running\":false,\"button_state\":\"unknown\",\"error\":\"MT5 window not found\"}";
        var tester = FindTesterHwnd(mt5);
        var btn    = FindDescendantById(tester, int.Parse(ID_START_BTN));
        if (btn == IntPtr.Zero)
            return "{\"running\":false,\"button_state\":\"unknown\",\"error\":\"Start button not found\"}";
        var text = GetWindowTextWin32(btn);
        bool running = text == "Stop";
        return "{\"running\":" + (running ? "true" : "false")
             + ",\"button_state\":\"" + Escape(text) + "\"}";
    }

    // ── /tester/stop — click Stop button to abort a running test ─────────
    private static string TesterStop()
    {
        var mt5 = FindMt5Hwnd();
        if (mt5 == IntPtr.Zero) return Err("MT5 main window not found");
        var tester = FindTesterHwnd(mt5);
        var btn    = FindDescendantById(tester, int.Parse(ID_START_BTN));
        if (btn == IntPtr.Zero) return Err("Start/Stop button not found");
        var text = GetWindowTextWin32(btn);
        if (text != "Stop")
            return "{\"status\":\"not_running\",\"button_state\":\"" + Escape(text) + "\"}";
        PostMessageInt(btn, BM_CLICK, IntPtr.Zero, IntPtr.Zero);
        return "{\"status\":\"stop_clicked\"}";
    }

    // ── Strategy Tester endpoints ─────────────────────────────────────────

    private static string TesterShowTab(HttpListenerRequest req)
    {
        var name = req.QueryString["name"];
        if (string.IsNullOrEmpty(name)) return Err("?name= required (e.g. ?name=Graph)");
        var mt5 = FindMt5Hwnd();
        if (mt5 == IntPtr.Zero) return Err("MT5 main window not found");
        bool ok = ShowTesterTab(mt5, name);
        return "{\"status\":\"" + (ok ? "switched" : "not_found") + "\",\"tab\":\"" + Escape(name) + "\"}";
    }

    private static string TesterConfigure(HttpListenerRequest req)
    {
        try
        {
            // Accept params from JSON body (POST) OR query string (GET)
            string bodyStr = "";
            if (req.HttpMethod == "POST")
                using (var reader = new StreamReader(req.InputStream))
                    bodyStr = reader.ReadToEnd();

            var mt5 = FindMt5Hwnd();
            if (mt5 == IntPtr.Zero) return Err("MT5 main window not found");

            var tester = FindTesterHwnd(mt5);

            var body = (string.IsNullOrWhiteSpace(bodyStr)
                ? new JsonObject()
                : JsonNode.Parse(bodyStr) as JsonObject) ?? new JsonObject();

            // Merge query string params (used by GET callers)
            foreach (var key in new[] { "expert","symbol","timeframe","start_date","end_date",
                                        "modelling","delays","deposit","currency","leverage","optimization" })
            {
                var v = req.QueryString[key];
                if (!string.IsNullOrEmpty(v) && body[key] == null)
                    body[key] = JsonValue.Create(v);
            }
            var ok     = new System.Collections.Generic.List<string>();
            var failed = new System.Collections.Generic.List<string>();

            void TryCombo(string field, int id)
            {
                var v = body[field]?.ToString();
                if (string.IsNullOrEmpty(v)) return;
                (SetComboValueWin32(tester, id, v) ? ok : failed).Add(field);
            }

            // Expert combo is owner-drawn — Win32 CB_FINDSTRINGEXACT fails because
            // items aren't in the combo's standard listbox. Use keystroke simulation.
            var expertReq = body["expert"]?.ToString();
            if (!string.IsNullOrEmpty(expertReq))
            {
                var r = SwitchExpertCore(expertReq);
                if (r.ok) ok.Add("expert"); else failed.Add("expert");
            }
            TryCombo("symbol",       int.Parse(ID_SYMBOL));
            TryCombo("timeframe",    int.Parse(ID_TIMEFRAME));
            TryCombo("delays",       int.Parse(ID_DELAYS));
            TryCombo("modelling",    int.Parse(ID_MODELLING));
            TryCombo("deposit",      int.Parse(ID_DEPOSIT));
            TryCombo("currency",     int.Parse(ID_CURRENCY));
            TryCombo("leverage",     int.Parse(ID_LEVERAGE));
            TryCombo("optimization", int.Parse(ID_OPTIMIZE));
            TryCombo("forward_type", int.Parse(ID_FORWARD));

            if (body["start_date"] != null || body["end_date"] != null)
                SetComboValueWin32(tester, int.Parse(ID_DATE_TYPE), "Custom period");

            var sdStr = body["start_date"]?.ToString();
            if (!string.IsNullOrEmpty(sdStr) && DateTime.TryParse(sdStr, out var sd))
                (SetDateFieldWin32(tester, int.Parse(ID_START_DATE), sd) ? ok : failed).Add("start_date");

            var edStr = body["end_date"]?.ToString();
            if (!string.IsNullOrEmpty(edStr) && DateTime.TryParse(edStr, out var ed))
                (SetDateFieldWin32(tester, int.Parse(ID_END_DATE), ed) ? ok : failed).Add("end_date");

            // Verify what is actually selected after all fields are set
            var actualExpert = ReadFieldText(tester, ID_EXPERT);
            var requestedExpert = body["expert"]?.ToString() ?? "";
            bool expertMatch = string.IsNullOrEmpty(requestedExpert) ||
                               string.Equals(actualExpert, requestedExpert, StringComparison.OrdinalIgnoreCase);
            if (!expertMatch && ok.Contains("expert"))
            {
                ok.Remove("expert");
                failed.Add("expert");
            }

            var sb = new StringBuilder();
            sb.Append("{\"status\":\"").Append(failed.Count == 0 ? "configured" : "partial").Append("\",");
            sb.Append("\"tester_hwnd\":\"0x").Append(tester.ToString("X")).Append("\",");
            sb.Append("\"actual_expert\":\"").Append(Escape(actualExpert)).Append("\",");
            sb.Append("\"set_ok\":[");
            for (int i = 0; i < ok.Count; i++) { if (i > 0) sb.Append(','); sb.Append('"').Append(Escape(ok[i])).Append('"'); }
            sb.Append("],\"set_failed\":[");
            for (int i = 0; i < failed.Count; i++) { if (i > 0) sb.Append(','); sb.Append('"').Append(Escape(failed[i])).Append('"'); }
            sb.Append("]}");
            return sb.ToString();
        }
        catch (Exception ex) { return Err(ex.Message, ex.GetType().Name); }
    }

    private static string TesterRunSync(HttpListenerRequest req)
    {
        try
        {
            var ts = req.QueryString["timeout"];
            int timeoutSec = (ts != null && int.TryParse(ts, out var t)) ? t : 1800;

            var mt5 = FindMt5Hwnd();
            if (mt5 == IntPtr.Zero) return Err("MT5 main window not found");

            var tester  = FindTesterHwnd(mt5);
            var startBtn = FindDescendantById(tester, int.Parse(ID_START_BTN));
            if (startBtn == IntPtr.Zero) return Err("Start button (id=16790) not found in tester panel");

            var t0 = DateTime.UtcNow;

            // Attempt 1: synchronous SendMessage BM_CLICK — no coordinates, works from any thread
            SendInt(startBtn, BM_CLICK, 0, 0);
            Thread.Sleep(600);
            if (GetWindowTextWin32(startBtn) != "Stop")
            {
                // Attempt 2: physical mouse via SendInput with corrected virtual-desktop coords
                PhysicalClick(startBtn, mt5);
                Thread.Sleep(600);
            }
            if (GetWindowTextWin32(startBtn) != "Stop")
            {
                // Attempt 3: async PostMessage BM_CLICK as last resort
                PostMessageInt(startBtn, BM_CLICK, IntPtr.Zero, IntPtr.Zero);
            }

            var focusTab = req.QueryString["focus_tab"] ?? "Graph";
            if (!string.IsNullOrEmpty(focusTab) && focusTab != "none")
            {
                for (int i = 0; i < 5; i++)
                {
                    Thread.Sleep(100);
                    if (ShowTesterTab(mt5, focusTab)) break;
                }
            }

            var deadline = DateTime.UtcNow.AddSeconds(timeoutSec);
            bool sawRunning = false;
            string lastSeen = "";
            // Cache the button handle — avoids repeated tree walks during the poll loop
            var cachedBtn = startBtn;

            while (DateTime.UtcNow < deadline)
            {
                Thread.Sleep(50);
                var name = GetWindowTextWin32(cachedBtn);
                if (string.IsNullOrEmpty(name))
                {
                    // Handle might have been recreated; re-find it
                    cachedBtn = FindDescendantById(tester, int.Parse(ID_START_BTN));
                    if (cachedBtn == IntPtr.Zero) continue;
                    name = GetWindowTextWin32(cachedBtn);
                }
                lastSeen = name;
                if (name == "Stop") sawRunning = true;
                else if (name == "Start" && sawRunning)
                {
                    var el = (DateTime.UtcNow - t0).TotalSeconds;
                    Thread.Sleep(200); // let MT5 flush the agent log
                    var stats = TesterLogResult();
                    return "{\"status\":\"completed\",\"elapsed_seconds\":"
                         + el.ToString(System.Globalization.CultureInfo.InvariantCulture)
                         + ",\"result\":" + stats + "}";
                }
            }

            var elT = (DateTime.UtcNow - t0).TotalSeconds;
            return "{\"status\":\"timeout\",\"elapsed_seconds\":"
                 + elT.ToString(System.Globalization.CultureInfo.InvariantCulture)
                 + ",\"last_button_state\":\"" + Escape(lastSeen)
                 + "\",\"saw_running\":" + (sawRunning ? "true" : "false") + "}";
        }
        catch (Exception ex) { return Err(ex.Message, ex.GetType().Name); }
    }

    private static string TesterBtnState()
    {
        var mt5 = FindMt5Hwnd();
        if (mt5 == IntPtr.Zero) return Err("MT5 not found");
        var tester = FindTesterHwnd(mt5);
        var btn = FindDescendantById(tester, int.Parse(ID_START_BTN));
        if (btn == IntPtr.Zero) return Err("Start button not found");
        var r = new RECT();
        GetWindowRect(btn, ref r);
        bool enabled = IsWindowEnabled(btn);
        bool visible = IsWindowVisible(btn);
        var parent = GetParent(btn);
        bool pEnabled = parent != IntPtr.Zero && IsWindowEnabled(parent);
        bool pVisible = parent != IntPtr.Zero && IsWindowVisible(parent);
        // Walk ancestors to find first disabled/hidden one
        string ancestorIssue = "none";
        var anc = btn;
        for (int i = 0; i < 10; i++)
        {
            anc = GetParent(anc);
            if (anc == IntPtr.Zero || anc == mt5) break;
            if (!IsWindowEnabled(anc)) { ancestorIssue = "disabled_at_depth_" + i; break; }
            if (!IsWindowVisible(anc)) { ancestorIssue = "hidden_at_depth_"   + i; break; }
        }
        int vx = GetSystemMetrics(76); int vy = GetSystemMetrics(77);
        int vw = GetSystemMetrics(78); int vh = GetSystemMetrics(79);
        int cx = (r.Left + r.Right) / 2; int cy = (r.Top + r.Bottom) / 2;
        int ax = vw > 0 ? (cx - vx) * 65535 / vw : 0;
        int ay = vh > 0 ? (cy - vy) * 65535 / vh : 0;
        return $"{{\"btn\":\"0x{btn:X}\",\"rect\":{{\"l\":{r.Left},\"t\":{r.Top},\"r\":{r.Right},\"b\":{r.Bottom}}},"
             + $"\"enabled\":{enabled.ToString().ToLower()},\"visible\":{visible.ToString().ToLower()},"
             + $"\"parent_enabled\":{pEnabled.ToString().ToLower()},\"parent_visible\":{pVisible.ToString().ToLower()},"
             + $"\"ancestor_issue\":\"{ancestorIssue}\","
             + $"\"sendinput_ax\":{ax},\"sendinput_ay\":{ay},"
             + $"\"vscreen\":{{\"x\":{vx},\"y\":{vy},\"w\":{vw},\"h\":{vh}}}}}";
    }

    private static string TesterWin32Diag()
    {
        var mt5 = FindMt5Hwnd();
        if (mt5 == IntPtr.Zero) return Err("MT5 main window not found");
        var tester = FindTesterHwnd(mt5);
        var dump   = Win32DiagDump(tester, 6);
        return "{\"mt5_hwnd\":\"0x" + mt5.ToString("X")
             + "\",\"tester_hwnd\":\"0x" + tester.ToString("X")
             + "\",\"controls\":" + dump + "}";
    }

    // ── JSON helpers ──────────────────────────────────────────────────────

    private static string Err(string msg, string? type = null) =>
        "{\"error\":\"" + Escape(msg ?? "") + "\""
        + (type != null ? ",\"type\":\"" + Escape(type) + "\"" : "")
        + "}";

    // Full RFC-8259 escape — handles all control chars, \n \r \t \b \f \uXXXX
    private static string Escape(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var sb = new StringBuilder(s.Length + 8);
        foreach (char c in s)
        {
            switch (c)
            {
                case '"':  sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n");  break;
                case '\r': sb.Append("\\r");  break;
                case '\t': sb.Append("\\t");  break;
                case '\b': sb.Append("\\b");  break;
                case '\f': sb.Append("\\f");  break;
                default:
                    if (c < 0x20) sb.Append($"\\u{(int)c:X4}");
                    else sb.Append(c);
                    break;
            }
        }
        return sb.ToString();
    }
}
