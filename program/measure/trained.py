"""The optional PyTorch add-on: trained YOLO segmentation and keypoint models, and SAM 2 masks.

Nothing here is needed to run the app. With the add-on installed (python main.py models install):

    seg model    outlines panicles, leaves, seeds, labels, rulers, envelopes and colour charts
    pose model   finds the two landmarks of each measurement (panicle base/tip, leaf margins, seed base/tip)
    SAM 2        turns a box or a click into a precise outline, for measuring and for quick labelling

The models are fine-tuned on this lab's reviewed sheets by training.py and recorded in
models/registry.json, so every measurement can name the model version that produced it.
torch and ultralytics are imported only inside the functions that need them.
"""
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from config import BASE_DIR, FROZEN
from . import downloads

MODELS_DIR = BASE_DIR / "models"
REGISTRY_FILE = MODELS_DIR / "registry.json"
# Object classes of the seg model, in label order. The pose model uses the first three.
CLASSES = ["panicle", "leaf", "seed", "label", "ruler", "envelope", "color_chart"]
POSE_CLASSES = ["panicle", "leaf", "seed"]
KEYPOINT_NAMES = {"panicle": ("base", "tip"), "leaf": ("a", "b"), "seed": ("base", "tip")}
OCCLUDER_CLASSES = ("label", "envelope", "color_chart")
SAM_WEIGHTS = "sam2.1_b.pt"
BASE_WEIGHTS = {"seg": "yolo11m-seg.pt", "pose": "yolo11m-pose.pt"}
SHEET_IMGSZ = 1280
TILE = 1280                     # native-resolution tile for small objects (seeds)
TILE_OVERLAP = 0.2
MIN_SCORE = 0.25
# pip steps for the add-on. ultralytics is installed without its dependency list because it asks for
# opencv-python, which would sit on top of the opencv-python-headless this app already uses.
INSTALL_STEPS = (["torch", "torchvision", "psutil", "polars", "pyyaml", "ultralytics-thop"],
                 ["--no-deps", "ultralytics"])


def available():
    """True when torch and ultralytics can be imported."""
    return all(importlib.util.find_spec(name) is not None for name in ("torch", "ultralytics"))


def install_commands(python=None):
    python = python or sys.executable
    return [[python, "-m", "pip", "install", *step] for step in INSTALL_STEPS]


def install(log=print, cancel=None):
    """Install the add-on into the Python environment the app is running from."""
    if FROZEN:
        raise RuntimeError("The packaged app cannot install the model add-on. Run the app from source "
                           "(python main.py) to use trained models and SAM 2.")
    for step in INSTALL_STEPS:
        downloads.run_pip(step, log, cancel)
    importlib.invalidate_caches()
    log("Model add-on installed.")


# --------------------------------------------------------------------------- weight files

ASSET_RELEASE = "v8.4.0"
ASSET_URL = "https://github.com/ultralytics/assets/releases/download/{tag}/{name}"
LATEST_RELEASE_API = "https://api.github.com/repos/ultralytics/assets/releases/latest"


def asset_urls(name):
    """Where to download a pretrained weight file: the release this code was written for, then the latest."""
    yield ASSET_URL.format(tag=ASSET_RELEASE, name=name)
    try:
        import requests

        tag = requests.get(LATEST_RELEASE_API, timeout=10).json().get("tag_name")
        if tag and tag != ASSET_RELEASE:
            yield ASSET_URL.format(tag=tag, name=name)
    except Exception:
        return


def fetch_asset(name, log=print, cancel=None):
    """Download a pretrained weight file into models/ (nothing is done when it is already there)."""
    dest = MODELS_DIR / name
    if dest.is_file():
        log(f"{name} is already downloaded.")
        return dest
    error = None
    for url in asset_urls(name):
        try:
            return downloads.fetch(url, dest, log, cancel)
        except downloads.Cancelled:
            raise
        except RuntimeError as exc:
            error = exc
    raise error or RuntimeError(f"Could not download {name}")


def sam_weights():
    """Path of the SAM 2 weights when they have been downloaded, else None."""
    path = MODELS_DIR / SAM_WEIGHTS
    return path if path.is_file() else None


def download_sam(log=print, cancel=None):
    """Download the SAM 2 weights (about 160 MB). A measuring run never does this itself: a slow server
    would stall the run."""
    return fetch_asset(SAM_WEIGHTS, log, cancel)


def base_weights(task):
    """Path of the pretrained starting weights for training a "pose" or "seg" model, or None."""
    path = MODELS_DIR / BASE_WEIGHTS[task]
    return path if path.is_file() else None


def download_base_weights(log=print, cancel=None, tasks=("pose", "seg")):
    return [fetch_asset(BASE_WEIGHTS[task], log, cancel) for task in tasks]


def device():
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    return "0" if torch.cuda.is_available() else "cpu"


# --------------------------------------------------------------------------- registry of trained models

def load_registry():
    try:
        data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_registry(registry):
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2), encoding="utf-8")


