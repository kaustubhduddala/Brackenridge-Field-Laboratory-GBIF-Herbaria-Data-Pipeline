"""Plot verified vs measured values from a CSV: one column per trait, a scatter on top and an error histogram below.

Input: one row per sheet, with a verified (hand) column and a measured column for each trait, as in
example_old_system.csv:

    sheet_id, panicle_length_verified_cm, panicle_length_old_system_cm, leaf_width_verified_cm,
    leaf_width_old_system_cm, seed_length_verified_cm, seed_length_old_system_cm, simulated

An empty measured value means no number was given for that sheet. Any <trait>_verified_cm column is paired
with <trait>_old_system_cm. The per_image.csv written by `python main.py benchmark ...` also works
(<trait>_hand_cm with <trait>_ai_cm); pass --measured-label "New method" to relabel the figure.
If any row has simulated=True the figure is stamped SIMULATED; drop that column for real data.

Run:
    python figures/plot_actual_vs_model.py figures/example_old_system.csv

Options: --out PREFIX (default: next to the CSV), --title, --note "extra footnote", --measured-label, --no-pdf.
Needs pandas, numpy and matplotlib.
"""
import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

INK, MUTED, GRID = "#1b2420", "#5c6962", "#e3e8e4"
POINT, BAND, WARN = "#2d6a4f", "#d5e9dd", "#b45309"
BANNER_FACE, BANNER_EDGE, BANNER_INK = "#fff3cd", "#c99700", "#6b4300"
UNIT = "cm"
BAND_PCT = 20.0                  # "close enough": within this % of the verified value
MIN_PAIRS = 3                    # a trait with fewer measured pairs than this is skipped
HIST_RANGE = (-100, 170)         # errors outside are drawn in the end bins
TRUTH_SUFFIXES = ("_verified_cm", "_hand_cm")
MEASURED_SUFFIXES = ("_old_system_cm", "_ai_cm", "_model_cm")


# --------------------------------------------------------------------------- loading

def nice(trait):
    text = str(trait).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def load(path):
    """(table with columns trait, verified, measured; simulated: bool)."""
    df = pd.read_csv(path)
    frames = []
    for column in df.columns:
        suffix = next((x for x in TRUTH_SUFFIXES if column.endswith(x)), None)
        if not suffix:
            continue
        stem = column[:-len(suffix)]
        measured = next((f"{stem}{x}" for x in MEASURED_SUFFIXES if f"{stem}{x}" in df.columns), None)
        if measured:
            frames.append(pd.DataFrame({"trait": nice(stem), "verified": df[column], "measured": df[measured]}))
    if not frames:
        raise SystemExit(f"{path} needs <trait>_verified_cm and <trait>_old_system_cm columns "
                         "(or <trait>_hand_cm and <trait>_ai_cm from the benchmark).")
    table = pd.concat(frames, ignore_index=True)
    simulated = ("simulated" in df.columns
                 and df["simulated"].astype(str).str.strip().str.lower().isin(("true", "1", "yes")).any())
    table["verified"] = pd.to_numeric(table["verified"], errors="coerce")
    table["measured"] = pd.to_numeric(table["measured"], errors="coerce")
    return table.dropna(subset=["verified"]).reset_index(drop=True), bool(simulated)


# --------------------------------------------------------------------------- statistics

def error_pct(verified, measured):
    return (measured - verified) / verified * 100


def summary_table(table):
    """One row per trait: the numbers behind the figure."""
    rows = []
    for trait, group in table.groupby("trait", sort=False):
        kept = group.dropna(subset=["measured"])
        row = {"trait": trait, "sheets_verified": len(group), "measured_values": len(kept),
               "coverage_pct": round(100 * len(kept) / len(group), 1)}
        if len(kept) >= MIN_PAIRS:
            error = error_pct(kept["verified"], kept["measured"])
            row.update(r=round(float(np.corrcoef(kept["verified"], kept["measured"])[0, 1]), 3),
                       median_abs_error_pct=round(float(error.abs().median()), 1),
                       bias_pct=round(float(error.mean()), 1),
                       within_20pct=round(float((error.abs() <= BAND_PCT).mean() * 100), 1),
                       bias_cm=round(float((kept["measured"] - kept["verified"]).mean()), 3))
        rows.append(row)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- drawing

