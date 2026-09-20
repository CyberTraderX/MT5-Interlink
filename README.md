# MT5-Interlink

**A parallel, headless backtest and optimisation engine for MetaTrader 5, with an MCP server that lets AI agents drive it.**

MetaTrader's Strategy Tester is accurate but single-threaded, GUI-bound and hard to script cleanly. MT5-Interlink turns it into a research service: a pool of portable, headless MT5 terminals that run many backtests at once, return MetaTrader's own native reports as ground truth, and expose the whole thing to Claude (or any MCP client) as tools. No EA source changes are needed. Any compiled `.ex5` works.

---

## What it does

### Parallel headless pool (`headless_pool.py`)
- **One portable MT5 worker per CPU lane.** Workers live at `C:\MT5_Headless`, `C:\MT5_Headless_2`, … and share tick history through a junction, so N strategies (or N parameter sets) backtest simultaneously. The live trading terminal is never touched.
- **Native reports as truth.** Each run yields MetaTrader's own HTML report, parsed into a JSON schema (net, PF, Sharpe, max DD, MAR, trade count, win rate, best and worst trade). No re-implemented metrics, no OnTester hacks.
- **Every-tick, OHLC or open-price models** selectable per run. Every-tick (model 0) is treated as the only trustworthy verdict; the faster models are used for screening only.
- **Equity curve reconstruction.** MT5's report only carries the balance line; `equity_curve.py` rebuilds the true equity time-series from the deal list and renders it, with optional PDF export.

### Built for many agents at once
The pool was hardened for the case where several AI sessions share one machine and launch runs independently:
- **Atomic worker leases** with heartbeat and stale-lease takeover, so two sessions can never claim the same terminal.
- **Gated, staggered launches** to stop tester agents racing for the same local port.
- **Stall watchdog.** A terminal can linger alive-but-idle after its `metatester64` agent dies under RAM pressure. The pool tracks the agent process, not just the terminal, tears the run down after a configurable idle window and retries, instead of blocking for the full timeout.
- **Zombie cleanup** on demand and automatically when no free worker is found.
- **Report integrity checks.** Stale reports are purged before launch, a locked stale report refuses to run, and any report older than the launch timestamp is rejected. One task can never be handed another task's result.
- **RAM governor.** A semaphore caps concurrent every-tick runs machine-wide (each agent grows to several GB of tick history), tunable via `MT5_EVERYTICK_MAX`.
- **Run tags.** Every result JSON and filename carries a session tag, so each agent pulls only its own results from the shared results folder.
- **Auto-retry with jittered backoff** on no-report and agent-port-bind failures.

### Optimisation (`optimizer.py`, `optimize.py`)
Two engines, one philosophy: search wide on the fast model, confirm the finalists every-tick, and reject anything that fades out-of-sample.
- **Native engine.** One in-process MT5 optimisation (genetic or complete) over the full space, read from MT5's official all-passes XML. Handles 100k+ combinations.
- **Python engine.** The search runs in Python over the parallel pool with one native report per candidate. Strategies: grid, random, coordinate hill-climb, genetic (tournament, crossover, mutation).
- **Multi-objective scoring.** Rank on any expression of the metrics, with constraints. Example: `mar if dd<10 and trades>200 else -1e9`.
- **Walk-forward validation.** Score in-sample, re-test the top candidates on an out-of-sample window they never saw, penalise configs whose OOS performance decays. The current defaults are always run as a baseline.
- **Parameter injection** via the tester's `[TesterInputs]` section, verified by trade count so a silently ignored input can't pass as a result.

### MCP server (`mcp/server.py`)
Exposes MT5 to any MCP client as 30 tools across two channels: the MetaTrader5 Python package for read-only account and market data, and the in-process HTTP bridge for tester automation and live pushed state.

| Group | Tools |
|---|---|
| Account & market | `mt5_account`, `mt5_state`, `mt5_positions`, `mt5_orders`, `mt5_symbols`, `mt5_tick`, `mt5_rates`, `mt5_deals`, `mt5_history`, `mt5_portfolio_exposure` |
| Strategy files | `mt5_compile`, `mt5_list_strategies`, `mt5_read_strategy`, `mt5_write_strategy` |
| Strategy Tester | `mt5_tester_configure`, `mt5_tester_smart_configure`, `mt5_tester_run`, `mt5_tester_status`, `mt5_tester_stop`, `mt5_tester_read_settings` |
| Research | `mt5_bt_results`, `mt5_compare_run`, `mt5_quick_compare`, `mt5_sweep`, `mt5_headless_compare` |

An agent can therefore compile an EA, launch a parallel comparison across symbols, read back the ranked table and decide the next experiment without a human touching the terminal.

### Live bridge (`BridgePro.mq5` + `MT5Bridge.cs`)
An in-process C# HTTP bridge loaded as a DLL by a lightweight EA. It pushes account, position, order and terminal state on a 200 ms timer and instantly on every trade transaction, with dirty-flag diffing so unchanged state costs nothing. A single `/state` endpoint returns everything atomically. Full RFC-8259 JSON escaping and correct 64-bit ticket encoding.

---

## Quick start

```powershell
# compare several EAs on one symbol, every-tick, ranked by profit factor
python headless_pool.py --period H1 --from 2020.01.01 --to 2025.12.31 --model 0 `
    --symbol NDX100 --expert StrategyA.ex5 --expert StrategyB.ex5 --sort pf --tag mySession

# walk-forward optimisation with an OOS split and every-tick confirmation of the top 5
python optimizer.py --ea StrategyA.ex5 --symbol NDX100 --period H1 `
    --from 2021.01.01 --to 2026.06.01 `
    --param InpVolMultiplier=1.0:1.4:0.1 --param InpRiskPercent=0.30:0.55:0.01 `
    --objective "mar if dd<10 and trades>200 else -1e9" `
    --search genetic --oos-split 2024.09.01 --confirm-top 5

# pool health
python headless_pool.py --status
```

Setup for a new machine, including worker creation and MCP registration, is in [SETUP_NEW_MACHINE.md](SETUP_NEW_MACHINE.md). All machine identity (terminal ID, login, paths) lives in `interlink_config.py` and is overridable by environment variables; nothing else hardcodes a path.

## Design rules this enforces
- Every-tick is the only verdict that counts. OHLC and open-price runs are for screening.
- In-sample and out-of-sample are tested at the same risk. A config that wins IS and fades OOS is a curve-fit, not an edge.
- Inputs are verified by effect (trade count), never assumed.
- One result per task, provably from that task.

## Layout
```
headless_pool.py      parallel pool: leases, launch gate, watchdog, retries, export
headless_runner.py    single-worker runner + native report parser
optimizer.py          native + Python optimisation engines, multi-objective, walk-forward
optimize.py           simpler IS/OOS grid optimiser
equity_curve.py       equity reconstruction + chart/PDF rendering
interlink_config.py   single source of truth for paths, terminal, login (env-overridable)
dev_cycle.py          rebuild DLL, compile EA, redeploy (stops MT5)
mcp/server.py         MCP server (FastMCP)
BridgePro.mq5         bridge EA
MT5Bridge.cs/.csproj  in-process HTTP bridge (.NET, win-x64)
```

## Requirements
Windows, MetaTrader 5, Python 3.12+ with `MetaTrader5` and `fastmcp`, .NET SDK for the bridge. Portable MT5 copies for the workers.

## Licence
MIT. Not affiliated with MetaQuotes.
