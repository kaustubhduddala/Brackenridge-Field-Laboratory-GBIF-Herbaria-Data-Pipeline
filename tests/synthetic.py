"""A drawn herbarium sheet with known measurements, and a stand-in for the vision model.

The sheet is cream paper at PPC pixels per centimetre with a printed millimetre ruler, a label, a
curved panicle with spikelets, flat and folded leaf blades. Because every structure is drawn from
known numbers, the pipeline's answers can be checked exactly.
"""
import math

import cv2
import numpy as np

PPC = 120.0                                  # pixels per centimetre
W_CM, H_CM = 29.0, 42.0
PAPER, PLANT, SEED, INK = (236, 226, 200), (125, 100, 55), (105, 80, 40), (40, 40, 40)
SEED_CM = (0.42, 0.16)                       # spikelet length and width
LEAF_CM = 1.5                                # width of the flat leaf blade
PANICLE = {"centre": (14.0, 24.0), "radius": 12.0, "start": math.radians(200), "end": math.radians(290)}


def px(cm):
    return int(round(cm * PPC))


def panicle_points(n=400):
    c, r = PANICLE["centre"], PANICLE["radius"]
    return [(c[0] + r * math.cos(t), c[1] + r * math.sin(t)) for t in np.linspace(PANICLE["start"], PANICLE["end"], n)]


def panicle_length_cm():
    return PANICLE["radius"] * (PANICLE["end"] - PANICLE["start"])


def seed_centres():
    """(x, y, angle, branch x, branch y): spikelets on short branches either side of the upper panicle axis."""
    c, out = PANICLE["centre"], []
    for i, (x, y) in enumerate(panicle_points(40)[14:38:3]):
        ux, uy = (x - c[0]) / PANICLE["radius"], (y - c[1]) / PANICLE["radius"]
        side = 1.0 if i % 2 else -1.0
        out.append((x + side * 0.9 * ux, y + side * 0.9 * uy, 20 + 17 * i, x, y))
    return out


def make_sheet(label_over_tip=False, folded_leaf=False, margin_cm=1.0):
    """(rgb image, facts). The sheet sits on a dark background `margin_cm` wide so its edges can be found."""
    h, w = px(H_CM + 2 * margin_cm), px(W_CM + 2 * margin_cm)
    image = np.full((h, w, 3), (60, 60, 62), np.uint8)
    ox = oy = px(margin_cm)
    cv2.rectangle(image, (ox, oy), (ox + px(W_CM), oy + px(H_CM)), PAPER, -1)

    def at(x_cm, y_cm):
        return (ox + px(x_cm), oy + px(y_cm))

    # printed ruler along the bottom edge: millimetre ticks, longer every centimetre
    y0 = oy + px(H_CM - 1.6)
    cv2.rectangle(image, (ox + px(2), y0 - px(0.2)), (ox + px(22.4), y0 + px(1.0)), (252, 252, 250), -1)
    for mm in range(0, 201):
        x = ox + px(2.2) + int(round(mm * PPC / 10))
        cv2.line(image, (x, y0), (x, y0 + px(0.7 if mm % 10 == 0 else 0.45 if mm % 5 == 0 else 0.3)), INK, 2)

    # panicle: a curved main axis with side branches and spikelets
    axis = np.array([at(x, y) for x, y in panicle_points()], np.int32)
    cv2.polylines(image, [axis], False, PLANT, max(3, px(0.12)))
    for x, y, angle, bx, by in seed_centres():
        cv2.line(image, at(bx, by), at(x, y), PLANT, 2)
        cv2.ellipse(image, at(x, y), (px(SEED_CM[0] / 2), px(SEED_CM[1] / 2)), angle, 0, 360, SEED, -1)
    cv2.line(image, at(*panicle_points()[0]), at(panicle_points()[0][0] - 0.6, 36.5), PLANT, max(4, px(0.2)))  # stem

    # a flat leaf blade, gently curved, LEAF_CM wide
    leaf = np.array([at(20.5 + 1.2 * math.sin(t), 8 + 9 * t) for t in np.linspace(0, 2, 60)], np.int32)
    cv2.polylines(image, [leaf], False, (150, 140, 80), px(LEAF_CM))
    if folded_leaf:                          # a second blade folded back over part of the first
        cv2.line(image, at(21.3, 15.0), at(22.6, 18.5), (140, 128, 70), px(LEAF_CM))

    label = [ox + px(1.0), oy + px(30.0), ox + px(11.0), oy + px(39.0)]
    if label_over_tip:                       # the label hides the last stretch of the panicle
        tip = panicle_points()[-1]
        label = [ox + px(tip[0] - 4.5), oy + px(tip[1] - 2.0), ox + px(tip[0] + 4.5), oy + px(tip[1] + 3.0)]
    cv2.rectangle(image, tuple(label[:2]), tuple(label[2:]), (250, 250, 247), -1)
    cv2.rectangle(image, tuple(label[:2]), tuple(label[2:]), (120, 120, 120), 2)
    for i in range(4):
        cv2.line(image, (label[0] + 30, label[1] + 60 + 70 * i), (label[2] - 60, label[1] + 60 + 70 * i), INK, 3)

    points = panicle_points()
    facts = {"px_per_cm": PPC, "panicle_cm": panicle_length_cm(), "leaf_cm": LEAF_CM, "seed_cm": SEED_CM[0],
             "base": list(at(*points[0])), "tip": list(at(*points[-1])), "label": label,
             "leaf_centre": list(at(20.5 + 1.2 * math.sin(1.0), 17.0)),
             "seeds": [list(at(x, y)) for x, y, *_ in seed_centres()], "sheet": [ox, oy, ox + px(W_CM), oy + px(H_CM)],
             "image": image}
    return image, facts