def register(task, weights, **info):
    """Make `weights` the current model for a task ("seg" or "pose"); the previous one moves to history."""
    registry = load_registry()
    entry = {"version": info.pop("version", datetime.now().strftime("%Y%m%d-%H%M%S")),
             "weights": str(Path(weights).resolve().relative_to(MODELS_DIR.resolve()))
             if Path(weights).resolve().is_relative_to(MODELS_DIR.resolve()) else str(weights),
             "registered_at": datetime.now().isoformat(timespec="seconds"), **info}
    if registry.get(task):
        registry.setdefault("history", []).append({"task": task, **registry[task]})
    registry[task] = entry
    save_registry(registry)
    return entry


def current(task):
    """(weights path, version) of the registered model for a task, or (None, "")."""
    entry = load_registry().get(task) or {}
    if not entry.get("weights"):
        return None, ""
    path = Path(entry["weights"])
    path = path if path.is_absolute() else MODELS_DIR / path
    return (path, entry.get("version", "")) if path.is_file() else (None, "")


def status():
    """Plain sentences about the add-on and the trained models, for the Measure tab and the CLI."""
    lines = ["Model add-on (PyTorch, Ultralytics): " + ("installed" if available() else "not installed")]
    if available():
        lines.append("SAM 2 outlines: " + ("ready" if sam_weights() else
                                           "weights not downloaded yet (Model setup, or python main.py models download-sam)"))
    registry = load_registry()
    for task, what in (("pose", "Landmark model"), ("seg", "Outline model")):
        path, version = current(task)
        if path:
            entry = registry[task]
            lines.append(f"{what}: version {version}, trained on {entry.get('train_images', '?')} images")
        else:
            lines.append(f"{what}: not trained yet")
    return lines


# --------------------------------------------------------------------------- inference helpers

def _bgr(rgb):
    return np.ascontiguousarray(rgb[..., ::-1])


def _iou(a, b):
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def suppress(objects, threshold=0.5):
    """Keep the best-scoring object of each group of same-class boxes that overlap (non-maximum suppression)."""
    kept = []
    for obj in sorted(objects, key=lambda o: -(o.get("score") or 0)):
        if all(k["cls"] != obj["cls"] or _iou(k["box"], obj["box"]) < threshold for k in kept):
            kept.append(obj)
    return kept


def tiles(region, size=TILE, overlap=TILE_OVERLAP):
    """Overlapping square windows [x0, y0, x1, y1] covering a region (tiled inference for small objects)."""
    x0, y0, x1, y1 = [int(v) for v in region]
    step = max(1, int(size * (1 - overlap)))
    xs = list(range(x0, max(x0 + 1, x1 - size + 1), step)) or [x0]
    ys = list(range(y0, max(y0 + 1, y1 - size + 1), step)) or [y0]
    if xs[-1] + size < x1:
        xs.append(max(x0, x1 - size))
    if ys[-1] + size < y1:
        ys.append(max(y0, y1 - size))
    return [[x, y, min(x + size, x1), min(y + size, y1)] for y in ys for x in xs]


def _objects(result, offset=(0, 0), keypoint_names=None):
    """Plain dicts from one ultralytics result, shifted by a crop offset into sheet coordinates."""
    found = []
    if result.boxes is None:
        return found
    names = result.names
    boxes = result.boxes.xyxy.cpu().numpy()
    scores = result.boxes.conf.cpu().numpy()
    classes = result.boxes.cls.cpu().numpy().astype(int)
    polygons = result.masks.xy if getattr(result, "masks", None) is not None else None
    points = result.keypoints.xy.cpu().numpy() if getattr(result, "keypoints", None) is not None else None
    ox, oy = offset
    for i, box in enumerate(boxes):
        cls = names[int(classes[i])]
        obj = {"cls": cls, "box": [float(box[0] + ox), float(box[1] + oy), float(box[2] + ox), float(box[3] + oy)],
               "score": float(scores[i]), "polygon": None, "keypoints": None, "source": "trained"}
        if polygons is not None and i < len(polygons) and len(polygons[i]) >= 3:
            obj["polygon"] = [[round(float(x + ox), 1), round(float(y + oy), 1)] for x, y in polygons[i]]
        if points is not None and i < len(points):
            pair = (keypoint_names or KEYPOINT_NAMES).get(cls)
            if pair and len(points[i]) >= 2:
                obj["keypoints"] = {name: [round(float(points[i][k][0] + ox), 1), round(float(points[i][k][1] + oy), 1)]
                                    for k, name in enumerate(pair)}
        found.append(obj)
    return found


