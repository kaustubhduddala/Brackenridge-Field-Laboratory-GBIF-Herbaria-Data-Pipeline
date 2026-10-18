"""Score AI measurements against hand measurements, such as the lab's ImageJ spreadsheet."""
import csv
import math
import statistics
from datetime import datetime
from pathlib import Path

import pandas as pd

from config import MEASUREMENT_COLUMNS
from .analysis import TRAIT_BOUNDS_CM, TRAITS, load_measurements, measure_images
from .utils import index_images, is_blank, save_csv

REFERENCE_COLUMNS = dict(zip(TRAITS, MEASUREMENT_COLUMNS))
NOTE_COLUMNS = ("Flag", "Action", "Reason")
LABELS = {"panicle_length": "Panicle length", "leaf_width": "Leaf width", "seed_length": "Seed length"}
BENCHMARK_DIR_NAME = "benchmark"


def _number(text):
    try:
        value = float(str(text).strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _find(header, name):
    lowered = [h.lower() for h in header]
    return lowered.index(name.lower()) if name.lower() in lowered else None


def load_reference(csv_path, key="gbifID", columns=None):
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
            low, high = TRAIT_BOUNDS_CM[trait]
            if value is not None and not low <= value <= high:
                entry["excluded"].append(f"{LABELS[trait]} {value:g} cm")
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


def score(reference, measurements):
    """Per-image comparison rows and a per-trait summary, both as DataFrames."""
    per_image, summary = [], []
    shared = sorted(set(reference) & set(measurements))
    for gbif_id in shared:
        ref, row = reference[gbif_id], measurements[gbif_id]
        record = {"gbifID": gbif_id, "reference_note": ref["note"], "reference_excluded": ref["excluded"],
                  "ai_calibration": row.get("ai_calibration", ""), "ai_error": row.get("ai_error", "")}
        for trait in TRAITS:
            ai = _number(row.get(f"ai_{trait}_mean_cm", ""))
            hand = ref[trait]
            record[f"{trait}_hand_cm"] = hand
            record[f"{trait}_ai_cm"] = ai
            record[f"{trait}_error_cm"] = None if ai is None or hand is None else round(ai - hand, 3)
            record[f"{trait}_error_pct"] = (None if ai is None or hand is None
                                           else round(100.0 * (ai - hand) / hand, 1))
        per_image.append(record)

    for trait in TRAITS:
        with_hand = [r for r in per_image if r[f"{trait}_hand_cm"] is not None]
        pairs = [r for r in with_hand if r[f"{trait}_ai_cm"] is not None]
        extra = sum(1 for r in per_image if r[f"{trait}_hand_cm"] is None and r[f"{trait}_ai_cm"] is not None
                    and not r["reference_excluded"])
        abs_cm = [abs(r[f"{trait}_error_cm"]) for r in pairs]
        pct = [r[f"{trait}_error_pct"] for r in pairs]
        abs_pct = [abs(p) for p in pct]
        r_value = _pearson([r[f"{trait}_hand_cm"] for r in pairs], [r[f"{trait}_ai_cm"] for r in pairs])
        summary.append({
            "trait": LABELS[trait],
            "hand_measured": len(with_hand),
            "ai_measured": len(pairs),
            "coverage_pct": round(100.0 * len(pairs) / len(with_hand), 1) if with_hand else None,
            "ai_value_where_hand_NA": extra,
            "mean_abs_error_cm": round(statistics.fmean(abs_cm), 3) if abs_cm else None,
            "median_abs_error_pct": round(statistics.median(abs_pct), 1) if abs_pct else None,
            "mean_abs_error_pct": round(statistics.fmean(abs_pct), 1) if abs_pct else None,
            "bias_pct": round(statistics.fmean(pct), 1) if pct else None,
            "within_10pct": round(100.0 * sum(p <= 10 for p in abs_pct) / len(abs_pct), 1) if abs_pct else None,
            "within_20pct": round(100.0 * sum(p <= 20 for p in abs_pct) / len(abs_pct), 1) if abs_pct else None,
            "pearson_r": round(r_value, 3) if r_value is not None else None,
        })
    return pd.DataFrame(per_image), pd.DataFrame(summary)


def _format_summary(summary):
    lines = []
    for row in summary.to_dict("records"):
        if not row["ai_measured"]:
            lines.append(f"  {row['trait']}: no AI values to compare ({row['hand_measured']} hand-measured)")
            continue
        lines.append(
            f"  {row['trait']}: {row['ai_measured']} of {row['hand_measured']} measured "
            f"({row['coverage_pct']:g}%), median error {row['median_abs_error_pct']:g}%, "
            f"mean error {row['mean_abs_error_cm']:g} cm, bias {row['bias_pct']:+g}%, "
            f"within 10%: {row['within_10pct']:g}%, within 20%: {row['within_20pct']:g}%"
            + (f", r = {row['pearson_r']:g}" if pd.notna(row["pearson_r"]) else "")
            + (f"; {row['ai_value_where_hand_NA']} AI values where the hand sheet says NA"
               if row["ai_value_where_hand_NA"] else ""))
    return "\n".join(lines)


def run_benchmark(reference_csv, media_dir, output_dir, measurements_csv=None, measure=False, only_ids=None,
                  label="", log=print, reference_key="gbifID", **measure_options):
    """Measure (optionally) the hand-measured images, then score them against the reference.

    With measure=True the images are measured into <output_dir>/<run>/measurements.csv so the
    working measurements file is never overwritten. Otherwise an existing measurements CSV is scored.
    """
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
        f"have AI results")
    log(_format_summary(summary))
    log(f"Details saved to {run_dir}")
    return {"run_dir": str(run_dir), "summary": summary, "per_image": per_image}
