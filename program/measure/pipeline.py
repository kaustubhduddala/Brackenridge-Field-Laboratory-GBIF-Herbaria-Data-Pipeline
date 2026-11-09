"""Measure herbarium sheets: one sheet at a time, or a whole image folder into the measurements CSV.

For each sheet:
1. Scale first. The layout pass finds the ruler, labels and envelopes; calibration.py turns the ruler
   (or, failing that, the sheet width) into pixels per centimetre. No scale, no measurements.
2. The detector finds panicles, leaves and seeds: landmarks, boxes and, with the add-on, outlines.
3. Geometry measures them from the pixels: a geodesic path along the plant for a panicle, the blade's
   width from its outline or its edges for a leaf, the long axis of each spikelet for the seeds.
4. Occlusion: a panicle end that stops at a label or the sheet edge makes the length a lower bound.
5. Optional second opinions from a vision model (hidden tip? folded leaf? debris?) count as one vote.
6. decision.py turns the flags into accept / needs review / lower bound / not measurable.

Every point is kept in full-resolution pixels so a person can review and correct it later.
measure_images() writes <responses>/<gbifID>.json (the full result and the raw model answers),
<responses>/<gbifID>_overlay.jpg (what was measured) and one row of the measurements CSV.
"""
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from config import RESPONSES_DIR_NAME
from . import PIPELINE_VERSION, occlusion
from . import geometry
from .calibration import Calibration, calibrate, load_rgb
from .decision import add_flag, decide_trait, describe_decision, has_flag
from .detectors import DetectorOptions, build_detector
from .geometry import (WIDTH_PX_PER_CM, PlantImage, blade_checks, distance_to_edge, polygon_mask, principal_axis,
                       skeleton_path, spikelet_axis, widest_point)
from .overlay import draw_overlay
from .providers import ModelOutputError, ModelSettings, ProviderError
from .records import (export_measurement_or_fact, is_done, load_measurements, needs_review, now, result_row,
                      save_measurements, start_fresh)
from .taxa import DEFAULT_PROFILE, LANDMARKS, TRAIT_LABELS, TRAITS, taxon_names
from ..utils import ItemSkipped, index_images

MIN_ON_PLANT = 0.8          # share of a traced path that must lie on plant pixels
MAX_WINDING = 1.6           # path length over straight distance above which a panicle path "winds"
TIP_SLACK_CM = 1.0          # plant running on further than this beyond a tip means the tip is not the end
TIP_INSIDE_CM = 0.4         # a tip this far inside its own outline is not at the outline's end
METHODS_AGREE = 0.2         # two widths/lengths further apart than this share disagree
COPY_SPREAD = 0.03          # lengths this close together across 3+ marks mean the model copied one segment
SPIKELET_ASPECT = (1.4, 5.0)


@dataclass
class Sheet:
    """Everything the measuring functions need to know about the sheet being measured."""
    rgb: np.ndarray
    cal: Calibration
    plant: PlantImage
    occluders: list
    profile: object = DEFAULT_PROFILE

    @property
    def size(self):
        return (self.rgb.shape[1], self.rgb.shape[0])

    @property
    def ppc(self):
        return self.cal.px_per_cm


