"""Reviewed labels: what a person confirmed on a sheet, kept as training data.

A label file (<download>/labels/<gbifID>.json) lists the objects on one sheet: class, the two
landmarks of its measurement, a box, an outline when there is one, and the reviewer's attributes
(folded, cut off, excluded). Labels are written when a sheet is accepted on the Review tab, and can
be exchanged with CVAT, Label Studio or Roboflow as a COCO file:

    export_coco()   reviewed labels, or the pipeline's own unreviewed results as pre-labels to correct
    import_coco()   corrected labels back into the label store (and into the measurements)
"""
import json
import math
from pathlib import Path

from config import RESPONSES_DIR_NAME
from .decision import NOT_MEASURABLE
from .records import load_measurements, now, result_row, save_measurements
from .taxa import CLASS_TRAIT, DEFAULT_PROFILE, LANDMARKS, TRAIT_CLASS, TRAITS
from .trained import CLASSES, KEYPOINT_NAMES

LABELS_DIR_NAME = "labels"
ATTRIBUTES = ("folded", "cut_off", "exclude")
BOX_PAD = {"panicle": 0.12, "leaf": 1.0, "seed": 0.3}     # share of the landmark distance added round a box


def labels_dir(csv_path):
    return Path(csv_path).parent / LABELS_DIR_NAME


def box_of(cls, keypoints=None, polygon=None, size=None):
    """A box for an object: around its outline, else around its landmarks with a class-dependent margin."""
    if polygon and len(polygon) >= 3:
        xs, ys = [p[0] for p in polygon], [p[1] for p in polygon]
        box = [min(xs), min(ys), max(xs), max(ys)]
    elif keypoints and len(keypoints) >= 2:
        (ax, ay), (bx, by) = list(keypoints.values())[:2]
        pad = max(BOX_PAD.get(cls, 0.2) * math.hypot(bx - ax, by - ay), 4.0)
        box = [min(ax, bx) - pad, min(ay, by) - pad, max(ax, bx) + pad, max(ay, by) + pad]
    else:
        return None
    if size:
        box = [max(0.0, box[0]), max(0.0, box[1]), min(float(size[0]), box[2]), min(float(size[1]), box[3])]
    return [round(v, 1) for v in box]


def label_from_result(gbif_id, result, reviewed_only=True):
    """The label for a sheet from its measurement result.

    With reviewed_only (training data) only objects a person accepted are kept. Without it every
    measured object is kept, as pre-labels for correction in an external tool.
    """
    calibration = result.get("calibration") or {}
    objects = []
    for trait in TRAITS:
        cls = TRAIT_CLASS[trait]
        for entry in result["traits"][trait]:
            anchors = entry.get("anchors") or {}
            attrs = {k: bool((entry.get("attrs") or {}).get(k)) for k in ATTRIBUTES}
            if not all(anchors.get(name) for name in LANDMARKS[trait]):
                continue
            if reviewed_only and (not entry.get("reviewed") or attrs["exclude"]):
                continue
            keypoints = {name: [round(float(v), 1) for v in anchors[name]] for name in LANDMARKS[trait]}
            objects.append({"cls": cls, "keypoints": keypoints, "polygon": entry.get("polygon"),
                            "via": [list(p) for p in entry.get("via") or []],
                            "fitted": bool(entry.get("fit", True)),
                            "route": [[round(float(x), 1), round(float(y), 1)] for x, y in entry.get("points") or []]
                            if trait == "panicle_length" else None,
                            "box": box_of(cls, keypoints, entry.get("polygon"), result["size"]), "attrs": attrs,
                            "value_cm": entry.get("value_cm")})
    for obj in (result.get("layout") or {}).get("objects") or []:      # labels and rulers found by a trained model
        if obj.get("cls") in CLASSES and obj.get("box") and not reviewed_only:
            objects.append({"cls": obj["cls"], "keypoints": None, "polygon": obj.get("polygon"),
                            "box": [round(float(v), 1) for v in obj["box"]], "attrs": {}, "value_cm": None})
    return {"gbifID": str(gbif_id), "image": result["image"], "size": list(result["size"]),
            "px_per_cm": calibration.get("px_per_cm"), "scale_source": calibration.get("source"),
            "objects": objects, "source": "review" if result.get("review_status") == "reviewed" else "pipeline",
            "saved_at": now()}


def save_label(folder, label):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{label['gbifID']}.json"
    path.write_text(json.dumps(label, indent=2), encoding="utf-8")
    return path


def load_labels(folder):
    """{gbifID: label} for every label file in a folder."""
    labels = {}
    folder = Path(folder)
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            try:
                label = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(label, dict) and isinstance(label.get("objects"), list) and label.get("size"):
                labels[str(label.get("gbifID") or path.stem)] = label
    return labels


