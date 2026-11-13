"""Turn reviewed labels into training sets and fine-tune the models (needs the model add-on).

    build_dataset()   YOLO-format datasets from the label store, split by specimen 70/15/15
    train()           fine-tunes a pretrained YOLO pose or seg model and registers the result

Two models are trained from the same labels:

    pose   classes panicle, leaf, seed, each with its two landmarks. Reviewing sheets in the app
           produces exactly these labels, so this is the first model that can be trained.
    seg    outlines of panicle, leaf, seed, label, ruler, envelope and colour chart. Needs objects
           with outlines: from SAM 2 or a trained model accepted in review, or drawn in CVAT.

Whole sheets teach panicles, leaves and labels. Seeds are a few dozen pixels on a sheet of thousands,
so they are taught on native-resolution tiles instead, and found the same way (trained.TrainedModels.seeds).
A reviewer marks only a few of the hundreds of seeds on a panicle; on a seed tile everything except
the marked seeds is therefore painted out with paper colour, so unmarked seeds do not teach the model
that seeds are background.

The honest test of a model is not its detection score but the end measurement: after training, run
`python main.py benchmark <hand csv> --measure --split test` to score the held-out sheets in cm.
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

import config
from config import load_settings
from . import trained
from .labels import LABELS_DIR_NAME, load_labels
from .trained import CLASSES, KEYPOINT_NAMES, MODELS_DIR, POSE_CLASSES, TILE
from ..utils import index_images

DATASETS_DIR = MODELS_DIR / "datasets"
SPLITS = (("train", 0.70), ("val", 0.15), ("test", 0.15))
SHEET_MAX_SIDE = 2560           # whole sheets are stored at this size; labels are normalised, so nothing is lost
MIN_SHEETS = 10                 # fewer labelled sheets than this cannot be split and trained on sensibly
SEED_PAD = 0.6                  # share of a seed box kept around it on a painted-out tile
DEFAULTS = {"epochs": 150, "imgsz": 1280, "batch": 8}
DEVICE_BATCH = {"mps": 2, "cpu": 4}     # Apple GPUs share memory with the system, so they run out first
MIN_IMGSZ = 640
DEVICE_CHOICES = {"auto": "Automatic", "mps": "Apple GPU", "cuda": "NVIDIA GPU", "cpu": "CPU"}
IMGSZ_STEPS = (1280, 1120, 1024, 960, 800, 640)
# Rough training memory for one image at 640 px with the medium models, and the share of a computer's memory
# that training can count on (Apple GPUs and CPUs share it with everything else that is running).
GB_PER_IMAGE_640 = 0.6
MEMORY_SHARE = {"mps": 0.35, "cuda": 0.8, "cpu": 0.4}


def system_memory_gb():
    """Installed memory in GB, or None when it cannot be found."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            return int(out.stdout) / 1024 ** 3
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return None


def hardware():
    """What this computer can train on: {"device": "mps" | "cuda" | "cpu", "name", "memory_gb"} (memory is the GPU's
    own for NVIDIA, the system's otherwise). Without PyTorch it assumes an Apple GPU on an Apple-silicon Mac."""
    memory = system_memory_gb()
    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            return {"device": "cuda", "name": props.name, "memory_gb": props.total_memory / 1024 ** 3}
        if torch.backends.mps.is_available():
            return {"device": "mps", "name": "Apple GPU", "memory_gb": memory}
        return {"device": "cpu", "name": "CPU", "memory_gb": memory}
    except ImportError:
        apple = sys.platform == "darwin" and platform.machine() == "arm64"
        return {"device": "mps" if apple else "cpu", "name": "Apple GPU" if apple else "CPU", "memory_gb": memory}


def recommend(device, memory_gb, train_images=None):
    """Settings suited to a device with `memory_gb` and `train_images` training sheets (None when unknown):
    the largest image size that still allows a batch of at least 2 (4 on an NVIDIA GPU), the largest
    power-of-two batch that fits, and enough epochs for about 1500 steps."""
    budget = (memory_gb or 8) * MEMORY_SHARE.get(device, 0.4)
    smallest = 4 if device == "cuda" else 2

    def fits(size):
        return int(budget / (GB_PER_IMAGE_640 * (size / 640) ** 2))

    size = next((s for s in IMGSZ_STEPS if fits(s) >= smallest), MIN_IMGSZ)
    batch = 1
    while batch * 2 <= min(fits(size), 16):
        batch *= 2
    epochs = DEFAULTS["epochs"]
    if train_images:
        epochs = min(300, max(50, round(1500 * batch / train_images / 10) * 10))
    return {"device": "auto", "imgsz": size, "batch": batch, "epochs": epochs}


