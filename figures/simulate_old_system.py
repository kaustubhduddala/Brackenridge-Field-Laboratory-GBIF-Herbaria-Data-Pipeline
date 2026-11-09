"""SIMULATED illustration of the old LLM-only measuring method: hand-measured vs model values for 200 sheets.

None of these numbers were measured. They are drawn at random from distributions whose shape follows the
failure patterns seen in the one real benchmark of the old system (10 hand-measured sheets, 2026-10-18):

    panicle  80% coverage, median error 25%, bias -33%, only half within 20%   -> used
    seed     30% coverage, bias -22%, none within 20%                           -> used
    leaf     0 of 10 measured in the real benchmark                             -> ASSUMED (see LEAF below)

The hand-measured ("actual") values are drawn from the range of the lab's 163 real hand measurements
(panicle 8.0-60.7 cm, leaf 0.38-3.79 cm, seed 0.25-0.61 cm).

Every figure is stamped SIMULATED. Run:  python figures/simulate_old_system.py
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

OUT = Path(__file__).resolve().parent
N, SEED = 200, 2026
rng = np.random.default_rng(SEED)

INK, MUTED, GRID = "#1b2420", "#5c6962", "#d9dfda"
POINT, BAND, WARN = "#2d6a4f", "#cfe5d8", "#b45309"


def hand_values(median, sigma, low, high):
    return np.clip(rng.lognormal(np.log(median), sigma, N), low, high)


def make_panicle():
    hand = hand_values(24.0, 0.42, 8.0, 60.7)
    # Half the time the model's tip lands part-way along the panicle (short); otherwise it is close.
    close = rng.random(N) < 0.5
    ratio = np.where(close, rng.normal(0.95, 0.12, N), rng.uniform(0.2, 0.7, N))
    return hand, hand * ratio, rng.random(N) < 0.20


def make_leaf():
    # ASSUMPTION (no real benchmark data): bent or folded leaves read wide; 40% of sheets give no value.
    hand = hand_values(1.4, 0.40, 0.38, 3.79)
    return hand, hand * rng.lognormal(np.log(1.15), 0.35, N), rng.random(N) < 0.40


def make_seed():
    hand = hand_values(0.42, 0.18, 0.25, 0.61)
    model = np.clip(hand * rng.lognormal(np.log(0.78), 0.15, N), 0.12, 0.8)
    return hand, model, rng.random(N) < 0.70


TRAITS = [("Panicle length", "cm", make_panicle, 1), ("Leaf width", "cm", make_leaf, 2),
          ("Seed length", "cm", make_seed, 3)]


def stats(hand, model):
    err = (model - hand) / hand * 100
    return {"n": len(hand), "r": float(np.corrcoef(hand, model)[0, 1]), "median_abs_pct": float(np.median(np.abs(err))),
            "bias_pct": float(err.mean()), "within20": float((np.abs(err) <= 20).mean() * 100)}, err


def style(ax):
    ax.set_facecolor("white")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=11)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def main():
    fig, axes = plt.subplots(3, 3, figsize=(14, 11.2))
    fig.subplots_adjust(left=0.075, right=0.985, top=0.775, bottom=0.115, hspace=0.62, wspace=0.28)
    rows = []
    for row, (name, unit, make, _) in enumerate(TRAITS):
        hand_all, model_all, missing = make()
        keep = ~missing
        hand, model = hand_all[keep], model_all[keep]
        s, err = stats(hand, model)
        coverage = keep.mean() * 100
        rows += [{"sim_id": i + 1, "trait": name, "hand_cm": round(h, 3),
                  "model_cm": "" if miss else round(m, 3), "simulated": True}
                 for i, (h, m, miss) in enumerate(zip(hand_all, model_all, missing))]

        # ---- actual vs model
        ax = axes[row, 0]
        style(ax)
        top = max(hand.max(), model.max()) * 1.06
        ax.plot([0, top], [0, top], color=MUTED, linewidth=1.2, linestyle="--", zorder=1)
        ax.scatter(hand, model, s=26, color=POINT, alpha=0.6, edgecolor="white", linewidth=0.5, zorder=2)
        ax.set(xlim=(0, top), ylim=(0, top), xlabel=f"Hand-measured ({unit})", ylabel=f"{name}\nModel ({unit})")
        ax.yaxis.label.set_color(INK)
        ax.xaxis.label.set_color(MUTED)
        ax.set_aspect("equal", adjustable="box")
        ax.set_title(f"n = {s['n']} of {N} measured ({coverage:.0f}%)   r = {s['r']:.2f}", fontsize=11, color=INK,
                     loc="left")

        # ---- error distribution
        ax = axes[row, 1]
        style(ax)
        bins = np.arange(-100, 171, 10)
        ax.axvspan(-20, 20, color=BAND, zorder=0)
        ax.hist(np.clip(err, -100, 170), bins=bins, color=POINT, edgecolor="white", linewidth=1.2, zorder=2)
        ax.axvline(0, color=INK, linewidth=1.0, zorder=3)
        ax.axvline(s["bias_pct"], color=WARN, linewidth=2.0, linestyle="--", zorder=3)
        ax.set(xlabel="Model error (% of hand value)", ylabel="Sheets", xlim=(-100, 170))
        ax.xaxis.label.set_color(MUTED)
        ax.yaxis.label.set_color(MUTED)
        ax.set_title(f"bias {s['bias_pct']:+.0f}%   median |error| {s['median_abs_pct']:.0f}%   "
                     f"within ±20%: {s['within20']:.0f}%", fontsize=11, color=INK, loc="left")

        # ---- difference vs mean
        ax = axes[row, 2]
        style(ax)
        mean, diff = (hand + model) / 2, model - hand
        bias, spread = diff.mean(), 1.96 * diff.std(ddof=1)
        ax.axhline(0, color=INK, linewidth=1.0)
        for level, dashed in ((bias, False), (bias + spread, True), (bias - spread, True)):
            ax.axhline(level, color=WARN, linewidth=1.6, linestyle="--" if dashed else "-")
        ax.scatter(mean, diff, s=26, color=POINT, alpha=0.6, edgecolor="white", linewidth=0.5, zorder=2)
        ax.set(xlabel=f"Mean of hand and model ({unit})", ylabel=f"Model − hand ({unit})")
        ax.xaxis.label.set_color(MUTED)
        ax.yaxis.label.set_color(MUTED)
        ax.set_title(f"bias {bias:+.2f} {unit}   limits {bias - spread:+.2f} to {bias + spread:+.2f}", fontsize=11,
                     color=INK, loc="left")

    fig.text(0.5, 0.965, "SIMULATED DATA: illustrative example of the old measuring method, not measured values",
             ha="center", va="center", fontsize=16, fontweight="bold", color="#6b4300",
             bbox={"facecolor": "#fff3cd", "edgecolor": "#c99700", "linewidth": 1.5, "boxstyle": "round,pad=0.45"})
    fig.text(0.075, 0.915, f"Hand-measured vs model values for {N} synthetic sheets, per trait",
             ha="left", va="center", fontsize=15, fontweight="bold", color=INK)
    for col, (head, note) in enumerate((("Actual vs model", "dashed line = perfect agreement"),
                                        ("Error distribution", "green band = within ±20%; orange = mean bias"),
                                        ("Bias and agreement", "Bland–Altman: orange = mean and ±1.96 SD"))):
        x = axes[0, col].get_position().x0
        fig.text(x, 0.872, head, ha="left", va="center", fontsize=13, fontweight="bold", color=INK)
        fig.text(x, 0.850, note, ha="left", va="center", fontsize=10.5, color=MUTED)

    fig.text(0.075, 0.045,
             f"Simulated (n = {N} synthetic sheets per trait, random seed {SEED}); no real sheets were measured for this figure. "
             "Panicle and seed behaviour follow the failure pattern\nobserved in a real 10-sheet benchmark of the old "
             "LLM-only method (panicle: bias −33%, median error 25%; seed: 30% measured, bias −22%). "
             "Leaf behaviour is assumed.\nHand-measured values are drawn from the range of the lab's 163 real hand "
             "measurements of Megathyrsus maximus.",
             ha="left", va="center", fontsize=10, color=MUTED, linespacing=1.5)

    fig.savefig(OUT / "SIMULATED_old_system_actual_vs_model.png", dpi=300, facecolor="white")
    fig.savefig(OUT / "SIMULATED_old_system_actual_vs_model.pdf", facecolor="white")
    pd.DataFrame(rows).to_csv(OUT / "SIMULATED_old_system_data.csv", index=False)
    print("saved figure (png, pdf) and SIMULATED_old_system_data.csv in", OUT)


if __name__ == "__main__":
    main()