# --------------------------------------------------------------------------- accepting a reviewed sheet

def save_review(csv_path, gbif_id, image_path, result, rgb=None, profile=DEFAULT_PROFILE, raw=None):
    """Store a sheet a person has reviewed: result, overlay, CSV row and training label.

    Every usable object left on the sheet counts as confirmed; objects the pipeline rejected stay
    excluded unless the person corrected them. Returns the CSV row.
    """
    from .pipeline import load_result, remeasure, save_result

    csv_path = Path(csv_path)
    responses_dir = csv_path.parent / RESPONSES_DIR_NAME
    result["review_status"] = "reviewed"
    result["reviewed_at"] = now()
    for trait in TRAITS:
        for entry in result["traits"][trait]:
            # What the pipeline rejected and the person did not touch stays out; accepting the sheet
            # must not turn a greyed-out object into a value.
            if not entry.get("reviewed") and entry.get("status") == NOT_MEASURABLE:
                entry["attrs"] = {**(entry.get("attrs") or {}), "exclude": True}
            entry["reviewed"] = True
    remeasure(result, rgb=rgb, image_path=image_path, profile=profile)
    if raw is None:
        previous = load_result(responses_dir, gbif_id)
        raw = (previous or {}).get("raw", {})
    save_result(gbif_id, image_path, result, raw, "", responses_dir)
    rows = load_measurements(csv_path)
    rows[str(gbif_id)] = result_row(str(gbif_id), Path(image_path).name, result)
    save_measurements(rows, csv_path)
    save_label(labels_dir(csv_path), label_from_result(gbif_id, result))
    return rows[str(gbif_id)]


# --------------------------------------------------------------------------- COCO

def _categories():
    categories = []
    for number, cls in enumerate(CLASSES, start=1):
        category = {"id": number, "name": cls, "supercategory": "herbarium"}
        if cls in KEYPOINT_NAMES:
            category.update(keypoints=list(KEYPOINT_NAMES[cls]), skeleton=[[1, 2]])
        categories.append(category)
    return categories


def to_coco(labels):
    """A COCO dataset (dict) from {gbifID: label}: boxes, outlines and the two landmarks per object."""
    images, annotations = [], []
    category_id = {cls: n for n, cls in enumerate(CLASSES, start=1)}
    for image_id, (gbif_id, label) in enumerate(sorted(labels.items()), start=1):
        images.append({"id": image_id, "file_name": label["image"], "width": label["size"][0],
                       "height": label["size"][1], "gbifID": gbif_id})
        for obj in label["objects"]:
            if obj.get("cls") not in category_id or not obj.get("box"):
                continue
            x0, y0, x1, y1 = obj["box"]
            annotation = {"id": len(annotations) + 1, "image_id": image_id, "category_id": category_id[obj["cls"]],
                          "bbox": [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)],
                          "area": round((x1 - x0) * (y1 - y0), 1), "iscrowd": 0, "segmentation": [],
                          "attributes": {k: bool(v) for k, v in (obj.get("attrs") or {}).items()}}
            if obj.get("polygon") and len(obj["polygon"]) >= 3:
                annotation["segmentation"] = [[round(float(v), 1) for point in obj["polygon"] for v in point]]
            names = KEYPOINT_NAMES.get(obj["cls"])
            if names:
                points = obj.get("keypoints") or {}
                flat, visible = [], 0
                for name in names:
                    if points.get(name):
                        flat += [round(float(points[name][0]), 1), round(float(points[name][1]), 1), 2]
                        visible += 1
                    else:
                        flat += [0, 0, 0]
                annotation.update(keypoints=flat, num_keypoints=visible)
            annotations.append(annotation)
    return {"info": {"description": "Herbarium sheet labels", "date_created": now()}, "licenses": [],
            "categories": _categories(), "images": images, "annotations": annotations}


def export_coco(csv_path, output=None, reviewed_only=True, log=print):
    """Write a COCO file for the sheets of a download. Returns (path, images, annotations).

    reviewed_only=True exports the reviewed labels. False exports what the pipeline measured on
    every sheet, as pre-labels to correct in CVAT or Label Studio.
    """
    from .pipeline import load_result

    csv_path = Path(csv_path)
    if reviewed_only:
        labels = load_labels(labels_dir(csv_path))
    else:
        labels = {}
        responses_dir = csv_path.parent / RESPONSES_DIR_NAME
        for gbif_id in load_measurements(csv_path):
            record = load_result(responses_dir, gbif_id)
            if record:
                label = label_from_result(gbif_id, record["result"], reviewed_only=False)
                if label["objects"]:
                    labels[gbif_id] = label
    if not labels:
        raise ValueError("There is nothing to export yet: " + (
            "accept some sheets on the Review tab first." if reviewed_only else "measure some images first."))
    output = Path(output) if output else csv_path.parent / ("labels_coco.json" if reviewed_only
                                                              else "prelabels_coco.json")
    coco = to_coco(labels)
    output.write_text(json.dumps(coco), encoding="utf-8")
    log(f"COCO export: {len(coco['images'])} images and {len(coco['annotations'])} objects saved to {output}. "
        "Import it into CVAT as 'COCO Keypoints 1.0' together with the images.")
    return output, len(coco["images"]), len(coco["annotations"])


