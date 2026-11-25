"""Figures for the project document and slides (docs/assets/*.png).

    docs/.venv/bin/python docs/build_figures.py
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).parent / "assets"
NAVY, TEAL, SLATE, LIGHT, AMBER, INK, WHITE = "#0B2545", "#13A89E", "#5C6B7A", "#EEF2F5", "#E8A33D", "#1B1F24", "#FFFFFF"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})


def box(ax, x, y, w, h, title, sub="", fill=LIGHT, edge=SLATE, tcolor=INK, bold=True, fs=11.5):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.12",
                                linewidth=1.2, edgecolor=edge, facecolor=fill))
    if sub:
        ax.text(x + w / 2, y + h * 0.63, title, ha="center", va="center", fontsize=fs, color=tcolor,
                fontweight="bold" if bold else "normal")
        ax.text(x + w / 2, y + h * 0.30, sub, ha="center", va="center", fontsize=9.6, color=tcolor, linespacing=1.2)
    else:
        ax.text(x + w / 2, y + h / 2, title, ha="center", va="center", fontsize=fs, color=tcolor,
                fontweight="bold" if bold else "normal")


def arrow(ax, x1, y1, x2, y2, label="", color=SLATE, both=False, rad=0.0, lx=0, ly=0, fs=9.4):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="<|-|>" if both else "-|>", mutation_scale=12,
                                 linewidth=1.3, color=color, connectionstyle=f"arc3,rad={rad}"))
    if label:
        ax.text((x1 + x2) / 2 + lx, (y1 + y2) / 2 + ly, label, ha="center", va="center", fontsize=fs, color=color,
                bbox=dict(boxstyle="round,pad=0.15", fc=WHITE, ec="none"))


def architecture():
    fig, ax = plt.subplots(figsize=(11.9, 6.2), dpi=200)
    ax.set_xlim(0, 11.9)
    ax.set_ylim(0, 6.2)
    ax.axis("off")
    # left column: people and messages; middle: router and agents; right column: harness -> human -> database
    box(ax, 0.2, 4.6, 1.7, 1.0, "Patient", "chat (web UI)", fill=WHITE)
    box(ax, 2.5, 4.6, 2.1, 1.0, "Front-desk router", "Jev decision model\n(rules as backup)", fill=NAVY, edge=NAVY, tcolor=WHITE)
    box(ax, 2.5, 2.95, 2.1, 1.0, "Front desk", "fixed reply, no LLM\n(medical / emergency)", fill=WHITE)
    box(ax, 5.3, 4.6, 2.3, 1.0, "Scheduling agent", "LLM (Claude Haiku 5.5)\n13 actions", fill=TEAL, edge=TEAL, tcolor=WHITE)
    box(ax, 5.3, 2.95, 2.3, 1.0, "FAQ agent", "LLM (Claude Haiku 5.5)\n5 actions", fill=TEAL, edge=TEAL, tcolor=WHITE)
    box(ax, 8.55, 4.3, 2.45, 1.55, "Python harness", "parser + validator,\nPython checks, output guard,\nstep budget", fill=LIGHT)
    box(ax, 8.55, 2.55, 2.45, 1.05, "Staff approval", "a human approves or\ndenies every change", fill=AMBER, edge=AMBER, tcolor=INK)
    box(ax, 8.55, 0.35, 2.45, 1.5, "Postgres (Supabase)", "slots, appointments, approvals\n(audit), patients, notifications", fill=WHITE)
    box(ax, 0.2, 0.35, 4.4, 1.25, "Reminder agent (background, every 60 s)",
        "Python picks due notices; LLM writes email + text;\nPython checks them and attaches the .ics invite", fill=LIGHT)
    box(ax, 0.2, 2.95, 1.7, 1.0, "Messages", "email, text,\ncalendar invite", fill=WHITE)

    arrow(ax, 1.9, 5.1, 2.5, 5.1, "")
    arrow(ax, 3.55, 4.6, 3.55, 3.95, "medical /\nemergency", lx=0.55)
    arrow(ax, 4.6, 5.25, 5.3, 5.25, "intent", ly=0.17)
    arrow(ax, 4.6, 4.8, 5.3, 3.6, "", rad=-0.2)
    arrow(ax, 6.45, 4.6, 6.45, 3.95, "transfer", both=True, lx=0.55)
    arrow(ax, 7.6, 5.4, 8.55, 5.4, "JSON action", ly=0.22)
    arrow(ax, 8.55, 4.8, 7.6, 4.8, "observation", ly=-0.22)
    arrow(ax, 9.78, 4.3, 9.78, 3.6, "book / cancel / move request", lx=0.0, ly=0.0)
    arrow(ax, 9.78, 2.55, 9.78, 1.85, "approved: save, then re-read", lx=0.0, ly=0.0)
    arrow(ax, 11.0, 4.6, 11.0, 1.6, "", both=True, rad=-0.35)
    ax.text(11.5, 3.1, "read\ntools", fontsize=9.4, color=SLATE, ha="left", va="center", bbox=dict(boxstyle="round,pad=0.15", fc=WHITE, ec="none"))
    arrow(ax, 4.6, 0.95, 8.55, 0.95, "reads due bookings, records what it sent", ly=0.22)
    arrow(ax, 1.05, 1.6, 1.05, 2.95, "")
    arrow(ax, 1.05, 3.95, 1.05, 4.6, "")
    fig.tight_layout()
    fig.savefig(OUT / "fig_architecture.png", bbox_inches="tight", facecolor=WHITE)
    plt.close(fig)


def agent_loop():
    fig, ax = plt.subplots(figsize=(12.4, 4.9), dpi=200)
    ax.set_xlim(0, 12.4)
    ax.set_ylim(0, 4.9)
    ax.axis("off")
    steps = [("1  Model proposes", "one JSON action\n{action, argument}", TEAL, WHITE),
             ("2  Parse + validate", "known action? allowed for\nthis agent? arguments OK?", NAVY, WHITE),
             ("3  Python checks", "patient chose this slot?\nnamed date? registered?", NAVY, WHITE),
             ("4  Run the tool", "or the approval gate\n(a human decides)", AMBER, INK),
             ("5  Observation", "the real result goes back\nto the model as text", LIGHT, INK)]
    W, G = 2.3, 0.17
    xs = [0.15 + i * (W + G) for i in range(5)]
    for (t, sub, f, tc), x in zip(steps, xs):
        box(ax, x, 2.55, W, 1.45, t, sub, fill=f, edge=f if f != LIGHT else SLATE, tcolor=tc, fs=11)
    for a, b in zip(xs, xs[1:]):
        arrow(ax, a + W, 3.27, b, 3.27)
    arrow(ax, xs[4] + W / 2, 2.55, xs[0] + W / 2, 2.55, "the next proposal uses the observation (loop)", rad=-0.17, ly=-0.55)
    arrow(ax, xs[1] + W / 2, 4.0, xs[0] + W / 2, 4.0, "rejected: the reason goes back and the model retries", color="#B23A48", rad=0.45, ly=0.55)
    box(ax, 0.15, 0.15, 12.1, 1.05, "Stop conditions, decided by Python",
        "replied · booked_verified · cancelled_verified · rescheduled_verified · awaiting_approval\n"
        "front_desk · reply_corrected · too_many_invalid · max_steps · llm_error", fill=WHITE, fs=11)
    fig.tight_layout()
    fig.savefig(OUT / "fig_agent_loop.png", bbox_inches="tight", facecolor=WHITE)
    plt.close(fig)


def router_chart():
    fig, ax = plt.subplots(figsize=(6.4, 3.4), dpi=200)
    groups, rules, jev = ["Standard phrasings (24)", "Natural-language edge cases (23)"], [83, 13], [100, 100]
    import numpy as np
    x = np.arange(len(groups))
    b1 = ax.bar(x - 0.18, rules, 0.34, color="#AEB8C2", label="Keyword rules")
    b2 = ax.bar(x + 0.18, jev, 0.34, color=TEAL, label="Jev router")
    for bars in (b1, b2):
        for r in bars:
            ax.text(r.get_x() + r.get_width() / 2, r.get_height() + 2, f"{int(r.get_height())}%", ha="center", fontsize=9)
    ax.set_xticks(x, groups)
    ax.set_ylim(0, 112)
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    ax.set_ylabel("Intent, medical and skip-approval\nall correct (%)")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(frameon=False, loc="upper center", ncol=2, fontsize=9, bbox_to_anchor=(0.5, 1.13))
    fig.tight_layout()
    fig.savefig(OUT / "fig_router.png", bbox_inches="tight", facecolor=WHITE)
    plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    architecture()
    agent_loop()
    router_chart()
    print("figures written to", OUT)