def saved_settings():
    """The ML settings saved from File > ML settings; anything missing is the default (batch None = by device)."""
    saved = load_settings().get("training")
    saved = saved if isinstance(saved, dict) else {}
    settings = {**DEFAULTS, "batch": None, "device": "auto"}      # no batch saved: train() picks one for the device
    for key in ("epochs", "imgsz", "batch"):
        try:
            if int(saved.get(key)) > 0:
                settings[key] = int(saved[key])
        except (TypeError, ValueError):
            pass
    if saved.get("device") in DEVICE_CHOICES:
        settings["device"] = saved["device"]
    return settings


def is_out_of_memory(exc):
    text = str(exc).lower()
    return "out of memory" in text or "outofmemory" in text or "insufficient memory" in text


def free_gpu_memory():
    try:
        import gc
        import torch
        gc.collect()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def split_of(gbif_id, group=None):
    """train / val / test for a specimen, the same every time. `group` keeps a collection together."""
    digest = hashlib.sha1(str(group or gbif_id).encode()).digest()
    share = int.from_bytes(digest[:4], "big") / 2 ** 32
    total = 0.0
    for name, part in SPLITS:
        total += part
        if share < total:
            return name
    return SPLITS[-1][0]


def assign_splits(ids, groups=None):
    """{gbifID: split}. Small label sets still get at least one validation and one test sheet."""
    groups = groups or {}
    splits = {i: split_of(i, groups.get(i)) for i in sorted(ids)}
    for needed in ("val", "test"):
        if needed not in splits.values():
            donors = [i for i, s in splits.items() if s == "train"]
            if len(donors) > 1:
                splits[donors[-1 if needed == "val" else 0]] = needed
    return splits


def gather_labels(folders=None):
    """[(gbifID, label, image path)] from the label stores of the given downloads (all of them by default)."""
    if folders is None:
        folders = [p for p in config.DATA_DIR.iterdir() if (p / LABELS_DIR_NAME).is_dir()] if config.DATA_DIR.is_dir() else []
    found = []
    for folder in folders:
        folder = Path(folder)
        images = index_images(folder / "media")
        for gbif_id, label in load_labels(folder / LABELS_DIR_NAME).items():
            if gbif_id in images and label["objects"]:
                found.append((gbif_id, label, images[gbif_id]))
    return found


def _norm(value, size):
    return min(max(value / size, 0.0), 1.0)


def pose_line(obj, size, offset=(0, 0)):
    """One YOLO pose label line: class, box centre and size, then x y visibility for both landmarks."""
    names = KEYPOINT_NAMES[obj["cls"]]
    w, h = size
    x0, y0, x1, y1 = [obj["box"][0] - offset[0], obj["box"][1] - offset[1], obj["box"][2] - offset[0],
                      obj["box"][3] - offset[1]]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    parts = [POSE_CLASSES.index(obj["cls"]), _norm((x0 + x1) / 2, w), _norm((y0 + y1) / 2, h),
             _norm(x1 - x0, w), _norm(y1 - y0, h)]
    for name in names:
        x, y = obj["keypoints"][name][0] - offset[0], obj["keypoints"][name][1] - offset[1]
        inside = 0 <= x <= w and 0 <= y <= h
        parts += [_norm(x, w), _norm(y, h), 2 if inside else 0]
    return " ".join(str(p) if isinstance(p, int) else f"{p:.6f}" for p in parts)


def seg_line(obj, size, offset=(0, 0)):
    """One YOLO seg label line: class, then the outline's points. A box stands in when there is no outline."""
    w, h = size
    polygon = obj.get("polygon")
    if not polygon or len(polygon) < 3:
        x0, y0, x1, y1 = obj["box"]
        polygon = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    points = [f"{_norm(x - offset[0], w):.6f} {_norm(y - offset[1], h):.6f}" for x, y in polygon]
    return f"{CLASSES.index(obj['cls'])} " + " ".join(points)


def usable(obj, task):
    """Can this labelled object teach this model?"""
    if (obj.get("attrs") or {}).get("exclude") or not obj.get("box"):
        return False
    if task == "pose":
        names = KEYPOINT_NAMES.get(obj["cls"])
        return bool(names) and all((obj.get("keypoints") or {}).get(n) for n in names)
    # seg: a real outline, or a box for the rectangular things (labels, rulers, envelopes, charts)
    return obj["cls"] in CLASSES and (bool(obj.get("polygon")) or obj["cls"] not in POSE_CLASSES)


