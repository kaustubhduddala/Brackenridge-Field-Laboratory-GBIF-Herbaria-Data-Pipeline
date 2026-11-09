"""Measure herbarium sheets: one sheet at a time, or a whole image folder into the measurements CSV.

For each sheet:
1. The vision model locates the sheet, ruler, labels and plant structures (vision_locator.py).
2. Calibration measures pixels per cm from the ruler or the sheet (scale_calibration.py).
3. Plant geometry measures each structure from the pixels (plant_geometry.py).
4. Quality checks turn doubtful values into NA with a reason, instead of a guessed number.

Every point is kept in full-resolution pixels so a person can review and correct it later.
measure_images() writes <responses>/<gbifID>.json (the full result and the raw model answers),
<responses>/<gbifID>_overlay.jpg (what was measured) and one row of the measurements CSV.
"""
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

from config import RESPONSES_DIR_NAME
from .measurements_csv import describe, is_done, load_measurements, save_measurements
from .plant_geometry import PlantImage, spikelet_axis
from .scale_calibration import Calibration, calibrate, load_rgb
from .utils import ItemSkipped, index_images
from .vision_locator import Locator
from .vision_providers import ModelOutputError, ModelSettings, ProviderError, open_client

TRAITS = ("panicle_length", "leaf_width", "seed_length")
TRAIT_LABELS = {"panicle_length": "panicle", "leaf_width": "leaf", "seed_length": "seed"}
# Hard limits (outside them a value is rejected) and the typical range (5th-95th percentile) of the lab's
# 163 hand measurements of Megathyrsus maximus sheets (01-06-2025_GBIFdownload_CFmeasurements.csv). A value
# inside the hard limits but outside the typical range is kept and flagged so a person checks it.
TRAIT_BOUNDS_CM = {
    "panicle_length": (6.0, 75.0),      # hand values 8.0-60.7 cm
    "leaf_width": (0.3, 4.5),           # hand values 0.38-3.79 cm
    "seed_length": (0.22, 0.75),        # hand values 0.25-0.61 cm
}
TYPICAL_CM = {
    "panicle_length": (12.0, 48.0),
    "leaf_width": (0.5, 2.4),
    "seed_length": (0.33, 0.55),
}
COPY_SPREAD = 0.03          # lengths this close together across 3+ marks mean the model copied one segment


def _flag_unusual(entry, trait):
    low, high = TYPICAL_CM[trait]
    value = entry.get("value_cm")
    if value is not None and not low <= value <= high:
        entry["flags"].append(f"unusual: lab hand measurements are mostly {low:g}-{high:g} cm, please check")


def reject_copied(entries):
    """Marks with (nearly) identical lengths were not measured one by one; reject them all."""
    values = [e["value_cm"] for e in entries if e.get("value_cm")]
    if len(values) >= 3 and (max(values) - min(values)) / (sum(values) / len(values)) < COPY_SPREAD:
        for entry in entries:
            entry["ok"] = False
            entry["flags"].append("all marks have the same length (the model copied one segment)")
    return entries
VOUCHER_FIELDS = ["catalogNumber", "recordedBy", "recordNumber", "eventDate", "country", "stateProvince",
                  "county", "locality", "habitat", "verbatimElevation", "identifiedBy", "scientificName"]
MIN_ON_PLANT = 0.8          # share of a traced path that must lie on plant pixels


