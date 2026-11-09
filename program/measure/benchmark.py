"""Score measurements against hand measurements, such as the lab's ImageJ spreadsheet.

Two questions are answered for every trait:

1. How good are the values? Error and bias against the hand value, for accepted values and for
   values sent to review separately.
2. Do the flags catch the bad cases? Of the values that are more than BAD_ERROR_PCT off, how many
   were sent to review; where the hand sheet says NA, how often no value was given; and whether each
   lower bound really is below the hand value.
"""
import csv
import math
import statistics
from datetime import datetime
from pathlib import Path

import pandas as pd

from config import MEASUREMENT_COLUMNS
from .decision import ACCEPT, LOWER_BOUND, NEEDS_REVIEW
from .records import load_measurements
from .taxa import DEFAULT_PROFILE, TRAIT_NAMES, TRAITS
from ..utils import index_images, is_blank, save_csv

REFERENCE_COLUMNS = dict(zip(TRAITS, MEASUREMENT_COLUMNS))
NOTE_COLUMNS = ("Flag", "Action", "Reason")
BENCHMARK_DIR_NAME = "benchmark"
BAD_ERROR_PCT = 20.0            # a value further than this from the hand value is a bad case


def _number(text):
    try:
        value = float(str(text).strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _find(header, name):
    lowered = [h.lower() for h in header]
    return lowered.index(name.lower()) if name.lower() in lowered else None


def load_reference(csv_path, key="gbifID", columns=None, profile=DEFAULT_PROFILE):
    """Read hand measurements as {id: {trait: cm or None, "note": text, "excluded": text}}.

    Uses the csv module with skipinitialspace so hand-edited files with padded quoted cells
    (`, "gbif image, no ruler"`) parse correctly. Values outside the plausible range for a
    trait are treated as data-entry errors: kept out of the score and listed under "excluded".
    """
    columns = columns or REFERENCE_COLUMNS
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle, skipinitialspace=True))
    if not rows:
        raise ValueError(f"{csv_path} is empty")
    header = [h.strip() for h in rows[0]]
    key_index = _find(header, key)
    if key_index is None:
        raise ValueError(f"{csv_path} has no {key} column")
    trait_index = {trait: _find(header, name) for trait, name in columns.items()}
    if all(i is None for i in trait_index.values()):
        raise ValueError(f"{csv_path} has none of the columns " + ", ".join(columns.values()))
    note_index = [i for i in (_find(header, n) for n in NOTE_COLUMNS) if i is not None]

    reference = {}
    for row in rows[1:]:
        cells = [c.strip() for c in row]
        if len(cells) <= key_index or not cells[key_index]:
            continue
        entry = {"note": "; ".join(cells[i] for i in note_index if i < len(cells) and cells[i]), "excluded": []}
        for trait, index in trait_index.items():
            value = _number(cells[index]) if index is not None and index < len(cells) else None
            low, high = profile.reference_cm[trait]
            if value is not None and not low <= value <= high:
                entry["excluded"].append(f"{TRAIT_NAMES[trait]} {value:g} cm")
                value = None
            entry[trait] = value
        entry["excluded"] = ", ".join(entry["excluded"])
        reference[cells[key_index]] = entry
    return reference


def _pearson(xs, ys):
    if len(xs) < 3:
        return None
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def _errors(records, trait):
    pct = [r[f"{trait}_error_pct"] for r in records]
    return {"n": len(records),
            "median_abs_error_pct": round(statistics.median(abs(p) for p in pct), 1) if pct else None,
            "mean_abs_error_cm": round(statistics.fmean(abs(r[f"{trait}_error_cm"]) for r in records), 3)
            if records else None,
            "bias_pct": round(statistics.fmean(pct), 1) if pct else None,
            "within_10pct": _pct(sum(abs(p) <= 10 for p in pct), len(pct)),
            "within_20pct": _pct(sum(abs(p) <= 20 for p in pct), len(pct))}


