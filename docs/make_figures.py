"""Generate the README figures for MT5-Interlink into docs/img/ (python docs/make_figures.py).

Diagrams are drawn programmatically so they stay in sync with the code. The parallel-timeline
figure is a schematic of how the pool schedules work; it is labelled as such.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import numpy as np

IMG = Path(__file__).resolve().parent / "img"
IMG.mkdir(parents=True, exist_ok=True)
NAVY, TEAL, ROSE, MUTED, AMBER, PALE, INK = "#0F2A47", "#1B7F8C", "#C0392B", "#8A99A8", "#B7791F", "#EEF3F7", "#1F2933"


def box(ax, x, y, w, h, title, lines=(), fc=PALE, ec=NAVY, tc=NAVY, fs=9.5, lw=1.2, title_fc=None):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08", fc=fc, ec=ec, lw=lw))
    ax.text(x + w / 2, y + h - 0.16, title, ha="center", va="top", fontsize=fs, fontweight="bold", color=title_fc or tc)
    for i, ln in enumerate(lines):
        ax.text(x + w / 2, y + h - 0.40 - i * 0.22, ln, ha="center", va="top", fontsize=7.8, color=INK)


def arrow(ax, x1, y1, x2, y2, color=MUTED, lw=1.4, style="-|>", rad=0.0, ls="-"):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style, mutation_scale=12, color=color, lw=lw,
                                 connectionstyle=f"arc3,rad={rad}", linestyle=ls))


def architecture():
    fig, ax = plt.subplots(figsize=(13, 7.2))
    ax.set_xlim(0, 13); ax.set_ylim(0, 7.2); ax.axis("off")
    ax.text(0.15, 7.0, "MT5-Interlink architecture", fontsize=14, fontweight="bold", color=NAVY, va="top")
    ax.text(0.15, 6.62, "research on the left, live on the right; every result is MetaTrader's own native report", fontsize=9, color=MUTED, va="top")

    # inputs
    box(ax, 0.2, 4.6, 2.4, 1.5, "Strategy source", ["MQL5 .mq5 / compiled .ex5", "MetaEditor compile", "inputs via [TesterInputs]"])
    # optimiser
    box(ax, 0.2, 2.5, 2.4, 1.7, "Optimiser", ["grid / random / hill-climb / GA", "multi-objective scoring", "walk-forward IS -> OOS", "every-tick confirmation"])
    # pool controller
    box(ax, 3.3, 2.5, 3.1, 3.6, "Headless pool controller", ["atomic worker leases + heartbeat", "gated, staggered launches", "stall watchdog (tracks tester agent)",
                                                             "RAM governor for every-tick runs", "report integrity checks", "auto-retry, jittered backoff", "session run tags"], fc="#E8F3F4", ec=TEAL, tc=TEAL)
    # workers
    for i in range(4):
        y = 5.35 - i * 0.78
        box(ax, 7.0, y, 2.2, 0.62, f"worker {i+1}" if i < 3 else "worker N", [], fc="white", ec=NAVY, fs=8.5)
        ax.text(8.1, y + 0.16, "portable terminal + tester agent", ha="center", fontsize=7, color=MUTED)
        arrow(ax, 6.4, 3.9 + (1.5 - i) * 0.35, 7.0, y + 0.31)
    ax.text(8.1, 2.42, "shared tick history (junction)", ha="center", fontsize=7.8, color=MUTED, style="italic")
    ax.plot([7.0, 9.2], [2.62, 2.62], color=MUTED, lw=1, ls=":")
    # reports -> parser -> results
    box(ax, 9.9, 4.4, 2.9, 1.7, "Native HTML report", ["MetaTrader's own numbers", "deals table, balance line", "parsed to JSON schema"])
    box(ax, 9.9, 2.5, 2.9, 1.5, "Results", ["net, PF, Sharpe, DD, MAR,", "trades, best / worst trade", "equity reconstruction + PDF"])
    for i in range(4):
        y = 5.35 - i * 0.78 + 0.31
        arrow(ax, 9.2, y, 9.9, 5.25)
    arrow(ax, 11.35, 4.4, 11.35, 4.0)
    # flows
    arrow(ax, 2.6, 5.35, 3.3, 5.35)
    arrow(ax, 2.6, 3.35, 3.3, 3.35)
    arrow(ax, 9.9, 2.6, 2.6, 2.7, color=TEAL, rad=-0.18, ls="--")
    ax.text(8.1, 2.02, "results feed the next search round", fontsize=8, color=TEAL, ha="center", style="italic")
    # MCP + agents
    box(ax, 3.3, 0.35, 5.9, 1.45, "MCP server", ["30 tools: account & market, strategy files, tester control, research (sweeps, comparisons)",
                                                 "any MCP client can compile, launch a parallel comparison, read the ranked table"], fc="#FFF7E8", ec=AMBER, tc=AMBER)
    arrow(ax, 4.85, 1.8, 4.85, 2.5, color=AMBER)
    box(ax, 0.2, 0.35, 2.4, 1.45, "AI agents / operators", ["Claude, any MCP client,", "or the CLI directly"], fc="white")
    arrow(ax, 2.6, 1.07, 3.3, 1.07, color=AMBER)
    # live bridge
    box(ax, 9.9, 0.35, 2.9, 1.45, "Live bridge", ["BridgePro EA + in-process C# DLL", "HTTP /state, 200 ms + on-transaction", "dirty-flag diffing, atomic snapshot"], fc="#FBEAEA", ec=ROSE, tc=ROSE)
    arrow(ax, 9.9, 1.07, 9.2, 1.07, color=ROSE)
    ax.text(11.35, 0.18, "live terminal is never used for research", ha="center", fontsize=7.5, color=ROSE, style="italic")
    fig.tight_layout(); fig.savefig(IMG / "architecture.png", dpi=150); plt.close(fig)


def timeline():
    """Schematic of pool scheduling: N workers, staggered launches, one retry after a watchdog kill."""
    rng = np.random.default_rng(3)
    workers, runs = 6, 21
    durations = rng.uniform(4, 11, runs)
    fig, ax = plt.subplots(figsize=(13, 4.2))
    free = np.zeros(workers)
    stagger = 0.35
    colors = [NAVY, TEAL, "#3E6D8E", "#5B9AA0", "#2C4F6B", "#7FB3B8"]
    t_serial = durations.sum()
    for k in range(runs):
        w = int(np.argmin(free))
        start = max(free[w], k * stagger)
        d = durations[k]
        if k == 9:  # watchdog kill + retry
            ax.barh(w, 2.2, left=start, color=ROSE, alpha=0.7, height=0.6)
            ax.text(start + 1.1, w, "stall -> kill", ha="center", va="center", fontsize=7, color="white")
            start += 2.5
            ax.barh(w, d, left=start, color=colors[w], height=0.6, hatch="//", edgecolor="white")
            ax.text(start + d / 2, w, f"run {k+1} (retry)", ha="center", va="center", fontsize=7, color="white")
        else:
            ax.barh(w, d, left=start, color=colors[w], height=0.6, edgecolor="white")
            ax.text(start + d / 2, w, f"run {k+1}", ha="center", va="center", fontsize=7, color="white")
        free[w] = start + d + 0.3
    t_par = free.max()
    ax.set_yticks(range(workers)); ax.set_yticklabels([f"worker {i+1}" for i in range(workers)])
    ax.invert_yaxis(); ax.set_xlabel("minutes (schematic)")
    ax.set_title(f"Pool scheduling: {runs} every-tick backtests on {workers} workers, staggered launches, watchdog retry. "
                 f"Wall clock {t_par:.0f} min vs {t_serial:.0f} min serial", fontsize=10, loc="left")
    ax.grid(alpha=0.25, axis="x")
    fig.tight_layout(); fig.savefig(IMG / "pool_timeline.png", dpi=150); plt.close(fig)


def optimisation_flow():
    fig, ax = plt.subplots(figsize=(13, 2.9))
    ax.set_xlim(0, 13); ax.set_ylim(0, 2.9); ax.axis("off")
    steps = [
        ("1. Search wide", ["fast model (OHLC)", "grid / random / GA", "100k+ combinations via", "native engine, or Python"], PALE, NAVY),
        ("2. Score", ["any metric expression", "with constraints, e.g.", "mar if dd<10 and", "trades>200 else -1e9"], PALE, NAVY),
        ("3. Out-of-sample", ["re-test top candidates on", "a window they never saw", "same risk %, penalise", "OOS decay"], "#E8F3F4", TEAL),
        ("4. Confirm every-tick", ["finalists re-run on", "model 0, the only", "verdict that counts"], "#E8F3F4", TEAL),
        ("5. Baseline + verify", ["current defaults always run", "inputs verified by trade count", "one result per task, tagged"], "#FFF7E8", AMBER),
    ]
    x = 0.2
    for i, (t, lines, fc, ec) in enumerate(steps):
        box(ax, x, 0.35, 2.3, 1.85, t, lines, fc=fc, ec=ec, tc=ec)
        if i < len(steps) - 1:
            arrow(ax, x + 2.3, 1.27, x + 2.3 + 0.28, 1.27, color=MUTED, lw=1.8)
        x += 2.58
    ax.text(0.2, 2.78, "Optimisation pipeline: search on the fast model, confirm on the accurate one, reject anything that fades out-of-sample", fontsize=10.5, fontweight="bold", color=NAVY, va="top")
    fig.tight_layout(); fig.savefig(IMG / "optimisation_pipeline.png", dpi=150); plt.close(fig)


def worker_lifecycle():
    fig, ax = plt.subplots(figsize=(13, 3.4))
    ax.set_xlim(0, 13); ax.set_ylim(0, 3.4); ax.axis("off")
    states = [("free", PALE, NAVY), ("leased", "#E8F3F4", TEAL), ("launch gate", "#E8F3F4", TEAL), ("running", "#E8F3F4", TEAL),
              ("report verified", "#E8F3F4", TEAL), ("released", PALE, NAVY)]
    x = 0.3
    for i, (s, fc, ec) in enumerate(states):
        box(ax, x, 1.5, 1.75, 0.8, s, [], fc=fc, ec=ec, tc=ec, fs=9)
        if i < len(states) - 1:
            arrow(ax, x + 1.75, 1.9, x + 1.75 + 0.35, 1.9, color=MUTED, lw=1.6)
        x += 2.1
    # failure paths
    box(ax, 6.6, 0.2, 2.6, 0.7, "stalled (agent dead)", [], fc="#FBEAEA", ec=ROSE, tc=ROSE, fs=8.5)
    arrow(ax, 7.2, 1.5, 7.5, 0.9, color=ROSE)
    arrow(ax, 6.6, 0.55, 2.9, 0.55, color=ROSE, ls="--")
    arrow(ax, 1.6, 0.9, 1.15, 1.5, color=ROSE, ls="--")
    ax.text(4.75, 0.68, "teardown + retry with backoff", fontsize=7.8, color=ROSE, ha="center", style="italic")
    box(ax, 0.3, 0.2, 2.6, 0.7, "stale lease taken over / retry", [], fc="#FBEAEA", ec=ROSE, tc=ROSE, fs=8.5)
    arrow(ax, 2.6, 1.5, 2.2, 0.9, color=ROSE, ls="--")
    ax.text(0.3, 3.25, "Worker lifecycle: leases are atomic, heartbeats detect dead sessions, the watchdog tracks the tester agent (not just the terminal)", fontsize=10.5, fontweight="bold", color=NAVY, va="top")
    ax.text(10.6, 2.55, "report newer than launch\nstale reports purged first", fontsize=7.5, color=MUTED, ha="center")
    fig.tight_layout(); fig.savefig(IMG / "worker_lifecycle.png", dpi=150); plt.close(fig)


if __name__ == "__main__":
    architecture(); timeline(); optimisation_flow(); worker_lifecycle()
    print("figures written to", IMG)