def from_coco(coco):
    """{gbifID: label} from a COCO dataset dict (the reverse of to_coco)."""
    categories = {c["id"]: c for c in coco.get("categories", [])}
    labels = {}
    by_image = {}
    for annotation in coco.get("annotations", []):
        by_image.setdefault(annotation["image_id"], []).append(annotation)
    for image in coco.get("images", []):
        gbif_id = str(image.get("gbifID") or Path(image["file_name"]).stem)
        objects = []
        for annotation in by_image.get(image["id"], []):
            category = categories.get(annotation["category_id"]) or {}
            cls = str(category.get("name", "")).lower().replace(" ", "_")
            if cls not in CLASSES:
                continue
            polygon = None
            segmentation = annotation.get("segmentation")
            if isinstance(segmentation, list) and segmentation and isinstance(segmentation[0], list):
                longest = max(segmentation, key=len)
                if len(longest) >= 6:
                    polygon = [[float(longest[i]), float(longest[i + 1])] for i in range(0, len(longest) - 1, 2)]
            keypoints = None
            flat, names = annotation.get("keypoints") or [], category.get("keypoints") or KEYPOINT_NAMES.get(cls, ())
            if len(flat) >= 6 and len(names) >= 2:
                wanted = KEYPOINT_NAMES.get(cls, tuple(names[:2]))
                found = {str(name): [float(flat[3 * i]), float(flat[3 * i + 1])]
                         for i, name in enumerate(names) if 3 * i + 2 < len(flat) and flat[3 * i + 2] > 0}
                if all(name in found for name in wanted):
                    keypoints = {name: found[name] for name in wanted}
            x, y, w, h = annotation.get("bbox") or [0, 0, 0, 0]
            box = [float(x), float(y), float(x + w), float(y + h)] if w and h else box_of(cls, keypoints, polygon)
            if not box:
                continue
            attrs = annotation.get("attributes") or {}
            objects.append({"cls": cls, "keypoints": keypoints, "polygon": polygon, "box": box,
                            "attrs": {k: bool(attrs.get(k)) for k in ATTRIBUTES}, "value_cm": None})
        labels[gbif_id] = {"gbifID": gbif_id, "image": image["file_name"], "size": [image["width"], image["height"]],
                           "px_per_cm": None, "scale_source": None, "objects": objects, "source": "coco",
                           "saved_at": now()}
    return labels


def import_coco(coco_path, csv_path, media_dir=None, log=print, profile=DEFAULT_PROFILE):
    """Read corrected labels from a COCO file into the label store.

    Where a sheet has already been measured, its measurements are replaced by the labelled objects
    (as a reviewed sheet) so the CSV follows the corrections. Returns the number of sheets imported.
    """
    from .pipeline import load_result, new_entry
    from ..utils import index_images

    csv_path = Path(csv_path)
    labels = from_coco(json.loads(Path(coco_path).read_text(encoding="utf-8")))
    images = index_images(media_dir) if media_dir else {}
    responses_dir = csv_path.parent / RESPONSES_DIR_NAME
    updated = 0
    for gbif_id, label in labels.items():
        record = load_result(responses_dir, gbif_id)
        image = images.get(gbif_id)
        if record and image is not None:
            result = record["result"]
            for trait in TRAITS:
                entries = []
                for n, obj in enumerate((o for o in label["objects"] if CLASS_TRAIT.get(o["cls"]) == trait), start=1):
                    if not obj.get("keypoints"):
                        continue
                    entry = new_entry(trait, {"polygon": obj.get("polygon"), "box": obj.get("box"),
                                              "source": "review"}, n)
                    entry.update(anchors=obj["keypoints"], attrs=dict(obj.get("attrs") or {}), edited=True)
                    entries.append(entry)
                result["traits"][trait] = entries
            save_review(csv_path, gbif_id, image, result, profile=profile, raw=record.get("raw", {}))
            label["px_per_cm"] = (result.get("calibration") or {}).get("px_per_cm")
            updated += 1
        label["source"] = "coco"
        save_label(labels_dir(csv_path), label)
    log(f"COCO import: labels for {len(labels)} sheets saved to {labels_dir(csv_path)}; "
        f"{updated} measured sheets were updated from them.")
    return len(labels)
