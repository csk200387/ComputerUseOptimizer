"""README charts for ComputerUseOptimizer. Numbers are copied from the private experiment records
(benchmarks/results.md 'M9 main run', 2026-11-21; DECISIONS 'M0 baseline'; calculator demo 2026-11-24)."""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs/images"
OUT.mkdir(parents=True, exist_ok=True)

INK, BODY, MUTED = "#1B2233", "#4A5568", "#8A90A0"
BLUE, GREY, SLATE, ORANGE, BG = "#2457C5", "#5B6770", "#A7B0B6", "#B04A12", "#FFFFFF"
plt.rcParams.update({"font.family": "Helvetica", "font.size": 11, "text.color": INK, "axes.labelcolor": BODY,
                     "xtick.color": BODY, "ytick.color": BODY, "axes.edgecolor": "#D5D9DD",
                     "figure.facecolor": BG, "axes.facecolor": BG, "savefig.dpi": 200})

# task, steps, cuop input, screenshot input, cuop $, screenshot $, cuop ok, shot ok
M9 = [
    ("Web order form (Chromium)", 123248, 413102, 0.066, 0.242, 3, 3),
    ("Copy between two apps", 167138, 363347, 0.072, 0.211, 3, 3),
    ("TextEdit editing", 136418, 240063, 0.086, 0.153, 3, 3),
    ("Menu-bar battery panel", 18763, 57282, 0.016, 0.050, 3, 0),
    ("VS Code (Electron)", 37890, 56729, 0.029, 0.054, 3, 3),
    ("Scientific calculator", 100711, 113002, 0.051, 0.117, 3, 3),
    ("Preview: rotate + save", 65444, 66150, 0.040, 0.064, 3, 3),
    ("Dictionary lookup", 31734, 27489, 0.027, 0.036, 3, 3),
    ("Clock (Catalyst)", 53055, 38449, 0.028, 0.044, 3, 3),
    ("Calculator 2 + 5", 52498, 23020, 0.027, 0.048, 3, 3),
    ("Activity Monitor", 123443, 30914, 0.080, 0.046, 3, 3),
    ("Web canvas seat booking", 236206, 126934, 0.128, 0.094, 3, 3),
]


def pct(a, b):
    return (a - b) / b * 100


# 1. per-task savings (input tokens and cost)
rows = sorted(M9, key=lambda r: pct(r[1], r[2]))
fig, ax = plt.subplots(figsize=(9.5, 6.2))
y = range(len(rows))
inp = [pct(r[1], r[2]) for r in rows]
cost = [pct(r[3], r[4]) for r in rows]
ax.barh([i + 0.2 for i in y], inp, height=0.38, color=[BLUE if v < 0 else ORANGE for v in inp], label="Cumulative input tokens")
ax.barh([i - 0.2 for i in y], cost, height=0.38, color=[SLATE if v < 0 else "#E3B9A2" for v in cost], label="Cost (USD)")
for i, (a, c) in enumerate(zip(inp, cost)):
    ax.text(a + (2 if a >= 0 else -2), i + 0.2, f"{a:+.0f}%", va="center", ha="left" if a >= 0 else "right", fontsize=9.5,
            color=INK, fontweight="bold")
    ax.text(c + (2 if c >= 0 else -2), i - 0.2, f"{c:+.0f}%", va="center", ha="left" if c >= 0 else "right", fontsize=8.5,
            color=MUTED)
ax.set_yticks(list(y))
ax.set_yticklabels([r[0] for r in rows])
ax.axvline(0, color=INK, lw=0.8)
ax.set_xlim(-95, 330)
ax.invert_yaxis()
ax.set_xlabel("CUOP vs screenshot agent  (negative = CUOP uses less)")
for s in ("top", "right", "left"):
    ax.spines[s].set_visible(False)
ax.tick_params(axis="y", length=0)
ax.legend(loc="upper right", frameon=False)
ax.set_title("Per task: where CUOP saves the most (and where it doesn't)\n12 macOS tasks × 3 runs each, same model (Claude Sonnet 5.5)",
             loc="left", fontsize=13, fontweight="bold", color=INK)
fig.tight_layout()
fig.savefig(OUT / "per_task_savings.png")
plt.close(fig)

