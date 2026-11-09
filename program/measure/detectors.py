"""Finding things on a sheet, behind one interface.

Two sources can find the sheet layout and the plant structures:

    trained models   YOLO seg/pose fine-tuned on reviewed sheets (trained.py); fast, repeatable, and
                     the only source that gives pixel-accurate outlines and a detection score
    vision model     a vision-language model asked for boxes and points (locator.py); the starting
                     point before any model is trained, and the source of the first labels

Detector uses the trained model wherever one is registered and the vision model for the rest, so the
pipeline moves from one to the other as models are trained. Every object it returns is a plain dict:

    {"cls": "panicle" | "leaf" | "seed", "box": [x0, y0, x1, y1] or None,
     "keypoints": {"base": [x, y], "tip": [x, y]} or {"a": ..., "b": ...} or None,
     "polygon": [[x, y], ...] or None, "score": 0-1 or None, "source": "trained" | "vision", ...}

in full-resolution pixels. Nothing here measures anything.
"""
from dataclasses import dataclass

from . import ocr, trained
from .geometry import point_along as _along
from .locator import CROP_MARGIN, LEAF_SIZE_CM, SEED_SIZE_CM, Locator

DETECTOR_CHOICES = {"Automatic (trained models when available)": "auto", "Vision model only": "vision",
                    "Trained models only": "trained"}
LABEL_CHOICES = {"Automatic (built-in OCR if installed)": "auto", "Built-in OCR": "ocr", "Vision model": "vision",
                 "Don't read labels": "off"}


@dataclass
class DetectorOptions:
    mode: str = "auto"              # auto | vision | trained
    refine_masks: bool = True       # outline objects with SAM 2 when the add-on is installed
    second_opinion: bool = False    # ask the vision model yes/no questions about each measured object
    read_labels: bool = True        # transcribe the voucher label
    label_reader: str = "auto"      # auto (built-in OCR if installed, else the vision model) | ocr | vision

    @classmethod
    def from_settings(cls, saved):
        saved = (saved or {}).get("detector") or {}
        return cls(**{k: saved[k] for k in cls.__dataclass_fields__ if k in saved})


class DetectorError(RuntimeError):
    """Nothing is available to find the structures with, worded for the user."""


def _box_around(points, margin, size):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return [max(0, min(xs) - margin), max(0, min(ys) - margin), min(size[0], max(xs) + margin),
            min(size[1], max(ys) + margin)]


