"""The measurements CSV: its columns, reading and writing it, joining it into the dataset, and the
Darwin Core MeasurementOrFact export.

One row per sheet. For each trait the row carries the value, its status (accept, needs review,
lower bound, not measurable), a confidence, how many objects it is built from, the method and the
flags. A length that is only a minimum (the panicle runs under a label) is written to its own
..._lower_bound_cm column and never to the value column, so it cannot slip into a mean.
"""
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

from config import APP_NAME, RESPONSES_DIR_NAME
from . import PIPELINE_VERSION
from .decision import ACCEPT, LOWER_BOUND, NEEDS_REVIEW, NOT_MEASURABLE
from .providers import check_connection as provider_check_connection
from .taxa import TRAIT_LABELS, TRAIT_NAMES, TRAITS
from ..utils import index_images, is_blank, read_csv, save_csv

VOUCHER_FIELDS = ["catalogNumber", "recordedBy", "recordNumber", "eventDate", "country", "stateProvince",
                  "county", "locality", "habitat", "verbatimElevation", "identifiedBy", "scientificName"]
CENSORED_TRAITS = ("panicle_length",)          # traits that can have a lower bound
TRAIT_FIELDS = ("cm", "status", "confidence", "n", "values_cm", "method", "flags")
MOF_FILE_NAME = "measurement_or_fact.csv"
LEGACY_SUFFIX = "legacy"


def trait_columns(trait):
    columns = [f"ai_{trait}_{field}" for field in TRAIT_FIELDS]
    if trait in CENSORED_TRAITS:
        columns.insert(1, f"ai_{trait}_lower_bound_cm")
    return columns


RESULT_COLUMNS = (
    ["ai_image"]
    + [column for trait in TRAITS for column in trait_columns(trait)]
    + ["ai_px_per_cm", "ai_scale_source", "ai_calibration", "ai_notes", "ai_review", "voucher_text"]
    + [f"voucher_{field}" for field in VOUCHER_FIELDS]
    + ["ai_model", "ai_detector", "ai_pipeline", "ai_measured_at", "ai_error"]
)
MEASUREMENT_FILE_COLUMNS = ["gbifID", *RESULT_COLUMNS]
VALUE_COLUMNS = {trait: f"ai_{trait}_cm" for trait in TRAITS}
STATUS_COLUMNS = {trait: f"ai_{trait}_status" for trait in TRAITS}


def now():
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- reading and writing

def is_legacy(csv_path):
    """True for a measurements file written before the status columns existed."""
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        return False
    try:
        columns = pd.read_csv(csv_path, nrows=0, encoding="utf-8-sig").columns
    except (pd.errors.EmptyDataError, OSError):
        return False
    return "gbifID" in columns and "ai_pipeline" not in columns and any(str(c).startswith("ai_") for c in columns)


def _free_name(path):
    candidate, number = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        number += 1
    return candidate


def start_fresh(csv_path, log=print):
    """Set an old-format measurements file (and its responses folder) aside so a new one starts clean.

    Nothing is deleted: measurements.csv becomes measurements_legacy.csv and responses/ becomes
    responses_legacy/. Returns the new name of the CSV, or None when there was nothing to move.
    """
    csv_path = Path(csv_path)
    if not is_legacy(csv_path):
        return None
    kept = _free_name(csv_path.with_name(f"{csv_path.stem}_{LEGACY_SUFFIX}{csv_path.suffix}"))
    csv_path.rename(kept)
    log(f"{csv_path.name} was written by the old measuring method; it is kept as {kept.name} and a new "
        f"{csv_path.name} is started.")
    responses = csv_path.parent / RESPONSES_DIR_NAME
    if responses.is_dir() and any(responses.iterdir()):
        moved = _free_name(responses.with_name(f"{RESPONSES_DIR_NAME}_{LEGACY_SUFFIX}"))
        responses.rename(moved)
        log(f"The old model answers and overlays are kept in {moved.name}/.")
    return kept


def load_measurements(csv_path):
    """{gbifID: row} from a measurements CSV. A missing or old-format file gives {}."""
    csv_path = Path(csv_path)
    if not csv_path.exists() or is_legacy(csv_path):
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


