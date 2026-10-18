"""Measure a folder of herbarium images with the per-sheet pipeline and keep the results.

For each image this writes <responses>/<gbifID>.json (the full result: calibration, every traced
point, flags, and the raw model answers) and <responses>/<gbifID>_overlay.jpg (what was measured),
and one row of the measurements CSV in the columns the rest of the app already reads.
"""
import json
from datetime import datetime
from pathlib import Path

from config import RESPONSES_DIR_NAME
from .analysis import describe, is_done, load_measurements, save_measurements
from .calibration import Calibration
from .locate import Locator
from .pipeline import TRAITS, draw_overlay, measure_sheet, trait_value
from .providers import ModelOutputError, ModelSettings, ProviderError, open_client
from .utils import ItemSkipped, index_images

TRAIT_LABELS = {"panicle_length": "panicle", "leaf_width": "leaf", "seed_length": "seed"}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _number(value):
    return "" if value is None else f"{value:.2f}"


def result_columns(result, model, image):
    """One measurements-CSV row (without gbifID) from a pipeline result."""
    columns = {"ai_image": image.name}
    notes = []
    for trait in TRAITS:
        values, summary, note = trait_value(result, trait)
        for n in (1, 2):
            columns[f"ai_{trait}_{n}_cm"] = _number(values[n - 1] if len(values) >= n else None)
            columns[f"ai_{trait}_{n}_confidence"] = ""
        columns[f"ai_{trait}_mean_cm"] = _number(summary)
        if note:
            notes.append(f"{TRAIT_LABELS[trait]}: {note}")
    calibration = result.get("calibration") or {}
    columns.update({
        "ai_calibration": Calibration(**calibration).describe() if calibration else "no calibration",
        "ai_notes": "; ".join(notes + [f for f in result.get("flags", []) if f not in calibration.get("flags", [])]),
        "voucher_text": result.get("voucher_text", ""),
        **{f"voucher_{field}": value for field, value in (result.get("voucher") or {}).items()},
        "ai_model": model,
        "ai_measured_at": _now(),
        "ai_error": "",
    })
    return columns


def save_result(gbif_id, image, result, raw, error, responses_dir):
    """Write the per-image JSON, and the overlay when there is a result to draw."""
    responses_dir = Path(responses_dir)
    responses_dir.mkdir(parents=True, exist_ok=True)
    record = {"gbifID": gbif_id, "image": image.name, "saved_at": _now(), "error": error,
              "result": result, "raw": raw}
    path = responses_dir / f"{gbif_id}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if result:
        try:
            draw_overlay(image, result, responses_dir / f"{gbif_id}_overlay.jpg")
        except Exception as exc:  # the overlay is for review only; never lose the result over it
            record["overlay_error"] = str(exc)
    return path


def measure_images(media_dir, csv_path, log=print, control=None, progress=None, status=None, settings=None,
                   only_ids=None, force=False, read_labels=True):
    """Measure every (or the selected) image in media_dir into csv_path. Returns a summary dict."""
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    responses_dir = csv_path.parent / RESPONSES_DIR_NAME
    images = index_images(media_dir)
    rows = load_measurements(csv_path)
    targets = [(gbif_id, path) for gbif_id, path in images.items() if only_ids is None or gbif_id in only_ids]
    summary = {"total": len(targets), "measured": 0, "skipped": 0, "skipped_by_user": 0, "failed": 0,
               "cancelled": False}
    if not images:
        log(f"No downloaded images found in {media_dir}. Download images first.")
        return summary

    settings = settings or ModelSettings()
    client = open_client(settings, log)
    for warning in client.warnings:
        log(f"Warning: {warning}")
    locator = Locator(client)
    scope = "selected " if only_ids is not None else ""
    log(f"Measuring {len(targets)} {scope}images from {media_dir} with {settings.label}: {client.model_id}")

    for number, (gbif_id, image) in enumerate(targets, start=1):
        if control and control.cancelled:
            summary["cancelled"] = True
            break
        if control:
            control.new_item()
        if progress:
            progress(number - 1, len(targets))
        if is_done(rows.get(gbif_id)) and not force:
            summary["skipped"] += 1
            continue

        log(f"[{number}/{len(targets)}] Measuring {image.name}")
        started = datetime.now()
        try:
            result, raw = measure_sheet(image, locator, log=log, control=control, status=status,
                                        read_labels=read_labels)
            save_result(gbif_id, image, result, raw, "", responses_dir)
            rows[gbif_id] = {"gbifID": gbif_id, **result_columns(result, client.model_id, image)}
            summary["measured"] += 1
            log(f"    Done in {(datetime.now() - started).seconds} s: {describe(rows[gbif_id])}")
        except ItemSkipped:
            if control and control.cancelled:
                summary["cancelled"] = True
                break
            log(f"    Skipped {image.name}")
            summary["skipped_by_user"] += 1
            continue
        except ProviderError:
            raise
        except Exception as exc:
            save_result(gbif_id, image, None, getattr(exc, "raw", {}) if isinstance(exc, ModelOutputError) else {},
                        str(exc), responses_dir)
            previous = rows.get(gbif_id, {})
            rows[gbif_id] = {**previous, "gbifID": gbif_id, "ai_image": image.name, "ai_error": str(exc),
                             "ai_measured_at": _now()}
            summary["failed"] += 1
            log(f"    Failed: {exc}")
        save_measurements(rows, csv_path)

    if summary["cancelled"]:
        log("Measurement cancelled")
    elif progress:
        progress(len(targets), len(targets))
    log(f"Measurement complete: {summary['measured']} measured, {summary['skipped']} already done, "
        f"{summary['skipped_by_user']} skipped by you, {summary['failed']} failed. Saved to {csv_path}")
    return summary
