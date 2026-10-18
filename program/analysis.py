"""The measurements CSV: its columns, reading and writing it, and joining it into the dataset.

Measuring itself is done by measure.py (per-sheet pipeline in pipeline.py).
"""
from pathlib import Path

import pandas as pd

from config import RESPONSES_DIR_NAME
from .providers import check_connection as provider_check_connection
from .utils import index_images, is_blank, read_csv, save_csv

TRAITS = ("panicle_length", "leaf_width", "seed_length")
FIELDS = [f"{trait}_{n}_{kind}" for trait in TRAITS for n in (1, 2) for kind in ("cm", "confidence")]
VOUCHER_FIELDS = ["catalogNumber", "recordedBy", "recordNumber", "eventDate", "country", "stateProvince",
                  "county", "locality", "habitat", "verbatimElevation", "identifiedBy", "scientificName"]

TRAIT_BOUNDS_CM = {
    "panicle_length": (3.0, 75.0),
    "leaf_width": (0.15, 5.0),
    "seed_length": (0.15, 0.65),
}

RESULT_COLUMNS = (
    ["ai_image"]
    + [f"ai_{field}" for field in FIELDS]
    + [f"ai_{trait}_mean_cm" for trait in TRAITS]
    + ["ai_calibration", "ai_notes", "voucher_text"]
    + [f"voucher_{field}" for field in VOUCHER_FIELDS]
    + ["ai_model", "ai_measured_at", "ai_error"]
)
MEASUREMENT_FILE_COLUMNS = ["gbifID", *RESULT_COLUMNS]


def load_measurements(csv_path):
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if "gbifID" not in df.columns:
        raise ValueError(f"{csv_path} has no gbifID column")
    return {str(r["gbifID"]).strip(): r for r in df.to_dict("records")}


def save_measurements(rows, csv_path):
    df = pd.DataFrame(list(rows.values()))
    extra = [c for c in df.columns if c not in MEASUREMENT_FILE_COLUMNS]
    save_csv(df.reindex(columns=MEASUREMENT_FILE_COLUMNS + extra).fillna(""), csv_path)


def is_done(row):
    return bool(row) and not is_blank(row.get("ai_measured_at")) and is_blank(row.get("ai_error"))


def describe(row):
    parts = []
    for trait, label in zip(TRAITS, ("panicle", "leaf", "seed")):
        value = row.get(f"ai_{trait}_mean_cm")
        parts.append(f"{label} {value} cm" if not is_blank(value) else f"{label} -")
    return ", ".join(parts)


def measurement_status(media_dir, csv_path):
    rows = load_measurements(csv_path)
    responses_dir = Path(csv_path).parent / RESPONSES_DIR_NAME
    items = []
    for gbif_id, image in index_images(media_dir).items():
        row = rows.get(gbif_id)
        if is_done(row):
            status, detail = "Measured", describe(row)
        elif row and not is_blank(row.get("ai_error")):
            status, detail = "Failed", str(row["ai_error"])
        else:
            status, detail = "Not measured", ""
        notes = "" if not row else "\n".join(
            str(row.get(k)) for k in ("ai_calibration", "ai_notes", "voucher_text") if not is_blank(row.get(k)))
        response = responses_dir / f"{gbif_id}.json"
        items.append({"id": gbif_id, "status": status, "detail": detail, "image": image, "notes": notes,
                      "response": response if response.exists() else None})
    return items


check_connection = provider_check_connection


def csv_columns(csv_path):
    return [str(c).strip() for c in pd.read_csv(csv_path, nrows=0, encoding="utf-8-sig").columns]


def _key_values(csv_path, column):
    values = pd.read_csv(csv_path, usecols=[column], dtype=str, encoding="utf-8-sig")[column]
    return values.fillna("").str.strip()


def count_matches(master_csv, master_key, measurements_csv, measurement_key):
    master_ids = _key_values(master_csv, master_key)
    measured = set(_key_values(measurements_csv, measurement_key)) - {""}
    return int(master_ids.isin(measured).sum()), len(master_ids), len(measured)