def _number(value, digits=2):
    return "" if value is None else f"{value:.{digits}f}"


def result_row(gbif_id, image_name, result, versions=None):
    """One measurements-CSV row from a pipeline result (see pipeline.measure_sheet)."""
    from .calibration import Calibration

    row = {"gbifID": gbif_id, "ai_image": image_name}
    notes = []
    for trait in TRAITS:
        decision = result["decisions"][trait]
        digits = 3 if trait == "seed_length" else 2
        row[f"ai_{trait}_cm"] = _number(decision["value_cm"], digits)
        if trait in CENSORED_TRAITS:
            row[f"ai_{trait}_lower_bound_cm"] = _number(decision["lower_bound_cm"], digits)
        row[f"ai_{trait}_status"] = decision["status"]
        row[f"ai_{trait}_confidence"] = _number(decision["confidence"]) if decision["n"] else ""
        row[f"ai_{trait}_n"] = str(decision["n"]) if decision["n"] else ""
        row[f"ai_{trait}_values_cm"] = "; ".join(_number(v, digits) for v in decision["values"])
        row[f"ai_{trait}_method"] = decision["method"]
        row[f"ai_{trait}_flags"] = "; ".join(decision["flags"])
        if decision["note"]:
            notes.append(f"{TRAIT_LABELS[trait]}: {decision['note']}")
    calibration = result.get("calibration") or {}
    versions = versions if versions is not None else (result.get("detector") or {})
    row.update({
        "ai_px_per_cm": _number(calibration.get("px_per_cm"), 1),
        "ai_scale_source": calibration.get("source", "none") if calibration.get("px_per_cm") else "none",
        "ai_calibration": Calibration.from_dict(calibration).describe() if calibration else "no calibration",
        "ai_notes": "; ".join(notes),
        "ai_review": result.get("review_status", "auto"),
        "voucher_text": result.get("voucher_text", ""),
        **{f"voucher_{field}": (result.get("voucher") or {}).get(field, "") for field in VOUCHER_FIELDS},
        "ai_model": versions.get("vision", ""),
        "ai_detector": "; ".join(f"{k}={v}" for k, v in versions.items() if k != "vision"),
        "ai_pipeline": PIPELINE_VERSION,
        "ai_measured_at": now(),
        "ai_error": "",
    })
    return row


def describe(row):
    parts = []
    for trait in TRAITS:
        label, value = TRAIT_LABELS[trait], row.get(f"ai_{trait}_cm")
        bound = row.get(f"ai_{trait}_lower_bound_cm")
        status = row.get(f"ai_{trait}_status", "")
        if not is_blank(value):
            parts.append(f"{label} {value} cm" + (" (review)" if status == NEEDS_REVIEW else ""))
        elif not is_blank(bound):
            parts.append(f"{label} >= {bound} cm")
        else:
            parts.append(f"{label} -")
    return ", ".join(parts)


MISSED_FLAGS = ("not_found", "no_scale")      # nothing was found, or there is no scale: a person can fix both


def missed(row):
    """True when a trait was not measurable only because the detector found nothing or there is no scale."""
    return any(row.get(f"ai_{trait}_status") == NOT_MEASURABLE
               and any(flag in str(row.get(f"ai_{trait}_flags", "")) for flag in MISSED_FLAGS) for trait in TRAITS)


def needs_review(row):
    """True when a person should look at this sheet: a trait needs review, is only a lower bound, or was missed."""
    if not row or row.get("ai_review") == "reviewed":
        return False
    return missed(row) or any(row.get(f"ai_{trait}_status") in (NEEDS_REVIEW, LOWER_BOUND) for trait in TRAITS)


def lowest_confidence(row):
    """The smallest trait confidence in a row (the review queue is sorted by it). Missed sheets come first."""
    if missed(row):
        return 0.0
    values = []
    for trait in TRAITS:
        try:
            values.append(float(row.get(f"ai_{trait}_confidence") or ""))
        except ValueError:
            pass
    return min(values) if values else 1.0