def score(reference, measurements):
    """Per-image comparison rows and a per-trait summary, both as DataFrames."""
    per_image, summary = [], []
    for gbif_id in sorted(set(reference) & set(measurements)):
        ref, row = reference[gbif_id], measurements[gbif_id]
        record = {"gbifID": gbif_id, "reference_note": ref["note"], "reference_excluded": ref["excluded"],
                  "ai_calibration": row.get("ai_calibration", ""), "ai_review": row.get("ai_review", "")}
        for trait in TRAITS:
            ai, hand = _number(row.get(f"ai_{trait}_cm", "")), ref[trait]
            bound = _number(row.get(f"ai_{trait}_lower_bound_cm", ""))
            record[f"{trait}_hand_cm"] = hand
            record[f"{trait}_ai_cm"] = ai
            record[f"{trait}_lower_bound_cm"] = bound
            record[f"{trait}_status"] = row.get(f"ai_{trait}_status", "")
            record[f"{trait}_confidence"] = _number(row.get(f"ai_{trait}_confidence", ""))
            record[f"{trait}_error_cm"] = None if ai is None or hand is None else round(ai - hand, 3)
            record[f"{trait}_error_pct"] = None if ai is None or hand is None else round(100.0 * (ai - hand) / hand, 1)
            record[f"{trait}_flags"] = row.get(f"ai_{trait}_flags", "")
        per_image.append(record)

    for trait in TRAITS:
        with_hand = [r for r in per_image if r[f"{trait}_hand_cm"] is not None]
        pairs = [r for r in with_hand if r[f"{trait}_ai_cm"] is not None]
        accepted = [r for r in pairs if r[f"{trait}_status"] == ACCEPT]
        review = [r for r in pairs if r[f"{trait}_status"] == NEEDS_REVIEW]
        bad = [r for r in pairs if abs(r[f"{trait}_error_pct"]) > BAD_ERROR_PCT]
        hand_na = [r for r in per_image if r[f"{trait}_hand_cm"] is None and not r["reference_excluded"]]
        bounds = [r for r in with_hand if r[f"{trait}_status"] == LOWER_BOUND
                  and r[f"{trait}_lower_bound_cm"] is not None]
        everything, clean, checked = _errors(pairs, trait), _errors(accepted, trait), _errors(review, trait)
        r_value = _pearson([r[f"{trait}_hand_cm"] for r in pairs], [r[f"{trait}_ai_cm"] for r in pairs])
        summary.append({
            "trait": TRAIT_NAMES[trait],
            "hand_measured": len(with_hand),
            "ai_measured": len(pairs),
            "coverage_pct": _pct(len(pairs), len(with_hand)),
            "median_abs_error_pct": everything["median_abs_error_pct"],
            "mean_abs_error_cm": everything["mean_abs_error_cm"],
            "bias_pct": everything["bias_pct"],
            "within_10pct": everything["within_10pct"],
            "within_20pct": everything["within_20pct"],
            "pearson_r": round(r_value, 3) if r_value is not None else None,
            "accepted": clean["n"],
            "accepted_median_abs_error_pct": clean["median_abs_error_pct"],
            "accepted_bias_pct": clean["bias_pct"],
            "accepted_within_10pct": clean["within_10pct"],
            "needs_review": checked["n"],
            "needs_review_median_abs_error_pct": checked["median_abs_error_pct"],
            "bad_values": len(bad),
            "bad_values_sent_to_review_pct": _pct(sum(r[f"{trait}_status"] == NEEDS_REVIEW for r in bad), len(bad)),
            "hand_NA": len(hand_na),
            "hand_NA_without_ai_value_pct": _pct(sum(r[f"{trait}_ai_cm"] is None for r in hand_na), len(hand_na)),
            "lower_bounds": len(bounds),
            "lower_bounds_below_hand_pct": _pct(sum(r[f"{trait}_lower_bound_cm"] <= r[f"{trait}_hand_cm"] * 1.05
                                                    for r in bounds), len(bounds)),
        })
    return pd.DataFrame(per_image), pd.DataFrame(summary)