def join_measurements(master_csv, measurements_csv, log=print, fill_blanks=False,
                      master_key="gbifID", measurement_key="gbifID", master_columns=None,
                      measurement_columns=None, output_csv=None, matched_only=False):
    master_csv, measurements_csv = Path(master_csv), Path(measurements_csv)
    if not measurements_csv.exists():
        raise FileNotFoundError(f"{measurements_csv} does not exist yet. Measure some images first.")
    master = read_csv(master_csv, log)
    master.columns = master.columns.str.strip()
    measurements = read_csv(measurements_csv, log)
    measurements.columns = measurements.columns.str.strip()
    for name, df, key in (("dataset", master, master_key), ("measurements", measurements, measurement_key)):
        if key not in df.columns:
            raise ValueError(f"The {name} CSV has no column named {key}")

    master_ids = master[master_key].fillna("").astype(str).str.strip()
    measurement_ids = measurements[measurement_key].fillna("").astype(str).str.strip()
    measurements = (measurements.assign(_join_key=measurement_ids)[measurement_ids != ""]
                    .drop_duplicates("_join_key", keep="last").set_index("_join_key"))

    if measurement_columns is None:
        added = [c for c in RESULT_COLUMNS if c in measurements.columns]
    else:
        added = [c for c in measurement_columns if c in measurements.columns and c != measurement_key]
    kept = list(master.columns) if master_columns is None else [c for c in master.columns if c in set(master_columns)]
    if master_key not in kept:
        kept.insert(0, master_key)
    replaced = [c for c in added if c in kept and c != master_key]
    added = [c for c in added if c != master_key]
    if replaced:
        log(f"  {len(replaced)} columns exist in both files; the measurement values are used for: "
            + ", ".join(replaced[:6]) + (" and more" if len(replaced) > 6 else ""))

    had_fills = "voucher_filled_columns" in master.columns
    fills = (master["voucher_filled_columns"].fillna("") if had_fills
             else pd.Series("", index=master.index)).astype("object").copy()
    base = master[[c for c in kept if c not in replaced and c != "voucher_filled_columns"]]
    joined = measurements.reindex(master_ids.values)[added]
    joined.index = master.index
    result = pd.concat([base, joined.astype("object")], axis=1)
    matched = master_ids.isin(measurements.index) & (master_ids != "")

    if fill_blanks:
        pairs = [(f, f"voucher_{f}") for f in VOUCHER_FIELDS if f in result.columns and f"voucher_{f}" in result.columns]
        if not pairs:
            log("  Fill empty fields: no voucher_ columns were added, or the dataset has no matching columns")
        for idx in result.index[matched]:
            filled = [f for f in str(fills[idx]).split(", ") if f]
            for field, source in pairs:
                value = result.at[idx, source]
                if not is_blank(value) and is_blank(result.at[idx, field]):
                    result.at[idx, field] = value
                    filled.append(field)
            fills[idx] = ", ".join(dict.fromkeys(filled))
    if fill_blanks or had_fills:
        result["voucher_filled_columns"] = fills

    if matched_only:
        result = result[matched]
    output_csv = Path(output_csv) if output_csv else master_csv
    note = ""
    if output_csv.resolve() == master_csv.resolve():
        backup = master_csv.with_name(master_csv.stem + "_before_join.csv")
        backup.write_bytes(master_csv.read_bytes())
        note = f" (previous version kept as {backup.name})"
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    save_csv(result, output_csv)

    unmatched = sorted(set(measurements.index) - set(master_ids))
    if unmatched:
        preview = ", ".join(unmatched[:5]) + (" and more" if len(unmatched) > 5 else "")
        log(f"  {len(unmatched)} measured {measurement_key} values are not in the dataset and were not joined: {preview}")
    log(f"Join complete: {int(matched.sum())} of {len(master)} records matched on {master_key} = {measurement_key}; "
        f"{len(result.columns)} columns and {len(result)} rows saved to {output_csv}{note}")
    return {"matched": int(matched.sum()), "total": len(master), "unmatched": len(unmatched),
            "rows": len(result), "output": str(output_csv)}