def _distance(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def new_result(image_path, size, profile=DEFAULT_PROFILE):
    return {"version": PIPELINE_VERSION, "image": Path(image_path).name, "size": list(size), "profile": profile.name,
            "calibration": None, "layout": {}, "traits": {trait: [] for trait in TRAITS},
            "notes": {trait: "" for trait in TRAITS}, "decisions": {}, "flags": [], "detector": {},
            "voucher_text": "", "voucher": {}, "review_status": "auto"}


def blank_result(image_path, profile=DEFAULT_PROFILE, rgb=None):
    """A sheet nobody has measured: only the scale (ruler or sheet width, found from the pixels, no model
    needed) is filled in, so a person can add the measurements by clicking them in the Review tab."""
    rgb = load_rgb(image_path) if rgb is None else rgb
    result = new_result(image_path, (rgb.shape[1], rgb.shape[0]), profile)
    result["layout"] = {"sheet_box": None, "non_plant_boxes": [], "ruler": None, "objects": [], "source": "manual"}
    cal = calibrate(rgb)
    result["calibration"] = cal.as_dict()
    result["flags"] += cal.flags
    result["detector"] = {"manual": "reviewer"}
    return decide(result, profile)


def new_entry(trait, obj=None, n=1):
    obj = obj or {}
    return {"id": f"{TRAIT_LABELS[trait]}-{n}", "trait": trait, "anchors": {}, "points": [],
            "polygon": obj.get("polygon"), "box": obj.get("box"), "value_cm": None, "px": None, "method": "",
            "score": obj.get("score"), "source": obj.get("source", ""), "quality": 1.0, "flags": [], "attrs": {},
            "reviewed": False}


def _set_value(entry, px, sheet):
    """Store a measured extent, and reject it when it is too few pixels or an impossible size."""
    trait = entry["trait"]
    entry["px"] = round(float(px), 1)
    entry["value_cm"] = round(float(px) / sheet.ppc, 3)
    entry["flags"] = [f for f in entry["flags"] if f["code"] not in ("too_small", "out_of_range")]
    minimum = sheet.profile.min_object_px[trait]
    if px < minimum:
        add_flag(entry, "too_small", f"spans only {px:.0f} px (at least {minimum:g} are needed)")
    low, high = sheet.profile.hard_cm[trait]
    if not low <= entry["value_cm"] <= high:
        add_flag(entry, "out_of_range", f"{entry['value_cm']:.2f} cm is outside {low:g}-{high:g} cm")
    return entry


def _outline_mask(polygon, ppc, target_px_per_cm=None):
    """(mask, offset, scale) of an outline, drawn in its own bounding box at a working resolution."""
    xs, ys = [p[0] for p in polygon], [p[1] for p in polygon]
    scale = min(1.0, target_px_per_cm / ppc) if target_px_per_cm else 1.0
    x0, y0 = (min(xs) - 2) * scale, (min(ys) - 2) * scale
    shape = (int((max(ys) - min(ys) + 4) * scale) + 1, int((max(xs) - min(xs) + 4) * scale) + 1)
    return polygon_mask(polygon, shape, offset=(x0, y0), scale=scale), (x0, y0), scale


def _to_full(point, offset, scale):
    return [round((point[0] + offset[0]) / scale, 1), round((point[1] + offset[1]) / scale, 1)]


# --------------------------------------------------------------------------- panicle length

def _snap_landmark(sheet, point, radius_cm=1.0):
    """The plant pixel a landmark belongs to.

    A landmark placed inside a label's box (the model guessing where a hidden tip is) has no plant
    near it; it is then snapped to where the plant meets the box, and the end is judged hidden later.
    """
    snapped = sheet.plant.snap(point, radius_cm=radius_cm)
    if snapped is None:
        for occluder in sheet.occluders:
            box = occluder["box"]
            if occlusion.box_distance(point, box) == 0:
                reach = min(point[0] - box[0], box[2] - point[0], point[1] - box[1], box[3] - point[1])
                snapped = sheet.plant.snap(point, radius_cm=reach / sheet.ppc + radius_cm)
                if snapped is not None:
                    break
    return snapped


def measure_panicle(sheet, obj, n=1):
    """One panicle: snap the landmarks to the plant, trace the path between them, check both ends."""
    entry = new_entry("panicle_length", obj, n)
    plant, ppc = sheet.plant, sheet.ppc
    points = obj.get("keypoints") or {}
    base, tip, polygon = points.get("base"), points.get("tip"), obj.get("polygon")
    landmarks = "the base and tip landmarks"
    if not (base and tip) and polygon:
        # No landmarks: the ends of the outline's longest geodesic stand in for them (lower end = base).
        mask, offset, scale = _outline_mask(polygon, ppc, plant.work_px_per_cm)
        path, _ = skeleton_path(mask)
        if len(path) >= 2:
            ends = sorted((_to_full(path[0], offset, scale), _to_full(path[-1], offset, scale)), key=lambda p: -p[1])
            base, tip = ends
            landmarks = "the ends of the outline's longest geodesic"
    if not (base and tip):
        return add_flag(entry, "not_found", "no landmarks or outline for this panicle")
    entry["anchors"] = {"base": list(base), "tip": list(tip)}
    snapped_base = _snap_landmark(sheet, base)
    if snapped_base is None:
        return add_flag(entry, "not_on_plant", "the base is not on the plant")
    snapped_tip = plant.extremity_near(snapped_base, tip, radius_cm=1.5)
    if snapped_tip is None:
        # The model put the tip under a label: take the point where this panicle meets the label.
        hiding = next((o for o in sheet.occluders if occlusion.box_distance(tip, o["box"]) == 0), None)
        snapped_tip = (plant.entry_to_box(snapped_base, tip, hiding["box"]) if hiding else None) \
            or _snap_landmark(sheet, tip)
    if snapped_tip is None:
        return add_flag(entry, "not_on_plant", "the tip is not on the plant")
    straight_cm = _distance(snapped_base, snapped_tip) / ppc
    if straight_cm < 2.0:
        return add_flag(entry, "degenerate", "the tip and the base are almost the same point")

    path, length_cm, _ = plant.trace(snapped_base, snapped_tip, prefer=polygon)
    dense = occlusion.densify(path, max(1.0, 0.05 * ppc))
    in_view = [p for p in dense if not any(occlusion.box_distance(p, o["box"]) == 0 for o in sheet.occluders)]
    on_plant = plant.share_on(in_view) if in_view else 0.0
    entry.update(anchors={"base": snapped_base, "tip": snapped_tip}, points=path, quality=round(on_plant, 2),
                 method=f"geodesic path along the plant between {landmarks}")
    _set_value(entry, length_cm * ppc, sheet)

    if on_plant < MIN_ON_PLANT:
        add_flag(entry, "off_plant", f"the traced path is only {on_plant:.0%} on the plant")
    if length_cm / straight_cm > MAX_WINDING:
        add_flag(entry, "winding", f"the path winds ({length_cm / straight_cm:.1f}x the straight distance)")
    for crossing in occlusion.crossings(path, sheet.occluders, ppc, plant):
        if crossing["over"]:
            add_flag(entry, "bridged_over", f"the plant lies on top of a {crossing['kind']} for "
                                            f"{crossing['length_cm']:.1f} cm")
        else:
            add_flag(entry, "interpolated", f"{crossing['length_cm']:.1f} cm of the path is hidden under a "
                                            f"{crossing['kind']} and was bridged")
    entry["ends"] = {}
    for which in ("tip", "base"):
        state = occlusion.assess_end(plant, path, which, sheet.occluders, sheet.cal.sheet_box, sheet.size)
        entry["ends"][which] = state
        if state["state"] == "hidden":
            where = f"the {state['by']}" if "edge" in state["by"] else f"a {state['by']}"
            add_flag(entry, f"occluded_{which}", f"the {which} stops at {where}, so the length is a minimum"
                     + ("; the plant shows again beyond it" if state["continues"] else ""))
        elif state["state"] == "over" and state["continues"] and which == "tip":
            add_flag(entry, "tip_not_at_end", f"the plant carries on across a {state['by']} beyond the marked tip")
    if entry["ends"]["tip"]["state"] == "free":
        further = plant.continues_past(snapped_base, snapped_tip)
        if further > TIP_SLACK_CM:
            add_flag(entry, "tip_not_at_end", f"the plant carries on about {further:.1f} cm beyond the marked tip")
    if obj.get("visible") == "partly covered":
        add_flag(entry, "partly_covered")
    if polygon:
        mask, offset, scale = _outline_mask(polygon, ppc, plant.work_px_per_cm)
        inside = distance_to_edge(mask, [snapped_tip[0] * scale - offset[0], snapped_tip[1] * scale - offset[1]])
        if inside / (ppc * scale) > TIP_INSIDE_CM:
            add_flag(entry, "tip_inside_mask")
    return entry


# --------------------------------------------------------------------------- leaf width

def _fine_width(sheet, a, b):
    """Blade width (cm, edge, edge) along the line a-b on a plant mask made at 0.1 mm per pixel, or None."""
    marked = _distance(a, b)
    if marked < 2:
        return None
    margin = max(marked, 0.5 * sheet.ppc)
    w, h = sheet.size
    x0, y0 = int(max(0, min(a[0], b[0]) - margin)), int(max(0, min(a[1], b[1]) - margin))
    x1, y1 = int(min(w, max(a[0], b[0]) + margin)), int(min(h, max(a[1], b[1]) + margin))
    crop = sheet.rgb[y0:y1, x0:x1]
    if crop.shape[0] < 8 or crop.shape[1] < 8:
        return None
    boxes = [[o["box"][0] - x0, o["box"][1] - y0, o["box"][2] - x0, o["box"][3] - y0] for o in sheet.occluders]
    fine = PlantImage(crop, sheet.ppc, boxes, [0, 0, crop.shape[1], crop.shape[0]], work_px_per_cm=WIDTH_PX_PER_CM)
    found = fine.width_across([a[0] - x0, a[1] - y0], [b[0] - x0, b[1] - y0])
    if not found:
        return None
    return found[0], [found[1][0] + x0, found[1][1] + y0], [found[2][0] + x0, found[2][1] + y0]


def measure_leaf(sheet, obj, n=1):
    """Leaf width at the widest point of the blade.

    With an outline (SAM 2 or the trained model) the width is the blade's largest inscribed width and
    the outline is checked for folds. Without one, the blade's edges are found on the plant mask along
    the line between the two marked margin points.
    """
    entry = new_entry("leaf_width", obj, n)
    ppc = sheet.ppc
    points = obj.get("keypoints") or {}
    a, b, polygon = points.get("a"), points.get("b"), obj.get("polygon")
    marked_cm = _distance(a, b) / ppc if a and b else None
    across = _fine_width(sheet, a, b) if a and b else None
    if across and not 0.6 <= across[0] / max(marked_cm, 1e-6) <= 1.5:
        across = None                                    # the run of plant along the line is not this blade
    widest = None
    if polygon:
        mask, offset, scale = _outline_mask(polygon, ppc, WIDTH_PX_PER_CM)
        found = widest_point(mask)
        if found:
            widest = (found[0] / scale, _to_full(found[2], offset, scale), _to_full(found[3], offset, scale))
            facts, codes = blade_checks(mask, ppc * scale)
            entry["shape"] = facts
            for code in codes:
                add_flag(entry, code)
    if widest:
        width_px, edge_a, edge_b = widest
        entry["method"] = "largest inscribed width of the blade outline"
        other = across[0] if across else marked_cm
        if other and abs(other - width_px / ppc) / (width_px / ppc) > METHODS_AGREE:
            add_flag(entry, "methods_disagree", f"the outline gives {width_px / ppc:.2f} cm, the marked line "
                                                f"{other:.2f} cm")
    elif across:
        width_px, edge_a, edge_b = across[0] * ppc, across[1], across[2]
        entry.update(method="blade edges found on the plant mask along the marked line", quality=0.9)
    elif marked_cm is not None:
        width_px, edge_a, edge_b = marked_cm * ppc, a, b
        entry.update(method="distance between the marked margin points", quality=0.6)
        add_flag(entry, "edges_unconfirmed", "the blade edges were not confirmed on the image; the value is the "
                                             "distance between the marked points")
    else:
        return add_flag(entry, "not_found", "no margin points or outline for this leaf")
    entry["anchors"] = {"a": [round(v, 1) for v in edge_a], "b": [round(v, 1) for v in edge_b]}
    entry["points"] = [entry["anchors"]["a"], entry["anchors"]["b"]]
    if marked_cm is not None:
        entry["marked_cm"] = round(marked_cm, 3)
    return _set_value(entry, width_px, sheet)


# --------------------------------------------------------------------------- seed length

def measure_seed(sheet, obj, n=1):
    """Seed (spikelet) length: the long axis of its outline, or of the spikelet found by colour in its box."""
    entry = new_entry("seed_length", obj, n)
    points, polygon, box = obj.get("keypoints") or {}, obj.get("polygon"), obj.get("box")
    length_px, ends = None, None
    if polygon:
        mask, offset, scale = _outline_mask(polygon, sheet.ppc)
        axis = principal_axis(mask)
        if axis:
            ends = sorted((_to_full(axis[0], offset, scale), _to_full(axis[1], offset, scale)), key=lambda p: -p[1])
            length_px = axis[2]
            entry["method"] = "long axis of the spikelet outline"
            aspect = axis[2] / max(axis[3], 1.0)
            if not SPIKELET_ASPECT[0] <= aspect <= SPIKELET_ASPECT[1]:
                add_flag(entry, "odd_shape", f"the outline is {aspect:.1f} times longer than wide, unlike a spikelet")
    if length_px is None and box:
        axis = spikelet_axis(sheet.rgb, box)
        if axis:
            ends, length_px = [axis[0], axis[1]], axis[2]
            entry.update(method="long axis of the spikelet found by colour inside its box", quality=0.85)
            aspect = axis[2] / max(axis[3], 1.0)
            if not SPIKELET_ASPECT[0] <= aspect <= SPIKELET_ASPECT[1]:
                add_flag(entry, "odd_shape", f"the shape is {aspect:.1f} times longer than wide, unlike a spikelet")
    if points.get("base") and points.get("tip"):
        marked = _distance(points["base"], points["tip"])
        if length_px is None:
            ends, length_px = [points["base"], points["tip"]], marked
            entry.update(method="distance between the model's base and tip landmarks", quality=0.8)
        elif abs(marked - length_px) / length_px > METHODS_AGREE:
            add_flag(entry, "methods_disagree", f"the outline gives {length_px / sheet.ppc:.2f} cm, the landmarks "
                                                f"{marked / sheet.ppc:.2f} cm")
    if length_px is None:
        return add_flag(entry, "not_found", "no spikelet could be outlined in this box")
    entry["anchors"] = {"base": list(ends[0]), "tip": list(ends[1])}
    entry["points"] = [entry["anchors"]["base"], entry["anchors"]["tip"]]
    return _set_value(entry, length_px, sheet)


def reject_copied(entries):
    """Marks with (nearly) identical lengths were not placed one by one; reject them all."""
    marked = [e for e in entries if has_flag(e, "edges_unconfirmed") and e.get("value_cm")]
    values = [e["value_cm"] for e in marked]
    if len(values) >= 3 and (max(values) - min(values)) / (sum(values) / len(values)) < COPY_SPREAD:
        for entry in marked:
            add_flag(entry, "copied")
    return entries


# --------------------------------------------------------------------------- second opinions

def second_opinions(sheet, image, result, locator, raw, control=None, status=None):
    """Ask the vision model yes/no questions about what was measured. Its answer is one vote:

    where it agrees with a geometric warning the object is rejected, where it disagrees with a clean
    geometric result the object goes to review. It never changes a number.
    """
    raw["second_opinion"] = []

    def ask(what, call):
        try:
            answer, answer_raw = call()
        except ItemSkipped:
            raise
        except (ValueError, KeyError) as exc:          # an unusable answer is no vote
            raw["second_opinion"].append({"about": what, "error": str(exc)})
            return None
        raw["second_opinion"].append({"about": what, "answer": answer, "raw": answer_raw})
        return answer

    w, h = sheet.size
    for entry in result["traits"]["panicle_length"]:
        if entry.get("value_cm") is None or len(entry.get("points") or []) < 2:
            continue
        xs, ys = [p[0] for p in entry["points"]], [p[1] for p in entry["points"]]
        margin = 2.0 * sheet.ppc
        box = [max(0, min(xs) - margin), max(0, min(ys) - margin), min(w, max(xs) + margin), min(h, max(ys) + margin)]
        answer = ask(entry["id"], lambda: locator.check_panicle(image, box, control, status))
        if answer:
            entry["second_opinion"] = answer
            for which in ("tip", "base"):
                if answer[which] == "hidden" and not has_flag(entry, f"occluded_{which}"):
                    add_flag(entry, "second_opinion", f"the vision model thinks the {which} is hidden")
    for entry in result["traits"]["leaf_width"]:
        if entry.get("value_cm") is None:
            continue
        a, b = entry["anchors"]["a"], entry["anchors"]["b"]
        cx, cy, half = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2, 3.5 * sheet.ppc
        box = [max(0, cx - half), max(0, cy - half), min(w, cx + half), min(h, cy + half)]
        answer = ask(entry["id"], lambda: locator.check_leaf(image, box, control, status))
        if answer:
            entry["second_opinion"] = answer
            if not answer["flat"]:
                if has_flag(entry, "width_jump") or has_flag(entry, "methods_disagree"):
                    add_flag(entry, "folded", f"folded or overlapped: the outline and the vision model agree "
                                              f"({answer['problem']})")
                else:
                    add_flag(entry, "second_opinion", f"the vision model thinks the blade is {answer['problem']}")
    seeds = [e for e in result["traits"]["seed_length"] if e.get("value_cm") is not None and e.get("box")]
    if seeds:
        margin = 0.5 * sheet.ppc
        crop = [max(0, min(e["box"][0] for e in seeds) - margin), max(0, min(e["box"][1] for e in seeds) - margin),
                min(w, max(e["box"][2] for e in seeds) + margin), min(h, max(e["box"][3] for e in seeds) + margin)]
        answer = ask("seeds", lambda: locator.check_seeds(image, crop, [e["box"] for e in seeds], control, status))
        for entry, whole in zip(seeds, answer or []):
            entry["second_opinion"] = {"one_whole_spikelet": whole}
            if not whole:
                if has_flag(entry, "odd_shape"):
                    add_flag(entry, "not_this_structure", "not a whole spikelet: the outline and the vision model agree")
                else:
                    add_flag(entry, "second_opinion", "the vision model thinks this is not one whole spikelet")


# --------------------------------------------------------------------------- deciding

def blocked_reason(result, trait, profile=DEFAULT_PROFILE):
    """(code, text) when a trait cannot be measured on this sheet at all, else None."""
    calibration = result.get("calibration") or {}
    ppc = calibration.get("px_per_cm")
    if not ppc:
        return "no_scale", "no scale: " + Calibration.from_dict(calibration).describe().removeprefix("No scale: ")
    minimum = profile.min_px_per_cm[trait]
    if ppc < minimum:
        return "low_resolution", f"image resolution too low ({ppc:.0f} px/cm, {minimum:g} needed)"
    if not result["traits"][trait] and result["notes"].get(trait):
        return "not_found", result["notes"][trait]
    return None


def decide(result, profile=DEFAULT_PROFILE):
    """Fill result["decisions"]: the status, value and confidence of every trait."""
    reviewed = result.get("review_status") == "reviewed"
    for trait in TRAITS:
        result["decisions"][trait] = decide_trait(trait, result["traits"][trait], profile, result.get("calibration"),
                                                  blocked_reason(result, trait, profile), reviewed)
    return result


def _sheet_for(result, rgb, profile=DEFAULT_PROFILE, cache=None, image_path=None):
    """The Sheet (scale, plant mask, label boxes) for a stored result, built once per image when a cache is given."""
    cal = Calibration.from_dict(result.get("calibration"))
    if cache is not None and cache.get("sheet") is not None and cache.get("px_per_cm") == cal.px_per_cm:
        return cache["sheet"]
    rgb = load_rgb(image_path) if rgb is None else rgb
    occluders = occlusion.occluders_from_layout(result.get("layout") or {})
    sheet = Sheet(rgb, cal, PlantImage(rgb, cal.px_per_cm, [o["box"] for o in occluders], cal.sheet_box), occluders,
                  profile)
    if cache is not None:
        cache.update(sheet=sheet, px_per_cm=cal.px_per_cm)
    return sheet


def panicle_from_outline(result, rgb, polygon, box=None, profile=DEFAULT_PROFILE, cache=None):
    """A panicle entry for an outline (SAM 2 from a box a person drew). Not added to the result.

    The two ends are the extremes of the outline along its long axis, the lower one as the base. They are only
    a first guess, since a panicle with a long side branch has a longer axis than its main stem, so the entry
    is marked for measuring again: the route is then traced along the plant inside the outline between them,
    and a person can drag either end.
    """
    cal = Calibration.from_dict(result.get("calibration"))
    if not cal.px_per_cm:
        raise ValueError("This sheet has no scale yet; set it with Scale… first")
    sheet = _sheet_for(result, rgb, profile, cache)
    mask, offset, scale = _outline_mask(polygon, cal.px_per_cm, sheet.plant.work_px_per_cm)
    axis = geometry.principal_axis(mask)
    if axis is None:
        raise ValueError("The outline is too small to find its ends")
    base, tip = sorted((_to_full(axis[0], offset, scale), _to_full(axis[1], offset, scale)), key=lambda p: -p[1])
    taken = [int(e["id"].rsplit("-", 1)[-1]) for e in result["traits"]["panicle_length"]
             if e["id"].rsplit("-", 1)[-1].isdigit()]
    entry = new_entry("panicle_length", {"polygon": polygon, "box": box, "source": "sam"}, 1 + max(taken, default=0))
    entry.update(anchors={"base": base, "tip": tip}, edited=True, reviewed=True)
    return entry


def remeasure(result, rgb=None, image_path=None, profile=DEFAULT_PROFILE, cache=None):
    """Recompute values and decisions after a person changed points, attributes or the scale.

    An object whose points were moved ("edited") is measured again from them: a panicle along the
    plant between the two points (or straight, where the plant mask cannot follow), a leaf or seed as
    the distance between the two points, as in ImageJ. Other objects keep their pixels and are only
    rescaled, so a corrected scale flows through to every value. `cache` (a dict the caller keeps
    for one image) saves rebuilding the plant mask on every edit.
    """
    cal = Calibration.from_dict(result.get("calibration"))
    if not cal.px_per_cm:
        return decide(result, profile)
    sheet = None
    for trait in TRAITS:
        for n, entry in enumerate(result["traits"][trait], start=1):
            entry.setdefault("trait", trait)
            entry.setdefault("id", f"{TRAIT_LABELS[trait]}-{n}")
            first, second = LANDMARKS[trait]
            anchors = entry.get("anchors") or {}
            if entry.get("edited") and anchors.get(first) and anchors.get(second):
                if sheet is None:
                    sheet = _sheet_for(result, rgb, profile, cache, image_path)
                _measure_edited(sheet, entry, anchors[first], anchors[second])
                entry["edited"] = False
            elif entry.get("px") is not None:
                proxy = Sheet(np.zeros((1, 1, 3), np.uint8), cal, None, [], profile)
                _set_value(entry, entry["px"], proxy)
    return decide(result, profile)


def _measure_edited(sheet, entry, first, second):
    entry["flags"] = []
    straight = _distance(first, second)
    if entry["trait"] == "panicle_length" and straight / sheet.ppc >= 2.0:
        outline = entry.get("polygon")                  # from SAM 2: the route stays on this panicle
        reach = 1.0 if outline else 0.3
        a = sheet.plant.snap(first, radius_cm=reach) or first
        b = sheet.plant.snap(second, radius_cm=reach) or second
        path, length_cm, on_plant = sheet.plant.trace(a, b, prefer=outline)
        if on_plant >= 0.6 and length_cm * sheet.ppc <= MAX_WINDING * _distance(a, b):
            entry.update(points=path, quality=round(on_plant, 2),
                         method=("geodesic path along the plant inside the SAM outline between the reviewer's points"
                                 if outline else "geodesic path along the plant between the reviewer's points"))
            return _set_value(entry, length_cm * sheet.ppc, sheet)
    entry.update(points=[list(first), list(second)], quality=1.0,
                 method="straight distance between the reviewer's points")
    return _set_value(entry, straight, sheet)


def measure_sheet(image_path, detector, profile=DEFAULT_PROFILE, log=print, control=None, status=None):
    """Measure one sheet. Returns (result, raw model answers)."""
    from PIL import Image

    rgb = load_rgb(image_path)
    image = Image.fromarray(rgb)
    result = new_result(image_path, image.size, profile)
    result["detector"] = detector.versions()
    raw = {"image_size_px": list(image.size)}

    layout, raw["layout"] = detector.layout(image, rgb, control, status)
    result["layout"] = layout
    cal = calibrate(rgb, ruler_hint=layout.get("ruler"), sheet_hint=layout.get("sheet_box"))
    result["calibration"] = cal.as_dict()
    result["flags"] += cal.flags
    if cal.px_per_cm:
        occluders = occlusion.occluders_from_layout(layout)
        sheet = Sheet(rgb, cal, PlantImage(rgb, cal.px_per_cm, [o["box"] for o in occluders], cal.sheet_box),
                      occluders, profile)
        allowed = {trait: cal.allows(trait, profile.min_px_per_cm) for trait in TRAITS}

        if allowed["panicle_length"]:
            found, raw["panicles"] = detector.panicles(image, rgb, control, status)
            if not found:
                result["notes"]["panicle_length"] = "no panicle found"
            for n, obj in enumerate(found, start=1):
                obj["polygon"] = detector.outline(rgb, obj)
                result["traits"]["panicle_length"].append(measure_panicle(sheet, obj, n))

        if allowed["leaf_width"]:
            found, raw["leaves"] = detector.leaves(image, rgb, cal.px_per_cm, [o["box"] for o in occluders],
                                                   control, status)
            if not found:
                result["notes"]["leaf_width"] = "no flat, fully visible leaf found"
            for n, obj in enumerate(found, start=1):
                obj["polygon"] = detector.outline(rgb, obj)
                result["traits"]["leaf_width"].append(measure_leaf(sheet, obj, n))
            reject_copied(result["traits"]["leaf_width"])

        if allowed["seed_length"]:
            traced = [e for e in result["traits"]["panicle_length"] if e.get("points")]
            if not traced:
                result["notes"]["seed_length"] = "no panicle located, so no seeds to measure"
            else:
                found, raw["seeds"] = detector.seeds(image, rgb, cal.px_per_cm, traced, control, status)
                if not found:
                    result["notes"]["seed_length"] = "no clear spikelet found"
                for n, obj in enumerate(found, start=1):
                    obj["polygon"] = detector.outline(rgb, obj)
                    result["traits"]["seed_length"].append(measure_seed(sheet, obj, n))

        if detector.can_check:
            second_opinions(sheet, image, result, detector.locator, raw, control, status)

    if detector.can_read_labels:
        try:
            voucher, raw["voucher"] = detector.read_voucher(image, rgb, layout, cal.px_per_cm, taxon_names(profile),
                                                            control, status)
            result["voucher_text"], result["voucher"] = voucher["voucher_text"], voucher["voucher"]
        except ItemSkipped:
            raise
        except Exception as exc:  # labels are optional; never lose the measurements over them
            raw["voucher_error"] = str(exc)
    return decide(result, profile), raw


# --------------------------------------------------------------------------- measuring a folder of sheets

def response_path(responses_dir, gbif_id):
    return Path(responses_dir) / f"{gbif_id}.json"


def overlay_path(responses_dir, gbif_id):
    return Path(responses_dir) / f"{gbif_id}_overlay.jpg"


def save_result(gbif_id, image, result, raw, error, responses_dir):
    """Write the per-image JSON, and the overlay when there is a result to draw."""
    responses_dir = Path(responses_dir)
    responses_dir.mkdir(parents=True, exist_ok=True)
    record = {"gbifID": gbif_id, "image": Path(image).name, "saved_at": now(), "error": error,
              "result": result, "raw": raw}
    if result:
        try:
            draw_overlay(image, result, overlay_path(responses_dir, gbif_id))
        except Exception as exc:  # the overlay is for review only; never lose the result over it
            record["overlay_error"] = str(exc)
    path = response_path(responses_dir, gbif_id)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def load_result(responses_dir, gbif_id):
    """The saved record for an image, or None when there is none in the current format."""
    try:
        record = json.loads(response_path(responses_dir, gbif_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    result = record.get("result")
    if not isinstance(result, dict) or not str(result.get("version", "")).startswith("2"):
        return None
    return record


def measure_images(media_dir, csv_path, log=print, control=None, progress=None, status=None, settings=None,
                   only_ids=None, force=False, options=None, profile=DEFAULT_PROFILE, read_labels=None,
                   detector=None):
    """Measure every (or the selected) image in media_dir into csv_path. Returns a summary dict.

    The detector is built from `settings` (the vision model) and `options`; pass `detector` to use
    one that is already open.
    """
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    responses_dir = csv_path.parent / RESPONSES_DIR_NAME
    images = index_images(media_dir)
    targets = [(gbif_id, path) for gbif_id, path in images.items() if only_ids is None or gbif_id in only_ids]
    summary = {"total": len(targets), "measured": 0, "skipped": 0, "skipped_by_user": 0, "failed": 0,
               "cancelled": False, "needs_review": 0}
    if not images:
        log(f"No downloaded images found in {media_dir}. Download images first.")
        return summary

    options = options or DetectorOptions()
    if read_labels is not None:
        options.read_labels = read_labels
    detector = detector or build_detector(settings or ModelSettings(), options, log)
    start_fresh(csv_path, log)
    rows = load_measurements(csv_path)
    scope = "selected " if only_ids is not None else ""
    log(f"Measuring {len(targets)} {scope}images from {media_dir}: {detector.describe()}")

    for number, (gbif_id, image) in enumerate(targets, start=1):
        if control and control.cancelled:
            summary["cancelled"] = True
            break
        if control:
            control.new_item()
        if progress:
            progress(number - 1, len(targets))
        previous = rows.get(gbif_id)
        if is_done(previous) and not force:
            summary["skipped"] += 1
            continue

        log(f"[{number}/{len(targets)}] Measuring {image.name}")
        started = datetime.now()
        try:
            result, raw = measure_sheet(image, detector, profile, log=log, control=control, status=status)
            save_result(gbif_id, image, result, raw, "", responses_dir)
            rows[gbif_id] = result_row(gbif_id, image.name, result)
            summary["measured"] += 1
            log(f"    Done in {(datetime.now() - started).seconds} s: "
                + ", ".join(describe_decision(result["decisions"][trait]) for trait in TRAITS))
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
            rows[gbif_id] = {**(previous or {}), "gbifID": gbif_id, "ai_image": image.name, "ai_error": str(exc),
                             "ai_measured_at": now()}
            summary["failed"] += 1
            log(f"    Failed: {exc}")
        save_measurements(rows, csv_path)

    summary["needs_review"] = sum(1 for gbif_id, _ in targets if needs_review(rows.get(gbif_id)))
    if summary["measured"] and csv_path.is_file():
        export_measurement_or_fact(csv_path, log=lambda _: None)
    if summary["cancelled"]:
        log("Measurement cancelled")
    elif progress:
        progress(len(targets), len(targets))
    log(f"Measurement complete: {summary['measured']} measured, {summary['skipped']} already done, "
        f"{summary['skipped_by_user']} skipped by you, {summary['failed']} failed; {summary['needs_review']} of "
        f"these sheets need review. Saved to {csv_path}")
    return summary