def seed_tiles(seeds, size, tile=TILE):
    """Tiles [x0, y0, x1, y1] that together hold every labelled seed, each with the seeds inside it."""
    w, h = size
    left, tiles = list(seeds), []
    while left:
        cx, cy = (left[0]["box"][0] + left[0]["box"][2]) / 2, (left[0]["box"][1] + left[0]["box"][3]) / 2
        x0, y0 = int(min(max(0, cx - tile / 2), max(0, w - tile))), int(min(max(0, cy - tile / 2), max(0, h - tile)))
        box = [x0, y0, min(w, x0 + tile), min(h, y0 + tile)]
        inside = [s for s in left if box[0] <= s["box"][0] and s["box"][2] <= box[2]
                  and box[1] <= s["box"][1] and s["box"][3] <= box[3]] or [left[0]]
        tiles.append((box, inside))
        left = [s for s in left if not any(s is i for i in inside)]
    return tiles


def painted_tile(rgb, box, seeds):
    """The tile with everything but the labelled seeds painted out in the tile's paper colour."""
    x0, y0, x1, y1 = box
    tile = rgb[y0:y1, x0:x1]
    brightest = tile.reshape(-1, 3)[tile.reshape(-1, 3).sum(axis=1) >= np.percentile(tile.sum(axis=2), 70)]
    out = np.empty_like(tile)
    out[:] = np.median(brightest, axis=0).astype(np.uint8) if len(brightest) else 230
    for seed in seeds:
        sx0, sy0, sx1, sy1 = seed["box"]
        pad_x, pad_y = SEED_PAD * (sx1 - sx0), SEED_PAD * (sy1 - sy0)
        a, b = max(0, int(sx0 - pad_x) - x0), max(0, int(sy0 - pad_y) - y0)
        c, d = min(x1 - x0, int(sx1 + pad_x) - x0), min(y1 - y0, int(sy1 + pad_y) - y0)
        out[b:d, a:c] = tile[b:d, a:c]
    return out