class TrainedModels:
    """The registered seg and pose models. Either may be missing; `ready` says whether any is usable."""

    def __init__(self, log=print):
        self.log = log
        self.paths = {task: current(task) for task in ("seg", "pose")}
        self.models = {}
        self._cache = (None, None)
        self.ready = available() and any(path for path, _ in self.paths.values())

    def has(self, task):
        return available() and self.paths[task][0] is not None

    @property
    def version(self):
        return " ".join(f"{task}:{version}" for task, (path, version) in self.paths.items() if path)

    def _model(self, task):
        if task not in self.models:
            from ultralytics import YOLO

            self.models[task] = YOLO(str(self.paths[task][0]))
        return self.models[task]

    def _predict(self, task, rgb, offset=(0, 0), imgsz=SHEET_IMGSZ):
        results = self._model(task).predict(source=_bgr(rgb), imgsz=imgsz, conf=MIN_SCORE, device=device(),
                                            verbose=False)
        return _objects(results[0], offset)

    def sheet(self, rgb):
        """Every object found on the whole sheet (cached for the sheet being measured).

        Pose detections are joined to the seg outline of the same class that overlaps them most, so
        an object can carry both its landmarks and its outline.
        """
        if self._cache[0] is rgb:
            return self._cache[1]
        seg = self._predict("seg", rgb) if self.has("seg") else []
        pose = self._predict("pose", rgb) if self.has("pose") else []
        objects = [o for o in seg if o["cls"] not in POSE_CLASSES or not pose]
        for obj in pose:
            match = max((s for s in seg if s["cls"] == obj["cls"]), key=lambda s: _iou(s["box"], obj["box"]),
                        default=None)
            if match and _iou(match["box"], obj["box"]) >= 0.3:
                obj["polygon"] = match["polygon"]
            objects.append(obj)
        for obj in seg:                                  # outlines the pose model has no landmarks for
            if obj["cls"] in POSE_CLASSES and pose and not any(o.get("polygon") is obj["polygon"] for o in pose):
                objects.append(obj)
        objects = suppress(objects)
        self._cache = (rgb, objects)
        return objects

    def seeds(self, rgb, regions):
        """Seeds found by tiled inference at native resolution inside `regions` (the panicle boxes).

        Seeds are a few dozen pixels long on a sheet of thousands; on the downsized sheet they vanish,
        so the models look at full-resolution tiles instead.
        """
        h, w = rgb.shape[:2]
        found = []
        for region in regions:
            clipped = [max(0, region[0]), max(0, region[1]), min(w, region[2]), min(h, region[3])]
            for x0, y0, x1, y1 in tiles(clipped):
                tile = rgb[y0:y1, x0:x1]
                if tile.shape[0] < 32 or tile.shape[1] < 32:
                    continue
                for task in ("pose", "seg"):
                    if self.has(task):
                        found += [o for o in self._predict(task, tile, (x0, y0), imgsz=TILE) if o["cls"] == "seed"]
        merged = []
        for obj in suppress(found):                      # join a seed's landmarks to its outline
            twin = next((m for m in merged if _iou(m["box"], obj["box"]) >= 0.3), None)
            if twin is None:
                merged.append(obj)
            else:
                twin["polygon"] = twin["polygon"] or obj["polygon"]
                twin["keypoints"] = twin["keypoints"] or obj["keypoints"]
        return merged


class SamRefiner:
    """SAM 2 outlines from a box or a click. Works on a crop around the prompt so detail is kept."""

    def __init__(self, weights=None):
        from ultralytics import SAM

        path = Path(weights) if weights else sam_weights()
        if path is None or not path.is_file():
            raise FileNotFoundError("the SAM 2 weights are not downloaded (python main.py models download-sam)")
        self.name = path.stem
        self.model = SAM(str(path))

    def outline(self, rgb, box=None, point=None, margin=0.25, min_side=256):
        """Outline [[x, y], ...] in sheet pixels of the object in `box` and/or under `point`, or None."""
        from .geometry import largest_blob, mask_polygon

        h, w = rgb.shape[:2]
        if box is None:
            half = max(min_side, 0.04 * max(h, w))
            box_for_crop = [point[0] - half, point[1] - half, point[0] + half, point[1] + half]
        else:
            box_for_crop = box
        bw, bh = box_for_crop[2] - box_for_crop[0], box_for_crop[3] - box_for_crop[1]
        pad_x, pad_y = max(bw * margin, min_side / 4), max(bh * margin, min_side / 4)
        x0, y0 = int(max(0, box_for_crop[0] - pad_x)), int(max(0, box_for_crop[1] - pad_y))
        x1, y1 = int(min(w, box_for_crop[2] + pad_x)), int(min(h, box_for_crop[3] + pad_y))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        prompts = {}
        if box is not None:
            prompts["bboxes"] = [[box[0] - x0, box[1] - y0, box[2] - x0, box[3] - y0]]
        if point is not None:
            prompts["points"], prompts["labels"] = [[point[0] - x0, point[1] - y0]], [1]
        results = self.model(_bgr(rgb[y0:y1, x0:x1]), device=device(), verbose=False, **prompts)
        masks = results[0].masks
        if masks is None or not len(masks.data):
            return None
        mask = largest_blob(masks.data[0].cpu().numpy().astype(np.uint8))
        return mask_polygon(mask, offset=(x0, y0), scale=1.0, tolerance=1.0)
