//+------------------------------------------------------------------+
//|  BridgePro.mq5                                                   |
//|  Enhanced MT5 bridge — drop-in replacement for BridgeEA.mq5     |
//|                                                                  |
//|  Improvements over BridgeEA v0.47:                               |
//|   1. 200ms push timer  (5x faster live state updates)           |
//|   2. OnTradeTransaction() — instant push on every trade event   |
//|   3. Dirty-flag diff   — skips DLL call when state unchanged    |
//|   4. Correct ulong ticket encoding (no silent int overflow)     |
//|   5. Full RFC-8259 JSON escaping (\n \r \t \uXXXX etc)         |
//|   6. Terminal info refreshed every 60 s (not just once at init) |
//|   7. ping_last and connected fields added to terminal payload   |
//|                                                                  |
//|  Same DLL interface as BridgeEA — MT5Bridge.dll unchanged.      |
//|  Attach to ANY one chart. Port must match the MCP bridge config.|
//+------------------------------------------------------------------+
#property copyright "BridgePro"
#property version   "1.00"
#property strict
#property description "BridgePro — faster bridge: 200ms, event-driven, bug-fixed"

#import "MT5Bridge.dll"
    int  BridgeStart(int port);
    void BridgeStop();
    void BridgePushAccount(double balance, double equity, double margin,
                           double freeMargin, double profit);
    void BridgePushPositions(string json);
    void BridgePushOrders(string json);
    void BridgePushTerminal(string json);
#import

input int Port           = 8892;  // HTTP server port (must match MCP config)
input int PushIntervalMs = 200;   // State push interval ms (200 = 5x faster)

//--- dirty-flag state cache
string   g_prevPositions = "";
string   g_prevOrders    = "";
datetime g_lastTerminal  = 0;

//+------------------------------------------------------------------+
int OnInit()
{
    int ok = BridgeStart(Port);
    if(ok != 1)
    {
        PrintFormat("BridgePro: FAILED to start on port %d — check MT5Bridge.dll in MQL5\\Libraries", Port);
        return INIT_FAILED;
    }

    BridgePushTerminal(BuildTerminalJson());
    g_lastTerminal = TimeCurrent();

    EventSetMillisecondTimer(PushIntervalMs);
    PrintFormat("BridgePro v1.00: http://localhost:%d | push every %d ms", Port, PushIntervalMs);
    return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    EventKillTimer();
    BridgeStop();
    PrintFormat("BridgePro: stopped (reason=%d)", reason);
}

//+------------------------------------------------------------------+
void OnTimer()
{
    PushState(false);
}

//--- Fires instantly on every trade event (order placed/filled/cancelled,
//    position opened/modified/closed). No waiting for the next timer tick.
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest     &req,
                        const MqlTradeResult      &res)
{
    PushState(true);
}

void OnTick() { /* no-op — bridge is timer + event driven */ }

//+------------------------------------------------------------------+
void PushState(bool force)
{
    //--- account: always push (5 doubles, cheap)
    BridgePushAccount(
        AccountInfoDouble(ACCOUNT_BALANCE),
        AccountInfoDouble(ACCOUNT_EQUITY),
        AccountInfoDouble(ACCOUNT_MARGIN),
        AccountInfoDouble(ACCOUNT_MARGIN_FREE),
        AccountInfoDouble(ACCOUNT_PROFIT)
    );

    //--- positions: only push when changed (or forced by trade event)
    string posJson = BuildPositionsJson();
    if(force || posJson != g_prevPositions)
    {
        BridgePushPositions(posJson);
        g_prevPositions = posJson;
    }

    //--- orders: only push when changed
    string ordJson = BuildOrdersJson();
    if(force || ordJson != g_prevOrders)
    {
        BridgePushOrders(ordJson);
        g_prevOrders = ordJson;
    }

    //--- terminal: refresh every 60 s (ping/connection state can change)
    if(TimeCurrent() - g_lastTerminal >= 60)
    {
        BridgePushTerminal(BuildTerminalJson());
        g_lastTerminal = TimeCurrent();
    }
}

//+------------------------------------------------------------------+
//  JSON helpers
//+------------------------------------------------------------------+

//--- Full RFC-8259 compliant string escape
string EscapeStr(string s)
{
    string out = "";
    int len = StringLen(s);
    for(int i = 0; i < len; i++)
    {
        ushort c = StringGetCharacter(s, i);
        if      (c == '"')  out += "\\\"";
        else if (c == '\\') out += "\\\\";
        else if (c == '\n') out += "\\n";
        else if (c == '\r') out += "\\r";
        else if (c == '\t') out += "\\t";
        else if (c == 0x08) out += "\\b";
        else if (c == 0x0C) out += "\\f";
        else if (c < 0x20)  out += StringFormat("\\u%04X", c);
        else                out += ShortToString(c);
    }
    return out;
}

//--- Safe ulong -> string without int truncation
string TicketStr(ulong ticket)
{
    return StringFormat("%I64u", ticket);
}