def _distance(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def new_result(image_path, size):
    return {"image": image_path.name, "size": list(size), "calibration": None, "layout": {},
            "traits": {trait: [] for trait in TRAITS}, "notes": {trait: "" for trait in TRAITS},
            "flags": [], "voucher_text": "", "voucher": {}, "review_status": "auto"}


def measure_panicle(plant, cal, base, tip, visible="whole"):
    """One panicle from approximate base/tip points: snap, trace, check. Returns a trait entry."""
    entry = {"anchors": {"base": base, "tip": tip}, "points": [], "value_cm": None, "flags": [], "ok": False}
    if visible and visible != "whole":
        entry["flags"].append(f"panicle {visible}")
    snapped_base = plant.snap(base, radius_cm=1.0)
    if snapped_base is None:
        entry["flags"].append("base is not on the plant")
        return entry
    snapped_tip = plant.extremity_near(snapped_base, tip, radius_cm=1.5) or plant.snap(tip, radius_cm=1.0)
    if snapped_tip is None:
        entry["flags"].append("tip is not on the plant")
        return entry
    if _distance(snapped_base, snapped_tip) / cal.px_per_cm < 2.0:
        entry["flags"].append("tip and base are almost the same point")
        return entry
    path, length_cm, on_plant = plant.trace(snapped_base, snapped_tip)
    entry.update(anchors={"base": snapped_base, "tip": snapped_tip}, points=path, value_cm=round(length_cm, 2),
                 on_plant=round(on_plant, 2))
    straight = _distance(snapped_base, snapped_tip) / cal.px_per_cm
    if on_plant < MIN_ON_PLANT:
        entry["flags"].append(f"traced path is only {on_plant:.0%} on the plant")
    if straight and length_cm / straight > 1.6:
        entry["flags"].append(f"path winds ({length_cm / straight:.1f}x the straight distance)")
    low, high = TRAIT_BOUNDS_CM["panicle_length"]
    if not low <= length_cm <= high:
        entry["flags"].append(f"{length_cm:.1f} cm is outside {low:g}-{high:g} cm")
    entry["ok"] = on_plant >= MIN_ON_PLANT and low <= length_cm <= high
    if entry["ok"]:
        _flag_unusual(entry, "panicle_length")
    return entry


def measure_leaf(plant, cal, a, b):
    """Leaf width from two approximate margin points: the plant mask along the line fixes the true edges."""
    entry = {"anchors": {"a": a, "b": b}, "points": [a, b], "value_cm": None, "flags": [], "ok": False}
    ai_cm = _distance(a, b) / cal.px_per_cm
    entry["ai_cm"] = round(ai_cm, 2)
    value, edges = ai_cm, None
    found = plant.width_across(a, b)
    if found and 0.6 <= found[0] / max(ai_cm, 1e-6) <= 1.5:
        value, edges = found[0], (found[1], found[2])
        entry["points"] = list(edges)
    else:
        entry["flags"].append("blade edges not confirmed on the image; using the marked points")
    entry["value_cm"] = round(value, 2)
    low, high = TRAIT_BOUNDS_CM["leaf_width"]
    if not low <= value <= high:
        entry["flags"].append(f"{value:.2f} cm is outside {low:g}-{high:g} cm")
    entry["ok"] = low <= value <= high
    if entry["ok"]:
        _flag_unusual(entry, "leaf_width")
    return entry


def measure_seed(cal, base, tip):
    entry = {"anchors": {"base": base, "tip": tip}, "points": [base, tip], "flags": [], "ok": False,
             "value_cm": round(_distance(base, tip) / cal.px_per_cm, 3)}
    low, high = TRAIT_BOUNDS_CM["seed_length"]
    entry["ok"] = low <= entry["value_cm"] <= high
    if not entry["ok"]:
        entry["flags"].append(f"{entry['value_cm']:.2f} cm is outside {low:g}-{high:g} cm")
    else:
        _flag_unusual(entry, "seed_length")
    return entry


def remeasure(result, rgb=None, image_path=None):
    """Recompute every trait value from the stored points (after a person edits them)."""
    cal = Calibration(**result["calibration"]) if result.get("calibration") else None
    if not cal or not cal.px_per_cm:
        return result
    if rgb is None:
        rgb = load_rgb(image_path)
    layout = result.get("layout") or {}
    plant = PlantImage(rgb, cal.px_per_cm, layout.get("non_plant_boxes"), cal.sheet_box)
    entries = []
    for entry in result["traits"]["panicle_length"]:
        new = measure_panicle(plant, cal, entry["anchors"]["base"], entry["anchors"]["tip"])
        new["reviewed"] = entry.get("reviewed", False)
        entries.append(new)
    result["traits"]["panicle_length"] = entries
    for trait, redo in (("leaf_width", lambda e: measure_leaf(plant, cal, e["anchors"]["a"], e["anchors"]["b"])),
                        ("seed_length", lambda e: measure_seed(cal, e["anchors"]["base"], e["anchors"]["tip"]))):
        redone = []
        for entry in result["traits"][trait]:
            new = redo(entry)
            new["reviewed"] = entry.get("reviewed", False)
            redone.append(new)
        result["traits"][trait] = redone
    return result


def trait_value(result, trait):
    """(values used, summary value, note) for one trait."""
    entries = [e for e in result["traits"][trait] if e.get("ok") and e.get("value_cm") is not None]
    values = [e["value_cm"] for e in entries]
    if not values:
        return [], None, result["notes"].get(trait) or "; ".join(dict.fromkeys(
            f for e in result["traits"][trait] for f in e["flags"] if "outside" not in f or len(
                result["traits"][trait]) == 1)) or "; ".join(dict.fromkeys(
            f for e in result["traits"][trait] for f in e["flags"])) or "not found"
    summary = float(np.median(values)) if trait == "seed_length" else float(np.mean(values))
    flags = [f for e in entries for f in e["flags"]]
    return values, round(summary, 3), "; ".join(dict.fromkeys(flags))


def measure_sheet(image_path, locator, log=print, control=None, status=None, read_labels=True):
    """Measure one sheet. Returns (result, raw model answers)."""
    from PIL import Image

    rgb = load_rgb(image_path)
    image = Image.fromarray(rgb)
    result = new_result(image_path, image.size)
    raw = {"image_size_px": list(image.size)}

    layout, raw["layout"] = locator.layout(image, control, status)
    result["layout"] = layout
    cal = calibrate(rgb, ruler_hint=layout.get("ruler"), sheet_hint=layout.get("sheet_box"))
    result["calibration"] = cal.as_dict()
    result["flags"] += cal.flags
    if not cal.px_per_cm:
        for trait in TRAITS:
            result["notes"][trait] = "no scale: " + cal.describe()
        return result, raw

    for trait in TRAITS:
        if not cal.allows(trait):
            result["notes"][trait] = f"image resolution too low ({cal.px_per_cm:.0f} px/cm)"

    plant = PlantImage(rgb, cal.px_per_cm, layout.get("non_plant_boxes"), cal.sheet_box)
    if cal.allows("panicle_length"):
        panicles, raw["panicles"] = locator.panicles(image, control, status)
        if not panicles:
            result["notes"]["panicle_length"] = "no panicle found"
        for item in panicles:
            result["traits"]["panicle_length"].append(
                measure_panicle(plant, cal, item["base"], item["tip"], item.get("visible")))

    if cal.allows("leaf_width"):
        leaves, raw["leaves"] = locator.leaf_widths(image, cal.px_per_cm, layout.get("non_plant_boxes"), control, status)
        if not leaves:
            result["notes"]["leaf_width"] = "no flat, fully visible leaf found"
        for item in leaves:
            result["traits"]["leaf_width"].append(measure_leaf(plant, cal, item["a"], item["b"]))
        reject_copied(result["traits"]["leaf_width"])
    good = [e for e in result["traits"]["panicle_length"] if e.get("points")]
    if cal.allows("seed_length"):
        if not good:
            result["notes"]["seed_length"] = "no panicle located, so no seeds to measure"
        else:
            best = max(good, key=lambda e: e.get("on_plant", 0))
            centre = [(best["anchors"]["base"][0] + best["anchors"]["tip"][0]) / 2,
                      (best["anchors"]["base"][1] + best["anchors"]["tip"][1]) / 2]
            if getattr(locator, "seed_mode", "points") == "boxes":
                boxes, raw["seeds"] = locator.seed_boxes(image, cal.px_per_cm, centre, control, status)
                seeds = []
                for box in boxes:
                    axis = spikelet_axis(rgb, box)
                    if axis:
                        seeds.append({"base": axis[0], "tip": axis[1]})
            else:
                seeds, raw["seeds"] = locator.seed_lengths(image, cal.px_per_cm, centre, control, status)
            if not seeds:
                result["notes"]["seed_length"] = "no clear spikelet found"
            for item in seeds:
                result["traits"]["seed_length"].append(measure_seed(cal, item["base"], item["tip"]))
            reject_copied(result["traits"]["seed_length"])

    if read_labels:
        try:
            voucher, raw["voucher"] = locator.voucher(image, control, status)
            result["voucher_text"], result["voucher"] = voucher["voucher_text"], voucher["voucher"]
        except Exception as exc:  # labels are optional; never lose the measurements over them
            from .utils import ItemSkipped
            if isinstance(exc, ItemSkipped):
                raise
            raw["voucher_error"] = str(exc)
    return result, raw


# --------------------------------------------------------------------------- overlay

COLOURS = {"panicle_length": (255, 61, 0), "leaf_width": (0, 230, 118), "seed_length": (255, 234, 0),
           "ruler": (0, 229, 255), "sheet": (120, 144, 156), "excluded": (213, 0, 249), "rejected": (158, 158, 158)}


def draw_overlay(image_path, result, out_path, max_side=1800):
    """A downscaled JPEG showing what was measured; rejected measurements are drawn in grey."""
    from PIL import Image, ImageDraw, ImageFont

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(image_path) as im:
        canvas = im.convert("RGB")
        canvas.thumbnail((max_side, max_side))
    s = canvas.width / result["size"][0]
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=max(12, canvas.width // 70))
    except TypeError:
        font = ImageFont.load_default()

    def pt(p):
        return (p[0] * s, p[1] * s)

    cal = result.get("calibration") or {}
    if cal.get("sheet_box"):
        draw.rectangle([*pt(cal["sheet_box"][:2]), *pt(cal["sheet_box"][2:])], outline=COLOURS["sheet"], width=2)
    for box in (result.get("layout") or {}).get("non_plant_boxes", []):
        draw.rectangle([*pt(box[:2]), *pt(box[2:])], outline=COLOURS["excluded"], width=2)
    if cal.get("ruler_line"):
        a, b = map(pt, cal["ruler_line"])
        draw.line([a, b], fill=COLOURS["ruler"], width=4)
    title = (f"{cal.get('px_per_cm') or 0:.1f} px/cm ({cal.get('source')})" if cal.get("px_per_cm")
             else "no scale")
    draw.rectangle((0, 0, canvas.width, font.size + 10), fill=(0, 0, 0))
    draw.text((6, 4), f"{result['image']}  {title}", fill=(255, 255, 255), font=font)
    for trait in TRAITS:
        for n, entry in enumerate(result["traits"][trait], start=1):
            colour = COLOURS[trait] if entry.get("ok") else COLOURS["rejected"]
            points = [pt(p) for p in entry.get("points") or []]
            if len(points) >= 2:
                draw.line(points, fill=colour, width=3)
            for name, p in (entry.get("anchors") or {}).items():
                x, y = pt(p)
                draw.ellipse((x - 5, y - 5, x + 5, y + 5), outline=colour, width=2)
            if points:
                value = f"{entry['value_cm']:.2f} cm" if entry.get("value_cm") is not None else "NA"
                draw.text((points[0][0] + 8, points[0][1] - font.size), f"{TRAIT_LABELS[trait]} {n}: {value}",
                          fill=colour, font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "JPEG", quality=88)
    return out_path


# --------------------------------------------------------------------------- measuring a folder of sheets

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