def format_summary(summary):
    lines = []
    for row in summary.to_dict("records"):
        if not row["ai_measured"]:
            lines.append(f"  {row['trait']}: no values to compare ({row['hand_measured']} hand-measured)")
            continue
        line = (f"  {row['trait']}: {row['ai_measured']} of {row['hand_measured']} measured "
                f"({row['coverage_pct']:g}%), median error {row['median_abs_error_pct']:g}%, "
                f"bias {row['bias_pct']:+g}%, within 10%: {row['within_10pct']:g}%, "
                f"within 20%: {row['within_20pct']:g}%")
        if pd.notna(row["pearson_r"]):
            line += f", r = {row['pearson_r']:g}"
        lines.append(line)
        if row["accepted"]:
            lines.append(f"      accepted: {row['accepted']} values, median error "
                         f"{row['accepted_median_abs_error_pct']:g}%, bias {row['accepted_bias_pct']:+g}%")
        if row["needs_review"]:
            lines.append(f"      needs review: {row['needs_review']} values, median error "
                         f"{row['needs_review_median_abs_error_pct']:g}%")
        if row["bad_values"]:
            lines.append(f"      flags: {row['bad_values_sent_to_review_pct']:g}% of the {row['bad_values']} values "
                         f"more than {BAD_ERROR_PCT:g}% off were sent to review")
        if row["hand_NA"]:
            lines.append(f"      where the hand sheet says NA ({row['hand_NA']}): no value given in "
                         f"{row['hand_NA_without_ai_value_pct']:g}%")
        if row["lower_bounds"]:
            lines.append(f"      lower bounds: {row['lower_bounds']}, of which "
                         f"{row['lower_bounds_below_hand_pct']:g}% are below the hand value")
    return "\n".join(lines)


def run_benchmark(reference_csv, media_dir, output_dir, measurements_csv=None, measure=False, only_ids=None,
                  label="", log=print, reference_key="gbifID", **measure_options):
    """Measure (optionally) the hand-measured images, then score them against the reference.

    With measure=True the images are measured into <output_dir>/<run>/measurements.csv so the
    working measurements file is never overwritten. Otherwise an existing measurements CSV is scored.
    """
    from .pipeline import measure_images

    reference = load_reference(reference_csv, key=reference_key)
    images = index_images(media_dir)
    ids = set(reference) & set(images)
    if only_ids:
        ids &= set(only_ids)
    if not ids:
        raise ValueError(f"None of the {len(reference)} hand-measured records have an image in {media_dir}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(output_dir) / (f"{stamp}_{label}" if label else stamp)
    run_dir.mkdir(parents=True, exist_ok=True)
    if measure:
        measurements_csv = run_dir / "measurements.csv"
        log(f"Measuring {len(ids)} hand-measured images into {measurements_csv}")
        measure_options.setdefault("read_labels", False)   # only the traits are scored
        result = measure_images(media_dir, measurements_csv, log=log, only_ids=ids, force=True, **measure_options)
        if result.get("cancelled"):
            log("Benchmark cancelled; scoring the images measured so far")
    elif not measurements_csv or not Path(measurements_csv).is_file():
        raise FileNotFoundError(f"Measurements CSV not found: {measurements_csv}")

    measurements = {k: v for k, v in load_measurements(measurements_csv).items()
                    if k in ids and is_blank(v.get("ai_error"))}
    per_image, summary = score({k: reference[k] for k in ids}, measurements)
    save_csv(per_image, run_dir / "per_image.csv")
    save_csv(summary, run_dir / "summary.csv")
    log(f"Benchmark against {Path(reference_csv).name}: {len(measurements)} of {len(ids)} hand-measured images "
        f"have results")
    log(format_summary(summary))
    log(f"Details saved to {run_dir}")
    return {"run_dir": str(run_dir), "summary": summary, "per_image": per_image}