//+------------------------------------------------------------------+
string BuildTerminalJson()
{
    string s = "{";
    s += "\"company\":\""       + EscapeStr(AccountInfoString(ACCOUNT_COMPANY))   + "\",";
    s += "\"server\":\""        + EscapeStr(AccountInfoString(ACCOUNT_SERVER))    + "\",";
    s += "\"name\":\""          + EscapeStr(AccountInfoString(ACCOUNT_NAME))      + "\",";
    s += "\"login\":"           + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
    s += "\"currency\":\""      + EscapeStr(AccountInfoString(ACCOUNT_CURRENCY))  + "\",";
    s += "\"leverage\":"        + IntegerToString((long)AccountInfoInteger(ACCOUNT_LEVERAGE)) + ",";
    s += "\"trade_mode\":"      + IntegerToString((long)AccountInfoInteger(ACCOUNT_TRADE_MODE)) + ",";
    s += "\"trade_allowed\":"   + (AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ? "true" : "false") + ",";
    s += "\"trade_expert\":"    + (AccountInfoInteger(ACCOUNT_TRADE_EXPERT)  ? "true" : "false") + ",";
    s += "\"terminal_path\":\"" + EscapeStr(TerminalInfoString(TERMINAL_PATH))     + "\",";
    s += "\"data_path\":\""     + EscapeStr(TerminalInfoString(TERMINAL_DATA_PATH)) + "\",";
    s += "\"build\":"           + IntegerToString((long)TerminalInfoInteger(TERMINAL_BUILD)) + ",";
    s += "\"ping_last\":"       + IntegerToString((long)TerminalInfoInteger(TERMINAL_PING_LAST)) + ",";
    s += "\"connected\":"       + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false") + ",";
    s += "\"dlls_allowed\":"    + (TerminalInfoInteger(TERMINAL_DLLS_ALLOWED) ? "true" : "false");
    s += "}";
    return s;
}

//+------------------------------------------------------------------+
string BuildPositionsJson()
{
    string s = "[";
    int total = PositionsTotal();
    bool first = true;
    for(int i = 0; i < total; i++)
    {
        ulong ticket = PositionGetTicket(i);
        if(ticket == 0) continue;
        if(!first) s += ",";
        first = false;
        s += "{";
        s += "\"ticket\":"          + TicketStr(ticket) + ",";
        s += "\"symbol\":\""        + EscapeStr(PositionGetString(POSITION_SYMBOL))  + "\",";
        s += "\"type\":\""          + (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL") + "\",";
        s += "\"volume\":"          + DoubleToString(PositionGetDouble(POSITION_VOLUME),        2) + ",";
        s += "\"price_open\":"      + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN),    5) + ",";
        s += "\"price_current\":"   + DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT), 5) + ",";
        s += "\"sl\":"              + DoubleToString(PositionGetDouble(POSITION_SL), 5) + ",";
        s += "\"tp\":"              + DoubleToString(PositionGetDouble(POSITION_TP), 5) + ",";
        s += "\"profit\":"          + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) + ",";
        s += "\"swap\":"            + DoubleToString(PositionGetDouble(POSITION_SWAP),   2) + ",";
        s += "\"magic\":"           + IntegerToString((long)PositionGetInteger(POSITION_MAGIC)) + ",";
        s += "\"time_open\":"       + IntegerToString((long)PositionGetInteger(POSITION_TIME)) + ",";
        s += "\"identifier\":"      + TicketStr((ulong)PositionGetInteger(POSITION_IDENTIFIER)) + ",";
        s += "\"comment\":\""       + EscapeStr(PositionGetString(POSITION_COMMENT))  + "\"";
        s += "}";
    }
    s += "]";
    return s;
}

//+------------------------------------------------------------------+
string BuildOrdersJson()
{
    string s = "[";
    int total = OrdersTotal();
    bool first = true;
    for(int i = 0; i < total; i++)
    {
        ulong ticket = OrderGetTicket(i);
        if(ticket == 0) continue;
        if(!first) s += ",";
        first = false;
        s += "{";
        s += "\"ticket\":"        + TicketStr(ticket) + ",";
        s += "\"symbol\":\""      + EscapeStr(OrderGetString(ORDER_SYMBOL)) + "\",";
        s += "\"type\":"          + IntegerToString((long)OrderGetInteger(ORDER_TYPE)) + ",";
        s += "\"volume_init\":"   + DoubleToString(OrderGetDouble(ORDER_VOLUME_INITIAL), 2) + ",";
        s += "\"volume_curr\":"   + DoubleToString(OrderGetDouble(ORDER_VOLUME_CURRENT), 2) + ",";
        s += "\"price_open\":"    + DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN),    5) + ",";
        s += "\"price_curr\":"    + DoubleToString(OrderGetDouble(ORDER_PRICE_CURRENT), 5) + ",";
        s += "\"sl\":"            + DoubleToString(OrderGetDouble(ORDER_SL), 5) + ",";
        s += "\"tp\":"            + DoubleToString(OrderGetDouble(ORDER_TP), 5) + ",";
        s += "\"magic\":"         + IntegerToString((long)OrderGetInteger(ORDER_MAGIC)) + ",";
        s += "\"time_setup\":"    + IntegerToString((long)OrderGetInteger(ORDER_TIME_SETUP)) + ",";
        s += "\"time_expiry\":"   + IntegerToString((long)OrderGetInteger(ORDER_TIME_EXPIRATION)) + ",";
        s += "\"state\":"         + IntegerToString((long)OrderGetInteger(ORDER_STATE)) + ",";
        s += "\"comment\":\""     + EscapeStr(OrderGetString(ORDER_COMMENT)) + "\"";
        s += "}";
    }
    s += "]";
    return s;
}