class FakeLocator:
    """Second opinions with fixed answers."""

    class client:
        provider, model_id = "fake", "fake-vision"

    def __init__(self, tip="visible", flat=True, whole=True):
        self.tip, self.flat, self.whole = tip, flat, whole

    def check_panicle(self, image, box, control=None, status=None):
        return {"tip": self.tip, "base": "visible"}, {}

    def check_leaf(self, image, box, control=None, status=None):
        return {"flat": self.flat, "problem": "none" if self.flat else "folded"}, {}

    def check_seeds(self, image, crop, boxes, control=None, status=None):
        return [self.whole] * len(boxes), {}


def fake_detector(facts, tip_error_cm=0.8, outlines=False, locator=None, second_opinion=False, tip=None):
    """A detector that points at the drawn structures the way a model would: roughly.

    The panicle tip is placed `tip_error_cm` short of the real tip, leaf margins a little outside the
    blade, seeds as loose boxes. With `outlines` it also returns exact outlines, as a trained
    segmentation model or SAM 2 would.
    """
    from program.measure.detectors import Detector, DetectorOptions

    class Fake(Detector):
        def versions(self):
            return {"trained": "fake"}

        def describe(self):
            return "a stand-in detector for tests"

        def layout(self, image, rgb, control=None, status=None):
            return {"sheet_box": None, "ruler": None, "non_plant_boxes": [facts["label"]], "objects": [],
                    "source": "fake"}, {}

        def panicles(self, image, rgb, control=None, status=None):
            points = [(facts["sheet"][0] + px(x), facts["sheet"][1] + px(y)) for x, y in panicle_points()]
            short = int(len(points) * tip_error_cm / panicle_length_cm())
            marked = list(tip) if tip else list(points[-1 - short])
            return [{"cls": "panicle", "box": None, "polygon": None, "score": 0.9, "source": "trained",
                     "keypoints": {"base": [points[0][0] + 20, points[0][1] - 15], "tip": marked}}], {}

        def leaves(self, image, rgb, px_per_cm, avoid_boxes=(), control=None, status=None, limit=3):
            cx, cy = facts["leaf_centre"]
            reach = 0.62 * LEAF_CM * PPC                  # the marks land a little outside the blade
            obj = {"cls": "leaf", "box": None, "polygon": None, "score": 0.9, "source": "trained",
                   "keypoints": {"a": [cx - reach, cy + 6], "b": [cx + reach, cy - 6]}}
            if outlines:
                obj["polygon"] = leaf_outline(facts)
            return [obj], {}

        def seeds(self, image, rgb, px_per_cm, panicles, control=None, status=None):
            half = 0.45 * PPC
            return [{"cls": "seed", "box": [x - half, y - half, x + half, y + half], "keypoints": None,
                     "polygon": None, "score": 0.85, "source": "trained"} for x, y in facts["seeds"]], {}

    return Fake(locator, None, None, DetectorOptions(read_labels=False, second_opinion=second_opinion))