# 2. totals
fig, axes = plt.subplots(1, 3, figsize=(9.5, 3.4))
for ax, (title, a, b, fmt) in zip(axes, [
    ("Cumulative input tokens", 3.44, 4.67, "{:.2f}M"),
    ("Cost (USD)", 1.95, 3.48, "${:.2f}"),
    ("Tasks completed", 36, 33, "{:.0f}/36"),
]):
    bars = ax.bar(["CUOP", "Screenshot"], [a, b], color=[BLUE, GREY], width=0.6)
    for bar, v in zip(bars, (a, b)):
        ax.text(bar.get_x() + bar.get_width() / 2, v * 1.02, fmt.format(v), ha="center", va="bottom", fontsize=11,
                fontweight="bold", color=INK)
    ax.set_title(title, fontsize=11, color=INK)
    ax.set_yticks([])
    ax.set_ylim(0, max(a, b) * 1.22)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
axes[0].text(0.5, -0.24, "−26.3%", transform=axes[0].transAxes, ha="center", color=BLUE, fontsize=13, fontweight="bold")
axes[1].text(0.5, -0.24, "−43.9%", transform=axes[1].transAxes, ha="center", color=BLUE, fontsize=13, fontweight="bold")
axes[2].text(0.5, -0.24, "+3 tasks", transform=axes[2].transAxes, ha="center", color=BLUE, fontsize=13, fontweight="bold")
fig.suptitle("All 72 runs (12 tasks × 3 runs × 2 agents)", x=0.02, ha="left", fontsize=13, fontweight="bold", color=INK)
fig.tight_layout(rect=(0, 0.06, 1, 0.93))
fig.savefig(OUT / "totals.png")
plt.close(fig)

# 3. one observation of one window
fig, ax = plt.subplots(figsize=(9.5, 3.3))
groups = ["Calculator window", "ChatGPT desktop window"]
vals = {"CUOP text": (298, 189), "Screenshot": (500, 1533), "agent-desktop JSON": (1393, 10399)}
cols = {"CUOP text": BLUE, "Screenshot": GREY, "agent-desktop JSON": SLATE}
h = 0.26
for k, (name, v) in enumerate(vals.items()):
    pos = [i + (1 - k) * h for i in range(2)]
    ax.barh(pos, v, height=h * 0.9, color=cols[name], label=name)
    for p, x in zip(pos, v):
        ax.text(x * 1.12, p, f"{x:,}", va="center", fontsize=10, color=INK)
ax.set_xscale("log")
ax.set_xlim(80, 40000)
ax.set_yticks(range(2))
ax.set_yticklabels(groups)
ax.invert_yaxis()
ax.set_xlabel("Tokens for one look at the window (log scale)")
for s in ("top", "right", "left"):
    ax.spines[s].set_visible(False)
ax.tick_params(axis="y", length=0)
ax.legend(loc="upper right", frameon=False, fontsize=10)
ax.set_title("One observation of the same window", loc="left", fontsize=13, fontweight="bold", color=INK)
fig.tight_layout()
fig.savefig(OUT / "single_observation.png")
plt.close(fig)

# 4. calculator 7 + 5 = step by step
steps = ["look", "press 7", "press +", "press 5", "press ="]
shot = [500, 1000, 1500, 2000, 2500]
cuop = [264, 293, 314, 337, 391]
fig, ax = plt.subplots(figsize=(9.5, 3.4))
ax.plot(steps, shot, marker="o", color=GREY, lw=2.2, label="Screenshot: one 500-token image per step")
ax.plot(steps, cuop, marker="o", color=BLUE, lw=2.6, label="CUOP: 264-token tree once, then only the changed lines")
ax.text(4.08, 2500, "2,500", va="center", fontsize=11, fontweight="bold", color=GREY)
ax.text(4.08, 391, "391  (−84%)", va="center", fontsize=11, fontweight="bold", color=BLUE)
ax.set_xlim(-0.2, 4.9)
ax.set_ylim(0, 2850)
ax.set_ylabel("Cumulative screen tokens")
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
ax.legend(loc="upper left", frameon=False, fontsize=10)
ax.set_title("Calculator 7 + 5 = — screen information sent to the model", loc="left", fontsize=13, fontweight="bold",
             color=INK)
fig.tight_layout()
fig.savefig(OUT / "calculator_steps.png")
plt.close(fig)
print("ok", sorted(p.name for p in OUT.iterdir()))