class Detector:
    def __init__(self, locator=None, models=None, sam=None, options=None):
        self.locator, self.models, self.sam = locator, models, sam
        self.options = options or DetectorOptions()

    # ------------------------------------------------------------------ what is in use

    @property
    def organs_source(self):
        return "trained" if self.models and self.models.ready else "vision"

    @property
    def can_check(self):
        return bool(self.options.second_opinion and self.locator)

    @property
    def label_source(self):
        """"ocr", "vision" or "off": what reads the label on this run."""
        if not self.options.read_labels:
            return "off"
        mode = self.options.label_reader
        if mode == "ocr":
            return "ocr" if ocr.available() else "off"
        if mode == "vision":
            return "vision" if self.locator else "off"
        return "ocr" if ocr.available() else ("vision" if self.locator else "off")

    @property
    def can_read_labels(self):
        return self.label_source != "off"

    def read_voucher(self, image, rgb, layout, px_per_cm=None, taxon_names=(), control=None, status=None):
        """({"voucher_text", "voucher"}, raw answer) from the label reader chosen for this run."""
        if self.label_source == "ocr":
            if status:
                status("Reading the label (OCR)")
            return ocr.read_label(rgb, layout, px_per_cm, taxon_names)
        return self.locator.voucher(image, control, status)

    def versions(self):
        """What produced a measurement, stored with it."""
        parts = {}
        if self.models and self.models.ready:
            parts["trained"] = self.models.version
        if self.locator:
            parts["vision"] = f"{self.locator.client.provider}:{self.locator.client.model_id}"
        if self.sam:
            parts["masks"] = self.sam.name
        if self.label_source == "ocr":
            parts["labels"] = ocr.ENGINE_NAME
        return parts

    def describe(self):
        organs = (f"trained models ({self.models.version})" if self.organs_source == "trained"
                  else f"vision model {self.locator.client.model_id}")
        masks = "SAM 2 outlines" if self.sam else (
            "trained outlines" if self.models and self.models.has("seg") else "colour-based plant mask")
        extras = [name for name, on in (("second-opinion checks", self.can_check),
                                        ("labels read by " + ("built-in OCR" if self.label_source == "ocr"
                                                              else "the vision model"),
                                         self.can_read_labels)) if on]
        return f"structures from {organs}; {masks}" + ("; " + ", ".join(extras) if extras else "")

    # ------------------------------------------------------------------ layout

    def layout(self, image, rgb, control=None, status=None):
        """({"sheet_box", "non_plant_boxes", "ruler", "objects"}, raw answer)."""
        if self.models and self.models.has("seg"):
            if status:
                status("Finding labels and the ruler")
            objects = self.models.sheet(rgb)
            ruler = max((o for o in objects if o["cls"] == "ruler"), key=lambda o: o["score"], default=None)
            return {"sheet_box": None, "ruler": {"box": ruler["box"]} if ruler else None,
                    "non_plant_boxes": [o["box"] for o in objects if o["cls"] in trained.OCCLUDER_CLASSES],
                    "objects": [o for o in objects if o["cls"] in (*trained.OCCLUDER_CLASSES, "ruler")],
                    "source": "trained"}, {"source": "trained", "version": self.models.version}
        if self.locator:
            layout, raw = self.locator.layout(image, control, status)
            return {**layout, "objects": [], "source": "vision"}, raw
        return {"sheet_box": None, "ruler": None, "non_plant_boxes": [], "objects": [], "source": "none"}, {}

    # ------------------------------------------------------------------ plant structures

    def _trained(self, rgb, cls):
        return [dict(o) for o in self.models.sheet(rgb) if o["cls"] == cls]

    def panicles(self, image, rgb, control=None, status=None):
        if self.organs_source == "trained":
            if status:
                status("Finding panicles")
            found = sorted(self._trained(rgb, "panicle"), key=lambda o: -o["score"])[:2]
            return found, {"source": "trained", "found": len(found)}
        items, raw = self.locator.panicles(image, control, status)
        return [{"cls": "panicle", "box": item.get("box"), "keypoints": {"base": item["base"], "tip": item["tip"]},
                 "polygon": None, "score": None, "source": "vision", "visible": item.get("visible", "unsure"),
                 "first_guess": item.get("first_guess")} for item in items], raw

    def leaves(self, image, rgb, px_per_cm, avoid_boxes=(), control=None, status=None, limit=3):
        if self.organs_source == "trained":
            if status:
                status("Finding leaves")
            found = sorted(self._trained(rgb, "leaf"), key=lambda o: -o["score"])[:max(limit, 6)]
            return found, {"source": "trained", "found": len(found)}
        items, raw = self.locator.leaf_widths(image, px_per_cm, avoid_boxes, control, status, limit)
        half = LEAF_SIZE_CM * px_per_cm / 2
        return [{"cls": "leaf", "keypoints": {"a": item["a"], "b": item["b"]}, "polygon": None, "score": None,
                 "source": "vision", "centre": item.get("centre"),
                 "box": _box_around([item.get("centre") or item["a"]], half, image.size)} for item in items], raw

    def seeds(self, image, rgb, px_per_cm, panicles, control=None, status=None):
        """Seeds on the measured panicles. `panicles` are the panicle entries that were traced."""
        if not panicles:
            return [], {}
        if self.organs_source == "trained":
            if status:
                status("Finding seeds")
            regions = []
            for entry in panicles:
                points = entry.get("points") or list((entry.get("anchors") or {}).values())
                box = entry.get("box") or _box_around(points, 0, image.size)
                margin = CROP_MARGIN * max(box[2] - box[0], box[3] - box[1], px_per_cm)
                regions.append(_box_around([box[:2], box[2:]], margin, image.size))
            found = sorted(self.models.seeds(rgb, regions), key=lambda o: -o["score"])[:12]
            return found, {"source": "trained", "found": len(found), "regions": regions}
        best = max(panicles, key=lambda e: e.get("quality", 0))
        # Look where the spikelets are: on the traced panicle, a little above its middle. (The point
        # midway between base and tip is off the plant altogether when the panicle curves.)
        centre = _along(best.get("points") or list(best["anchors"].values()), 0.6)
        boxes, raw = self.locator.seed_boxes(image, px_per_cm, centre, control, status)
        half = SEED_SIZE_CM * px_per_cm / 2
        crop = _box_around([centre], half, image.size)
        return [{"cls": "seed", "box": box, "keypoints": None, "polygon": None, "score": None, "source": "vision",
                 "crop": crop} for box in boxes], raw

    # ------------------------------------------------------------------ outlines

    def outline(self, rgb, obj):
        """The object's outline: its own (trained seg) or one from SAM 2; None without the add-on."""
        if obj.get("polygon"):
            return obj["polygon"]
        if not self.sam:
            return None
        cls, points = obj.get("cls"), obj.get("keypoints") or {}
        try:
            if cls == "leaf" and points.get("a") and points.get("b"):
                middle = [(points["a"][0] + points["b"][0]) / 2, (points["a"][1] + points["b"][1]) / 2]
                return self.sam.outline(rgb, point=middle)
            if obj.get("box"):
                return self.sam.outline(rgb, box=obj["box"])
        except Exception:                                 # an outline is an extra; measuring goes on without it
            return None
        return None


