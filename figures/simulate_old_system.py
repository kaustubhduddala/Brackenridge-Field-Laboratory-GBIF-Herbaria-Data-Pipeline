"""Write an EXAMPLE CSV: 200 sheets, each with the verified (hand) measurement and what the old LLM-only
measuring method would have given.

None of these numbers were measured. They are drawn at random from distributions whose shape follows the
failure patterns seen in the one real benchmark of the old system (10 hand-measured sheets, 2026-10-18):

    panicle  80% coverage, median error 25%, bias -33%, only half within 20%   -> followed
    seed     30% coverage, bias -22%, none within 20%                           -> followed
    leaf     0 of 10 measured in the real benchmark                             -> ASSUMED (see make_leaf)

The hand-measured values are drawn from the range of the lab's 163 real hand measurements
(panicle 8.0-60.7 cm, leaf 0.38-3.79 cm, seed 0.25-0.61 cm).

One row per sheet. Columns: sheet_id, then <trait>_verified_cm and <trait>_old_system_cm for the three traits,
then simulated. An empty old_system value means the old method gave no number for that sheet.
Every row has simulated=True, which makes plot_actual_vs_model.py stamp the figure SIMULATED.

Run:  python figures/simulate_old_system.py [--n 200] [--seed 2026] [--out figures/example_old_system.csv]
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent


def lognormal_clipped(rng, n, median, sigma, low, high):
    return np.clip(rng.lognormal(np.log(median), sigma, n), low, high)


def panicle(rng, n):
    hand = lognormal_clipped(rng, n, 24.0, 0.42, 8.0, 60.7)
    # Half the time the model's tip lands part-way along the panicle (short); otherwise it is close.
    close = rng.random(n) < 0.5
    ratio = np.where(close, rng.normal(0.95, 0.12, n), rng.uniform(0.2, 0.7, n))
    return hand, hand * ratio, rng.random(n) < 0.20          # last: share of sheets with no model value


def leaf(rng, n):
    # ASSUMPTION (no real benchmark data): bent or folded leaves read wide; 40% of sheets give no value.
    hand = lognormal_clipped(rng, n, 1.4, 0.40, 0.38, 3.79)
    return hand, hand * rng.lognormal(np.log(1.15), 0.35, n), rng.random(n) < 0.40


def seed(rng, n):
    hand = lognormal_clipped(rng, n, 0.42, 0.18, 0.25, 0.61)
    model = np.clip(hand * rng.lognormal(np.log(0.78), 0.15, n), 0.12, 0.8)
    return hand, model, rng.random(n) < 0.70


TRAITS = (("panicle_length", panicle), ("leaf_width", leaf), ("seed_length", seed))


def simulate(n, seed_value):
    rng = np.random.default_rng(seed_value)
    table = pd.DataFrame({"sheet_id": [f"sim-{i + 1:03d}" for i in range(n)]})
    for name, make in TRAITS:
        verified, old, missing = make(rng, n)
        table[f"{name}_verified_cm"] = np.round(verified, 3)
        table[f"{name}_old_system_cm"] = np.where(missing, np.nan, np.round(old, 3))
    table["simulated"] = True
    return table


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--n", type=int, default=200, help="synthetic sheets per trait")
    parser.add_argument("--seed", type=int, default=2026, help="random seed")
    parser.add_argument("--out", type=Path, default=HERE / "example_old_system.csv")
    args = parser.parse_args()
    table = simulate(args.n, args.seed)
    table.to_csv(args.out, index=False)
    print(f"wrote {len(table)} sheets to {args.out}")


if __name__ == "__main__":
    main()