def build_dataset(task, labelled=None, out_dir=None, groups=None, log=print):
    """Write a YOLO dataset for "pose" or "seg". Returns {"yaml", "dir", "counts", "splits"}."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    labelled = gather_labels() if labelled is None else labelled
    labelled = [(i, label, path) for i, label, path in labelled if any(usable(o, task) for o in label["objects"])]
    if len(labelled) < MIN_SHEETS:
        raise ValueError(f"Only {len(labelled)} labelled sheets can teach the {task} model; at least {MIN_SHEETS} "
                         "are needed. " + ("Accept more sheets on the Review tab." if task == "pose" else
                                           "Outlines come from SAM 2 or from labels drawn in CVAT."))
    out_dir = Path(out_dir) if out_dir else DATASETS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}_{task}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    splits = assign_splits([i for i, _, _ in labelled], groups)
    counts = {name: {"images": 0, "objects": 0} for name, _ in SPLITS}
    line = pose_line if task == "pose" else seg_line
    for gbif_id, label, image_path in labelled:
        split = splits[gbif_id]
        image_dir, label_dir = out_dir / "images" / split, out_dir / "labels" / split
        image_dir.mkdir(parents=True, exist_ok=True)
        label_dir.mkdir(parents=True, exist_ok=True)
        objects = [o for o in label["objects"] if usable(o, task)]
        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
        size = image.size
        whole = [o for o in objects if o["cls"] != "seed"]
        if whole:
            small = image.copy()
            small.thumbnail((SHEET_MAX_SIDE, SHEET_MAX_SIDE))
            small.save(image_dir / f"{gbif_id}.jpg", "JPEG", quality=92)
            (label_dir / f"{gbif_id}.txt").write_text("\n".join(line(o, size) for o in whole) + "\n")
            counts[split]["images"] += 1
            counts[split]["objects"] += len(whole)
        seeds = [o for o in objects if o["cls"] == "seed"]
        if seeds:
            rgb = np.asarray(image)
            for n, (box, inside) in enumerate(seed_tiles(seeds, size), start=1):
                tile = painted_tile(rgb, box, inside)
                tile_size = (box[2] - box[0], box[3] - box[1])
                Image.fromarray(tile).save(image_dir / f"{gbif_id}_seeds{n}.jpg", "JPEG", quality=95)
                (label_dir / f"{gbif_id}_seeds{n}.txt").write_text(
                    "\n".join(line(o, tile_size, box[:2]) for o in inside) + "\n")
                counts[split]["images"] += 1
                counts[split]["objects"] += len(inside)
    names = POSE_CLASSES if task == "pose" else CLASSES
    lines = [f"path: {out_dir}", "train: images/train", "val: images/val", "test: images/test", "names:"]
    lines += [f"  {n}: {name}" for n, name in enumerate(names)]
    if task == "pose":
        lines += ["kpt_shape: [2, 3]", "flip_idx: [0, 1]"]
    yaml_path = out_dir / "data.yaml"
    yaml_path.write_text("\n".join(lines) + "\n")
    (out_dir / "splits.json").write_text(json.dumps(splits, indent=2))
    log(f"{task} dataset: " + ", ".join(f"{name} {c['images']} images / {c['objects']} objects"
                                         for name, c in counts.items()) + f" in {out_dir}")
    return {"yaml": yaml_path, "dir": out_dir, "counts": counts, "splits": splits}


def latest_splits():
    """{gbifID: split} of the most recently built dataset (for benchmarking the held-out sheets)."""
    if not DATASETS_DIR.is_dir():
        return {}
    for folder in sorted(DATASETS_DIR.iterdir(), reverse=True):
        try:
            return json.loads((folder / "splits.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def train(task, labelled=None, base=None, epochs=None, imgsz=None, batch=None, log=print, groups=None,
          device=None):
    """Fine-tune a pretrained YOLO model on the labels and make it the current model for `task`.

    Starts from pretrained weights (never from scratch), with augmentations that mimic old scans:
    rotation, brightness, scale. Returns the registry entry.
    """
    if task not in ("pose", "seg"):
        raise ValueError("Train 'pose' (landmarks) or 'seg' (outlines)")
    if not trained.available():
        raise RuntimeError("The model add-on is not installed. Install it in File > Model setup, or run: python main.py models install")
    dataset = build_dataset(task, labelled, groups=groups, log=log)
    from ultralytics import YOLO

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    version = datetime.now().strftime("%Y%m%d-%H%M%S")
    previous, _ = trained.current(task)
    start = Path(base) if base else (previous or MODELS_DIR / trained.BASE_WEIGHTS[task])
    if not start.is_file():
        if base:
            raise FileNotFoundError(f"The starting weights {base} do not exist")
        trained.fetch_asset(trained.BASE_WEIGHTS[task], log)       # about 45 MB, once
    saved = saved_settings()          # File > ML settings; what the caller passes wins over it
    device = trained.device(device or saved["device"])
    log(f"Training the {task} model from {start.name} on {device} "
        f"({dataset['counts']['train']['images']} training images). This takes a while.")
    size = int(imgsz or saved["imgsz"])
    epochs = int(epochs or saved["epochs"])
    per_step = int(batch or saved["batch"] or DEVICE_BATCH.get(device, DEFAULTS["batch"]))
    while True:
        model = YOLO(str(start))
        try:
            results = model.train(
                data=str(dataset["yaml"]), imgsz=size, epochs=epochs, batch=per_step,
                device=device, project=str(MODELS_DIR / task), name=version, exist_ok=True, plots=False,
                verbose=False, degrees=20.0, scale=0.4, hsv_v=0.3, hsv_s=0.4, fliplr=0.5, flipud=0.1, mosaic=0.3)
            break
        except Exception as exc:
            if not is_out_of_memory(exc):
                raise
            del model
            free_gpu_memory()
            if per_step > 1:                       # first fewer images per step, then smaller images
                per_step = max(1, per_step // 2)
            elif size > MIN_IMGSZ:
                size = max(MIN_IMGSZ, int(size * 0.75) // 32 * 32)
            else:
                raise RuntimeError("Training ran out of memory even at batch 1 and image size "
                                   f"{MIN_IMGSZ}. Close other apps, or train on a computer with more memory.") from exc
            log(f"Out of memory on {device}; trying again with batch {per_step} and image size {size}.")
    save_dir = Path(getattr(results, "save_dir", MODELS_DIR / task / version))
    weights = save_dir / "weights" / "best.pt"
    if not weights.is_file():
        raise RuntimeError(f"Training finished without producing {weights}")
    metrics = {k: round(float(v), 4) for k, v in (getattr(results, "results_dict", None) or {}).items()
               if isinstance(v, (int, float))}
    entry = trained.register(task, weights, version=version, base=start.name, metrics=metrics,
                             train_images=dataset["counts"]["train"]["images"], dataset=str(dataset["dir"]),
                             classes=POSE_CLASSES if task == "pose" else CLASSES)
    log(f"Training complete: {task} model version {version} is now in use. "
        + ("Key scores: " + ", ".join(f"{k.split('/')[-1]} {v}" for k, v in list(metrics.items())[:4]) if metrics else ""))
    return entry