def build_detector(model_settings=None, options=None, log=print):
    """The detector for a run, from the app's model settings and the Detector options.

    The vision model is opened only when it is needed: for the structures (no trained model yet),
    for second opinions, or for reading labels.
    """
    from .providers import open_client

    options = options or DetectorOptions()
    models = None
    if options.mode in ("auto", "trained") and trained.available():
        models = trained.TrainedModels(log)
        if not models.ready:
            models = None
    if options.mode == "trained" and models is None:
        raise DetectorError("No trained model is available. " + (
            "Review some sheets and train a model first (Review tab)." if trained.available()
            else "Install the model add-on first: python main.py models install"))
    if options.read_labels and options.label_reader == "ocr" and not ocr.available():
        raise DetectorError("The built-in label reader is not installed. Install it in Model setup, or choose "
                            "the vision model for labels.")
    labels_need_vision = options.read_labels and (options.label_reader == "vision" or (
        options.label_reader == "auto" and not ocr.available()))
    need_vision = options.mode == "vision" or models is None or options.second_opinion or labels_need_vision
    locator = None
    if need_vision and model_settings is not None:
        try:
            client = open_client(model_settings, log)
        except Exception:
            if models is None or options.mode == "vision":
                raise
            log("  The vision model is not reachable; continuing with the trained models only "
                "(no label reading or second-opinion checks).")
        else:
            for warning in client.warnings:
                log(f"Warning: {warning}")
            locator = Locator(client)
    if models is None and locator is None:
        raise DetectorError("Choose a vision model on the Measure tab, or train a model first.")
    if options.mode == "vision":
        models = None
    sam = None
    if options.refine_masks and trained.available():
        if trained.sam_weights() is None:
            log("  SAM 2 outlines are skipped: its weights are not downloaded yet "
                "(run python main.py models download-sam once). Using the colour-based plant mask instead.")
        else:
            try:
                sam = trained.SamRefiner()
            except Exception as exc:
                log(f"  SAM 2 could not be loaded ({exc}); using the colour-based plant mask instead.")
    return Detector(locator, models, sam, options)
