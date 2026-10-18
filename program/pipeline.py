"""Measure one herbarium sheet.

1. The vision model locates the sheet, ruler, labels and plant structures (locate.py).
2. Calibration measures pixels per cm from the ruler or the sheet (calibration.py).
3. Plant geometry measures each structure from the pixels (plant.py).
4. Quality checks turn doubtful values into NA with a reason, instead of a guessed number.

Every point is kept in full-resolution pixels so a person can review and correct it later.
"""
import math

import numpy as np

from .calibration import Calibration, calibrate, load_rgb
from .plant import PlantImage

TRAITS = ("panicle_length", "leaf_width", "seed_length")
TRAIT_LABELS = {"panicle_length": "panicle", "leaf_width": "leaf", "seed_length": "seed"}
TRAIT_BOUNDS_CM = {
    "panicle_length": (3.0, 75.0),
    "leaf_width": (0.15, 5.0),
    "seed_length": (0.15, 0.8),
}
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
    return entry


def measure_seed(cal, base, tip):
    entry = {"anchors": {"base": base, "tip": tip}, "points": [base, tip], "flags": [], "ok": False,
             "value_cm": round(_distance(base, tip) / cal.px_per_cm, 3)}
    low, high = TRAIT_BOUNDS_CM["seed_length"]
    entry["ok"] = low <= entry["value_cm"] <= high
    if not entry["ok"]:
        entry["flags"].append(f"{entry['value_cm']:.2f} cm is outside {low:g}-{high:g} cm")
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
        return [], None, result["notes"].get(trait) or "; ".join(
            f for e in result["traits"][trait] for f in e["flags"]) or "not found"
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
    good = [e for e in result["traits"]["panicle_length"] if e.get("points")]
    if cal.allows("seed_length"):
        if not good:
            result["notes"]["seed_length"] = "no panicle located, so no seeds to measure"
        else:
            best = max(good, key=lambda e: e.get("on_plant", 0))
            centre = [(best["anchors"]["base"][0] + best["anchors"]["tip"][0]) / 2,
                      (best["anchors"]["base"][1] + best["anchors"]["tip"][1]) / 2]
            seeds, raw["seeds"] = locator.seed_lengths(image, cal.px_per_cm, centre, control, status)
            if not seeds:
                result["notes"]["seed_length"] = "no clear spikelet found"
            for item in seeds:
                result["traits"]["seed_length"].append(measure_seed(cal, item["base"], item["tip"]))

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