def style(ax, grid_axis="both"):
    ax.set_facecolor("white")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=11, length=3)
    ax.grid(True, axis=grid_axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.xaxis.label.set_color(MUTED)
    ax.yaxis.label.set_color(MUTED)


def draw(table, simulated, title, note, measured_label):
    groups = [(trait, g) for trait, g in table.groupby("trait", sort=False)
              if g["measured"].notna().sum() >= MIN_PAIRS]
    skipped = [t for t in table["trait"].unique() if t not in {g[0] for g in groups}]
    if not groups:
        raise SystemExit("No trait has at least %d measured pairs to plot." % MIN_PAIRS)
    cols = len(groups)
    fig = plt.figure(figsize=(4.15 * cols + 0.6, 7.6))
    grid = fig.add_gridspec(2, cols, height_ratios=(1.45, 1), left=0.075, right=0.985, top=0.80, bottom=0.135,
                            hspace=0.50, wspace=0.30)

    for col, (trait, group) in enumerate(groups):
        kept = group.dropna(subset=["measured"])
        verified, measured = kept["verified"].to_numpy(), kept["measured"].to_numpy()
        error = error_pct(verified, measured)
        bias, within = float(error.mean()), float((np.abs(error) <= BAND_PCT).mean() * 100)

        # ---- verified vs measured
        ax = fig.add_subplot(grid[0, col])
        style(ax)
        top = max(verified.max(), measured.max()) * 1.06
        line = np.array([0, top])
        ax.fill_between(line, line * (1 - BAND_PCT / 100), line * (1 + BAND_PCT / 100), color=BAND, linewidth=0,
                        zorder=0)
        ax.plot(line, line, color=MUTED, linewidth=1.2, linestyle="--", zorder=1)
        ax.scatter(verified, measured, s=30, color=POINT, alpha=0.65, edgecolor="white", linewidth=0.6, zorder=2)
        ax.set(xlim=(0, top), ylim=(0, top), xlabel=f"Verified ({UNIT})", ylabel=f"{measured_label} ({UNIT})")
        ax.set_aspect("equal", adjustable="box")
        ax.set_anchor("W")
        ax.set_title(trait, loc="left", fontsize=16, fontweight="bold", color=INK, pad=26)
        ax.text(0, 1.035, f"{len(kept)} of {len(group)} measured  ·  {within:.0f}% within ±{BAND_PCT:.0f}%",
                transform=ax.transAxes, ha="left", va="bottom", fontsize=11.5, color=MUTED)

        # ---- error distribution
        ax = fig.add_subplot(grid[1, col])
        style(ax, "y")
        bins = np.arange(HIST_RANGE[0], HIST_RANGE[1] + 1, 10)
        ax.axvspan(-BAND_PCT, BAND_PCT, color=BAND, zorder=0)
        ax.hist(np.clip(error, *HIST_RANGE), bins=bins, color=POINT, edgecolor="white", linewidth=1.2, zorder=2)
        ax.axvline(bias, color=WARN, linewidth=2.2, linestyle="--", zorder=3)
        ax.set(xlim=HIST_RANGE, xlabel=f"{measured_label} error (%)", ylabel="Sheets")
        ax.set_xticks([-100, -50, 0, 50, 100, 150])
        ax.text(bias, 1.04, f"bias {bias:+.0f}%", transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                fontsize=11.5, fontweight="bold", color=WARN)

    if simulated:
        fig.text(0.975, 0.955, "SIMULATED EXAMPLE  ·  not measured data", ha="right", va="center", fontsize=12.5,
                 fontweight="bold", color=BANNER_INK,
                 bbox={"facecolor": BANNER_FACE, "edgecolor": BANNER_EDGE, "linewidth": 1.2,
                       "boxstyle": "round,pad=0.4"})
    fig.text(0.075, 0.955, title, ha="left", va="center", fontsize=18, fontweight="bold", color=INK)
    key = (f"Shaded band: within ±{BAND_PCT:.0f}% of the verified value   ·   dashed line: perfect agreement   ·   "
           "orange line: average error")
    if skipped:
        key += f"   ·   not plotted (too few measured sheets): {', '.join(skipped)}"
    fig.text(0.075, 0.055, key, ha="left", va="center", fontsize=10.5, color=MUTED)
    if note:
        fig.text(0.075, 0.022, note, ha="left", va="center", fontsize=10.5, color=MUTED)
    return fig


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("csv", type=Path, help="CSV with <trait>_verified_cm and <trait>_old_system_cm columns")
    parser.add_argument("--out", type=Path, help="output path without extension (default: next to the CSV)")
    parser.add_argument("--title", help="figure title")
    parser.add_argument("--note", default="", help="extra footnote line")
    parser.add_argument("--measured-label", default="Old system", help="name of the measured column in the figure")
    parser.add_argument("--no-pdf", action="store_true", help="write only the PNG")
    args = parser.parse_args()

    table, simulated = load(args.csv)
    title = args.title or f"{args.measured_label} vs verified measurements"
    fig = draw(table, simulated, title, args.note.replace("\\n", " "), args.measured_label)

    base = args.out or args.csv.with_name(args.csv.stem + "_plots")
    fig.savefig(base.with_suffix(".png"), dpi=300, facecolor="white")
    written = [base.with_suffix(".png")]
    if not args.no_pdf:
        fig.savefig(base.with_suffix(".pdf"), facecolor="white")
        written.append(base.with_suffix(".pdf"))
    summary = summary_table(table)
    summary.to_csv(base.with_name(base.name + "_summary.csv"), index=False)
    print(summary.to_string(index=False))
    print("wrote " + ", ".join(str(p) for p in written) + f" and {base.name}_summary.csv")


if __name__ == "__main__":
    main()