def measurement_status(media_dir, csv_path):
    rows = load_measurements(csv_path)
    responses_dir = Path(csv_path).parent / RESPONSES_DIR_NAME
    items = []
    for gbif_id, image in index_images(media_dir).items():
        row = rows.get(gbif_id)
        if is_done(row):
            status = "Reviewed" if row.get("ai_review") == "reviewed" else (
                "Needs review" if needs_review(row) else "Measured")
            detail = describe(row)
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


# --------------------------------------------------------------------------- Darwin Core MeasurementOrFact

MOF_COLUMNS = ["gbifID", "measurementID", "measurementType", "measurementValue", "measurementUnit",
               "measurementAccuracy", "measurementMethod", "measurementDeterminedBy", "measurementDeterminedDate",
               "measurementRemarks"]


def measurement_or_fact(rows):
    """Long-format records (one per value) using the Darwin Core MeasurementOrFact terms.

    Only values that exist are written. A lower bound is its own measurementType ("minimum panicle
    length") so that it is never read as the length itself.
    """
    records = []
    for gbif_id, row in rows.items():
        if not is_done(row):
            continue
        scale = row.get("ai_px_per_cm") or ""
        try:
            accuracy = f"1 pixel = {1.0 / float(scale):.4f} cm; scale from {row.get('ai_scale_source') or 'unknown'}"
        except (ValueError, ZeroDivisionError):
            accuracy = ""
        reviewed = row.get("ai_review") == "reviewed"
        by = f"{APP_NAME} {row.get('ai_pipeline', '')}".strip()
        models = "; ".join(p for p in (row.get("ai_model"), row.get("ai_detector")) if p)
        by += f" ({models})" if models else ""
        by += "; reviewed by a person" if reviewed else ""
        for trait in TRAITS:
            name = TRAIT_NAMES[trait].lower()
            status = row.get(f"ai_{trait}_status", "")
            remarks = [f"status: {status}"]
            for label, key in (("confidence", "confidence"), ("n", "n"), ("flags", "flags")):
                if not is_blank(row.get(f"ai_{trait}_{key}")):
                    remarks.append(f"{label}: {row[f'ai_{trait}_{key}']}")
            base = {"gbifID": gbif_id, "measurementUnit": "cm", "measurementAccuracy": accuracy,
                    "measurementMethod": row.get(f"ai_{trait}_method", ""), "measurementDeterminedBy": by,
                    "measurementDeterminedDate": row.get("ai_measured_at", ""),
                    "measurementRemarks": "; ".join(remarks)}
            if not is_blank(row.get(f"ai_{trait}_cm")) and status in (ACCEPT, NEEDS_REVIEW):
                records.append({**base, "measurementID": f"{gbif_id}:{trait}", "measurementType": name,
                                "measurementValue": row[f"ai_{trait}_cm"]})
            if not is_blank(row.get(f"ai_{trait}_lower_bound_cm")):
                records.append({**base, "measurementID": f"{gbif_id}:{trait}:minimum",
                                "measurementType": f"minimum {name}",
                                "measurementValue": row[f"ai_{trait}_lower_bound_cm"]})
    return pd.DataFrame(records, columns=MOF_COLUMNS)


def export_measurement_or_fact(csv_path, output=None, log=print):
    """Write measurement_or_fact.csv next to a measurements CSV. Returns (path, number of records)."""
    csv_path = Path(csv_path)
    table = measurement_or_fact(load_measurements(csv_path))
    output = Path(output) if output else csv_path.with_name(MOF_FILE_NAME)
    save_csv(table, output)
    log(f"Darwin Core MeasurementOrFact: {len(table)} records saved to {output}")
    return output, len(table)


def status_counts(rows):
    """{trait: {status: count}} over the measured rows."""
    counts = {trait: {s: 0 for s in (ACCEPT, NEEDS_REVIEW, LOWER_BOUND, NOT_MEASURABLE)} for trait in TRAITS}
    for row in rows.values():
        if is_done(row):
            for trait in TRAITS:
                status = row.get(f"ai_{trait}_status")
                if status in counts[trait]:
                    counts[trait][status] += 1
    return counts


# --------------------------------------------------------------------------- joining into a dataset

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
        shutil.copy2(master_csv, backup)
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