def leaf_outline(facts):
    """The exact outline of the leaf blade (and of anything drawn over it), from the image itself."""
    image = facts["image"]
    lab = cv2.cvtColor(image, cv2.COLOR_RGB2LAB).astype(np.int16)
    blade = ((np.abs(lab[..., 0] - 148) < 14) & (lab[..., 2] > 150)).astype(np.uint8)
    count, labels = cv2.connectedComponents(blade)
    cx, cy = [int(v) for v in facts["leaf_centre"]]
    blob = (labels == labels[cy, cx]).astype(np.uint8)
    contour = max(cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0], key=cv2.contourArea)
    return [[float(x), float(y)] for x, y in contour[:, 0, :]]


class FakeVisionClient:
    """Answers the real Locator's prompts from the sheet's facts, in the pixels of the image it is sent."""
    provider, model_id, max_image_side, warnings = "anthropic", "fake-vision", 20000, []

    def __init__(self, facts):
        self.facts, self.asked, self.crop = facts, [], None

    def attach(self, locator):
        """Let the client see which crop the locator is about to send (a real model sees it in the image)."""
        prepare = locator._prepare

        def remembering(image, crop=None):
            self.crop = crop
            return prepare(image, crop)

        locator._prepare = remembering
        return locator

    def _xy(self, point, sent):
        x0, y0, x1, y1 = self.crop or [0, 0, *self.facts["image"].shape[1::-1]]
        return {"x": (point[0] - x0) * sent.size[0] / (x1 - x0), "y": (point[1] - y0) * sent.size[1] / (y1 - y0)}

    def _box(self, box, sent):
        a, b = self._xy(box[:2], sent), self._xy(box[2:], sent)
        return {"x0": a["x"], "y0": a["y"], "x1": b["x"], "y1": b["y"]}

    def ask_json(self, images, system, text, schema=None, name="answer", control=None, status=None, stage=""):
        self.asked.append(name)
        sent, f = images[-1], self.facts
        half = 0.3 * PPC
        answers = {
            "sheet_layout": lambda: {"sheet": self._box(f["sheet"], sent), "ruler_present": False, "ruler_unit": "none",
                                     "ruler_box": self._box([0, 0, 0, 0], sent),
                                     "ruler_tick_a": {"x": 0, "y": 0, "value": 0},
                                     "ruler_tick_b": {"x": 0, "y": 0, "value": 0},
                                     "non_plant_boxes": [self._box(f["label"], sent)]},
            "panicles": lambda: {"panicles": [{"tip": self._xy(f["tip"], sent), "base": self._xy(f["base"], sent),
                                               "box": self._box([min(f["base"][0], f["tip"][0]), f["tip"][1] - 200,
                                                                 max(f["base"][0], f["tip"][0]), f["base"][1]], sent),
                                               "visible": "whole"}]},
            "panicle_ends": lambda: {"tip": self._xy(f["tip"], sent), "base": self._xy(f["base"], sent)},
            "leaf_positions": lambda: {"leaves": [self._xy(f["leaf_centre"], sent)]},
            "leaf_width": lambda: {"left_margin": self._xy([f["leaf_centre"][0] - 0.6 * LEAF_CM * PPC, f["leaf_centre"][1]], sent),
                                   "right_margin": self._xy([f["leaf_centre"][0] + 0.6 * LEAF_CM * PPC, f["leaf_centre"][1]], sent),
                                   "usable": True},
            "spikelet_boxes": lambda: {"spikelets": [self._box([x - half, y - half, x + half, y + half], sent)
                                                     for x, y in f["seeds"]
                                                     if self.crop[0] + half < x < self.crop[2] - half
                                                     and self.crop[1] + half < y < self.crop[3] - half]},
            "panicle_check": lambda: {"tip": "visible", "base": "visible"},
            "leaf_check": lambda: {"flat": True, "problem": "none"},
            "spikelet_check": lambda: {"boxes": [{"n": n, "one_whole_spikelet": True} for n in range(1, 9)]},
            "specimen_voucher": lambda: {"voucher_text": "HERBARIUM | Panicum maximum",
                                         "voucher": {"scientificName": "Panicum maximum"}},
        }
        answer = answers[name]()
        return answer, {"fake": name, "parsed": answer}
