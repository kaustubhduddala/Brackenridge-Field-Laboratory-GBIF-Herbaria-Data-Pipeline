"""Ask a vision model WHERE things are on a herbarium sheet. It never measures anything.

The model answers with points and boxes; plant.py and calibration.py then measure from the
pixels. Points use named x/y fields so models cannot mix up the axis order, and each provider's
coordinate convention is converted back to full-resolution pixels here.
"""
from pathlib import Path

from PIL import Image

MIN_CROP_SIDE = 900
REFERENCE_DIR = Path(__file__).parent / "reference"
_reference_cache = {}


def reference_image(name):
    """An exemplar figure from the lab's measurement protocol (program/reference/<name>.jpg)."""
    if name not in _reference_cache:
        _reference_cache[name] = Image.open(REFERENCE_DIR / f"{name}.jpg").convert("RGB")
    return _reference_cache[name]

# How each provider reports positions. Qwen-VL (LM Studio's usual vision models) and Gemini are
# trained on 0-1000 normalised coordinates; current Claude models answer in pixels of the image sent.
COORDINATES = {"lmstudio": "norm1000", "custom": "norm1000", "openai": "norm1000", "gemini": "norm1000",
               "anthropic": "pixels"}

SYSTEM = ("You are a botanist helping to measure dried Megathyrsus maximus (Guinea grass) herbarium specimens. "
          "You only point at things; software does the measuring. Be precise: put points exactly on the "
          "structure described. Answer with JSON only.")


# What each trait is, as defined by the lab's protocol (G064 Herbaria Image Measurement Protocol, 3.3).
PROTOCOL_DEFINITIONS = (
    "Lab protocol definitions:\n"
    "- Panicle length: from the LOWEST PANICLE NODE (the lowest point where a whorl of side branches leaves the "
    "main axis; below it the stem is bare) up along the main axis to the TIP of the panicle.\n"
    "- Leaf width: from margin to margin (edge to edge) across the leaf blade at its widest point, measured "
    "straight across the blade, perpendicular to the leaf's length. Use a flat, fully visible blade.\n"
    "- Seed length: one spikelet (the small enclosed capsule at the end of a panicle branch) measured from the point "
    "where it attaches to its branch to its tip. Spikelets are about 0.4-0.6 cm long.")

PANICLE_EXAMPLES = (
    "Reference figures from the lab protocol come first, then the specimen sheet to analyse.\n"
    "Reference 1 shows a panicle: the arrow runs from the lowest node (where the lowest branches leave the bare "
    "stem) to the tip.\n"
    "Reference 2 shows a whole herbarium sheet: the arrow marks one panicle, the thin feathery seed head at the top "
    "of a tall stem, not the leaves (flat ribbons).\n"
    "Reference 3 shows a panicle that is folded or hidden by leaves: the line is drawn from the lowest point where "
    "the seed branches taper in toward the stem up to the tip.\n")


def _point(desc):
    return {"type": "object", "description": desc,
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
            "required": ["x", "y"], "additionalProperties": False}


def _box(desc):
    return {"type": "object", "description": desc,
            "properties": {k: {"type": "number"} for k in ("x0", "y0", "x1", "y1")},
            "required": ["x0", "y0", "x1", "y1"], "additionalProperties": False}


LAYOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "sheet": _box("the mounting paper the plant is glued to"),
        "ruler_present": {"type": "boolean"},
        "ruler_unit": {"type": "string", "enum": ["cm", "inch", "none"]},
        "ruler_box": _box("tight box around the printed scale bar or ruler (all zeros if none)"),
        "ruler_tick_a": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                                                          "value": {"type": "number"}},
                         "required": ["x", "y", "value"], "additionalProperties": False},
        "ruler_tick_b": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                                                          "value": {"type": "number"}},
                         "required": ["x", "y", "value"], "additionalProperties": False},
        "non_plant_boxes": {"type": "array", "items": _box("label, barcode, envelope, stamp or colour chart")},
    },
    "required": ["sheet", "ruler_present", "ruler_unit", "ruler_box", "ruler_tick_a", "ruler_tick_b",
                 "non_plant_boxes"],
    "additionalProperties": False,
}

PANICLE_SCHEMA = {
    "type": "object",
    "properties": {
        "panicles": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "tip": _point("the very top end of the panicle's main axis"),
                "base": _point("the lowest panicle node: where the lowest branch of the inflorescence leaves "
                               "the main stem"),
                "box": _box("box around the whole panicle, from its tip down to its lowest branch"),
                "visible": {"type": "string", "enum": ["whole", "partly covered", "unsure"]},
            },
            "required": ["box", "tip", "base", "visible"], "additionalProperties": False}},
    },
    "required": ["panicles"],
    "additionalProperties": False,
}

# Second look at one panicle on a zoomed crop: small models tend to stop the panicle where its
# spikelets thin out, well above the lowest branch.
PANICLE_REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "tip": _point("the very top end of the panicle's main axis"),
        "base": _point("the lowest panicle node, where the lowest branch leaves the main stem"),
    },
    "required": ["tip", "base"],
    "additionalProperties": False,
}
LEAF_SCHEMA = {
    "type": "object",
    "properties": {"leaves": {"type": "array", "items": _point(
        "centre of the widest part of one flat, fully visible leaf blade")}},
    "required": ["leaves"], "additionalProperties": False,
}
LEAF_WIDTH_SCHEMA = {
    "type": "object",
    "properties": {
        "left_margin": _point("point on one edge (margin) of the leaf blade, at its widest part"),
        "right_margin": _point("point on the opposite edge of the same blade, straight across from left_margin"),
        "usable": {"type": "boolean"},
    },
    "required": ["left_margin", "right_margin", "usable"], "additionalProperties": False,
}
SEED_SCHEMA = {
    "type": "object",
    "properties": {"seeds": {"type": "array", "items": {
        "type": "object",
        "properties": {"base": _point("where the spikelet attaches to its branch"),
                       "tip": _point("the far end of the same spikelet")},
        "required": ["base", "tip"], "additionalProperties": False}}},
    "required": ["seeds"], "additionalProperties": False,
}
SEED_BOX_SCHEMA = {
    "type": "object",
    "properties": {"spikelets": {"type": "array", "items": _box("tight box around one whole spikelet")}},
    "required": ["spikelets"], "additionalProperties": False,
}
LEAF_SIZE_CM = 7.0          # side of the zoomed crop used to place the two margin points
SEED_SIZE_CM = 3.0
CROP_MARGIN = 0.25          # share of the panicle box added on each side of the zoomed crop


def _convention_text(convention, size):
    w, h = size
    if convention == "pixels":
        return (f"The image is {w} x {h} pixels. Give every coordinate in pixels of this image: x from 0 (left) "
                f"to {w} (right), y from 0 (top) to {h} (bottom).")
    return ("Give every coordinate normalised to 0-1000: x from 0 (left edge) to 1000 (right edge), "
            "y from 0 (top edge) to 1000 (bottom edge).")


class Locator:
    """Wraps a provider client; converts the model's coordinates to full-resolution pixels."""

    def __init__(self, client, use_references=False, seed_mode="points"):
        self.client = client
        self.use_references = use_references
        self.seed_mode = seed_mode          # "points" (base/tip) or "boxes" (box, then axis found by plant.py)
        self.convention = COORDINATES.get(client.provider, "norm1000")

    def _prepare(self, image, crop=None):
        """(image to send, function mapping model coordinates to full-res pixels)."""
        region = image.crop(tuple(int(round(v)) for v in crop)) if crop else image
        sent = region.copy()
        side = self.client.max_image_side
        if max(sent.size) > side:
            sent.thumbnail((side, side), Image.Resampling.LANCZOS)
        elif crop and max(sent.size) < MIN_CROP_SIDE:   # a small close-up: enlarge so fine structures are visible
            factor = MIN_CROP_SIDE / max(sent.size)
            sent = sent.resize((round(sent.width * factor), round(sent.height * factor)), Image.Resampling.LANCZOS)
        ox, oy = (crop[0], crop[1]) if crop else (0, 0)
        rw, rh = region.size
        sw, sh = sent.size

        def to_full(x, y):
            if self.convention == "pixels":
                fx, fy = x / sw, y / sh
            else:
                fx, fy = x / 1000.0, y / 1000.0
            fx, fy = min(max(fx, 0.0), 1.0), min(max(fy, 0.0), 1.0)
            return [round(ox + fx * rw, 1), round(oy + fy * rh, 1)]

        return sent, to_full

    def _ask(self, image, prompt, schema, name, control, status, stage, crop=None):
        sent, to_full = self._prepare(image, crop)
        text = prompt + "\n\n" + _convention_text(self.convention, sent.size)
        parsed, raw = self.client.ask_json([sent], SYSTEM, text, schema, name, control, status, stage)
        return parsed, raw, to_full

    def layout(self, image, control=None, status=None):
        prompt = (
            "Describe the layout of this herbarium sheet image.\n"
            "- sheet: box around the mounting paper.\n"
            "- ruler: is there a printed scale bar or ruler with numbered ticks? If yes, give its unit "
            "(cm or inch), a tight box around it, and two numbered ticks far apart (ruler_tick_a, ruler_tick_b): "
            "the position of each tick mark and the number printed at it. If there is no ruler, set "
            "ruler_present false, unit none and all ruler coordinates and values to 0.\n"
            "- non_plant_boxes: boxes around labels, barcodes, envelopes/packets, stamps and colour charts.")
        parsed, raw, to_full = self._ask(image, prompt, LAYOUT_SCHEMA, "sheet_layout", control, status,
                                         "Locating sheet and ruler")

        def box(b):
            if not isinstance(b, dict):
                return None
            p0, p1 = to_full(b.get("x0", 0), b.get("y0", 0)), to_full(b.get("x1", 0), b.get("y1", 0))
            box_ = [min(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[0], p1[0]), max(p0[1], p1[1])]
            return box_ if box_[2] - box_[0] > 2 and box_[3] - box_[1] > 2 else None

        result = {"sheet_box": box(parsed.get("sheet")),
                  "non_plant_boxes": [b for b in map(box, parsed.get("non_plant_boxes") or []) if b],
                  "ruler": None}
        unit = parsed.get("ruler_unit")
        ta, tb = parsed.get("ruler_tick_a") or {}, parsed.get("ruler_tick_b") or {}
        rbox = box(parsed.get("ruler_box"))
        if parsed.get("ruler_present") and unit in ("cm", "inch") and rbox:
            pa, pb = to_full(ta.get("x", 0), ta.get("y", 0)), to_full(tb.get("x", 0), tb.get("y", 0))
            span_units = abs(float(tb.get("value", 0)) - float(ta.get("value", 0)))
            span_px = ((pb[0] - pa[0]) ** 2 + (pb[1] - pa[1]) ** 2) ** 0.5
            cm = span_units * (2.54 if unit == "inch" else 1.0)
            if cm > 0 and span_px > 10:
                result["ruler"] = {"box": rbox, "px_per_cm": span_px / cm, "unit": unit,
                                   "ticks": [pa + [ta.get("value")], pb + [tb.get("value")]]}
        return result, raw

    def _ask_with_references(self, image, references, prompt, schema, name, control, status, stage, crop=None):
        """Like _ask, but reference figures are sent before the specimen image."""
        sent, to_full = self._prepare(image, crop)
        text = prompt + "\n\n" + _convention_text(self.convention, sent.size)
        shots = [reference_image(r) for r in references] if self.use_references else []
        parsed, raw = self.client.ask_json(shots + [sent], SYSTEM, text, schema, name, control, status, stage)
        return parsed, raw, to_full

    def panicles(self, image, control=None, status=None):
        prompt = (
            PANICLE_EXAMPLES if self.use_references else "") + PROTOCOL_DEFINITIONS + (
            "\n\nFind each panicle (the flowering seed head: a loose, branched cluster of thin branches carrying "
            "small oval spikelets) on the specimen sheet (the LAST image). Do not confuse it with leaves (flat "
            "ribbons) or stems.\n"
            "For each panicle (at most 2, the most complete first) give:\n"
            "- box: a box around the whole panicle;\n"
            "- tip: the top end of the panicle's main axis;\n"
            "- base: the lowest panicle node, where the lowest branch of the panicle leaves the main stem;\n"
            "- visible: whole, partly covered, or unsure.\n"
            "Return an empty list if there is no panicle.")
        parsed, raw, to_full = self._ask_with_references(
            image, ("panicle_diagram", "panicle_on_sheet", "panicle_covered"), prompt, PANICLE_SCHEMA, "panicles",
            control, status, "Locating panicles")
        found = []
        for item in (parsed.get("panicles") or [])[:2]:
            tip, base, box = item.get("tip") or {}, item.get("base") or {}, item.get("box") or {}
            found.append({"tip": to_full(tip.get("x", 0), tip.get("y", 0)),
                          "base": to_full(base.get("x", 0), base.get("y", 0)),
                          "box": [*to_full(box.get("x0", 0), box.get("y0", 0)),
                                  *to_full(box.get("x1", 0), box.get("y1", 0))],
                          "visible": item.get("visible", "unsure")})
        raw = {"panicles": raw, "refine": []}
        for item in found:
            crop = self._panicle_crop(image.size, item)
            try:
                refined, refine_raw, crop_to_full = self._ask_with_references(
                    image, ("panicle_diagram",), PROTOCOL_DEFINITIONS + "\n\n" + PANICLE_REFINE_PROMPT,
                    PANICLE_REFINE_SCHEMA, "panicle_ends", control, status, "Refining panicle ends", crop=crop)
            except (ValueError, KeyError) as exc:   # keep the first answer if the second look fails
                raw["refine"].append({"error": str(exc)})
                continue
            raw["refine"].append(refine_raw)
            tip, base = refined.get("tip") or {}, refined.get("base") or {}
            if {"x", "y"} <= tip.keys() and {"x", "y"} <= base.keys():
                item["first_guess"] = {"tip": item["tip"], "base": item["base"]}
                item["tip"], item["base"] = crop_to_full(tip["x"], tip["y"]), crop_to_full(base["x"], base["y"])
        return found, raw

    @staticmethod
    def _panicle_crop(size, item):
        """The panicle box (and both points) with a margin, clipped to the image."""
        w, h = size
        xs = [item["box"][0], item["box"][2], item["tip"][0], item["base"][0]]
        ys = [item["box"][1], item["box"][3], item["tip"][1], item["base"][1]]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        mx, my = max(x1 - x0, 0.05 * w) * CROP_MARGIN, max(y1 - y0, 0.05 * h) * CROP_MARGIN
        return [max(0, x0 - mx), max(0, y0 - my), min(w, x1 + mx), min(h, y1 + my)]

    def leaf_widths(self, image, px_per_cm, avoid_boxes=(), control=None, status=None, limit=3):
        """[{"a": point, "b": point}] across the widest part of up to `limit` leaf blades, plus raw answers."""
        prompt = (PROTOCOL_DEFINITIONS + "\n\nFind up to %d leaf blades on this sheet that are flat, fully visible and "
                  "not folded, curled, torn or hidden under other leaves. A leaf blade is a flat green or tan ribbon; "
                  "do not pick stems, sheaths wrapped round a stem, or the panicle. For each give the centre of its "
                  "widest part. Return an empty list if none is suitable." % limit)
        parsed, raw, to_full = self._ask(image, prompt, LEAF_SCHEMA, "leaf_positions", control, status,
                                         "Locating leaves")
        out, raws = [], {"positions": raw, "widths": []}
        for centre in (parsed.get("leaves") or [])[:limit]:
            cx, cy = to_full(centre.get("x", 0), centre.get("y", 0))
            if any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in avoid_boxes or ()):
                continue
            half = LEAF_SIZE_CM * px_per_cm / 2
            crop = [max(0, cx - half), max(0, cy - half), min(image.width, cx + half), min(image.height, cy + half)]
            try:
                ans, ans_raw, crop_to_full = self._ask_with_references(
                    image, ("leaf_width",), PROTOCOL_DEFINITIONS + (
                        "\n\nThe first image is a lab reference figure: the short line goes straight across the "
                        "leaf blade from margin to margin. The LAST image is a close-up of a leaf. Mark the two "
                        "margins of the blade at its widest point, on a line perpendicular to the blade's length. "
                        "Set usable false if the blade is folded, overlapped or its edges are unclear."),
                    LEAF_WIDTH_SCHEMA, "leaf_width", control, status, "Measuring a leaf", crop=crop)
            except (ValueError, KeyError) as exc:
                raws["widths"].append({"error": str(exc)})
                continue
            raws["widths"].append(ans_raw)
            if not ans.get("usable", True):
                continue
            a, b = ans.get("left_margin") or {}, ans.get("right_margin") or {}
            if {"x", "y"} <= a.keys() and {"x", "y"} <= b.keys():
                out.append({"a": crop_to_full(a["x"], a["y"]), "b": crop_to_full(b["x"], b["y"])})
        return out, raws

    def seed_lengths(self, image, px_per_cm, centre, control=None, status=None):
        """[{"base": point, "tip": point}] for a few intact spikelets near `centre`, plus the raw answer."""
        half = SEED_SIZE_CM * px_per_cm / 2
        crop = [max(0, centre[0] - half), max(0, centre[1] - half), min(image.width, centre[0] + half),
                min(image.height, centre[1] + half)]
        prompt = (PROTOCOL_DEFINITIONS + "\n\nThis is a close-up of part of a panicle. Pick up to 4 separate spikelets "
                  "that are intact, in focus and not overlapping others. A spikelet is a small, pointed, boat-shaped "
                  "capsule about 2.5 times longer than it is wide. For each, mark base and tip so that the segment "
                  "covers the WHOLE spikelet: base at its very bottom end where it joins the branch, tip at its very "
                  "top end. A segment that stops halfway is wrong. Each spikelet has its own length and direction; "
                  "do not reuse one segment. Skip any you cannot see clearly." + (
                      " The first image is a lab reference figure; the LAST image is the close-up."
                      if self.use_references else ""))
        parsed, raw, to_full = self._ask_with_references(image, ("seed_length",), prompt, SEED_SCHEMA, "spikelets",
                                                         control, status, "Measuring seeds", crop=crop)
        out = []
        for item in (parsed.get("seeds") or [])[:4]:
            base, tip = item.get("base") or {}, item.get("tip") or {}
            if {"x", "y"} <= base.keys() and {"x", "y"} <= tip.keys():
                out.append({"base": to_full(base["x"], base["y"]), "tip": to_full(tip["x"], tip["y"])})
        return out, raw

    def seed_boxes(self, image, px_per_cm, centre, control=None, status=None, size_cm=None):
        """Tight boxes (full-res pixels) around up to 6 intact spikelets near `centre`, plus the raw answer.

        Models are far better at boxing an object than at marking both ends of it; plant.spikelet_axis
        then finds the spikelet's long axis inside each box.
        """
        half = (size_cm or SEED_SIZE_CM) * px_per_cm / 2
        crop = [max(0, centre[0] - half), max(0, centre[1] - half), min(image.width, centre[0] + half),
                min(image.height, centre[1] + half)]
        prompt = (PROTOCOL_DEFINITIONS + "\n\nThis is a close-up of part of a panicle. Draw a tight bounding box "
                  "around each of up to 6 separate spikelets (small, pointed, boat-shaped capsules about 2.5 times "
                  "longer than wide) that are intact, in focus and not overlapping others. Each box must contain "
                  "exactly one whole spikelet, from its bottom end to its tip, and no neighbours. Skip unclear ones.")
        parsed, raw, to_full = self._ask(image, prompt, SEED_BOX_SCHEMA, "spikelet_boxes", control, status,
                                         "Finding seeds", crop=crop)
        out = []
        for b in (parsed.get("spikelets") or [])[:6]:
            p0, p1 = to_full(b.get("x0", 0), b.get("y0", 0)), to_full(b.get("x1", 0), b.get("y1", 0))
            box = [min(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[0], p1[0]), max(p0[1], p1[1])]
            if box[2] - box[0] > 2 and box[3] - box[1] > 2:
                out.append(box)
        return out, raw

    def voucher(self, image, control=None, status=None):
        sent, _ = self._prepare(image)
        parsed, raw = self.client.ask_json([sent], SYSTEM, VOUCHER_PROMPT, VOUCHER_SCHEMA, "specimen_voucher", control,
                                           status, "Reading the label")
        voucher = parsed.get("voucher") if isinstance(parsed.get("voucher"), dict) else {}
        return {"voucher_text": str(parsed.get("voucher_text") or ""),
                "voucher": {f: str(voucher.get(f) or "") for f in VOUCHER_FIELDS}}, raw


PANICLE_REFINE_PROMPT = (
    "The first image is a lab reference figure; the LAST image is a close-up of one panicle (the branched seed head) of a dried grass specimen. Follow the panicle's "
    "main axis from its top down past every branch that carries spikelets.\n"
    "- tip: the top end of the main axis;\n"
    "- base: the lowest panicle node, where the LOWEST branch leaves the main stem. Below it the stem is bare "
    "or wrapped in a leaf sheath. Panicles are often long, so check the lower part of the image too.")


VOUCHER_FIELDS = ["catalogNumber", "recordedBy", "recordNumber", "eventDate", "country", "stateProvince",
                  "county", "locality", "habitat", "verbatimElevation", "identifiedBy", "scientificName"]
VOUCHER_SCHEMA = {
    "type": "object",
    "properties": {
        "voucher_text": {"type": "string"},
        "voucher": {"type": "object", "properties": {f: {"type": "string"} for f in VOUCHER_FIELDS},
                    "required": VOUCHER_FIELDS, "additionalProperties": False},
    },
    "required": ["voucher_text", "voucher"],
    "additionalProperties": False,
}
VOUCHER_PROMPT = (
    "Transcribe the herbarium specimen label (usually in a bottom corner) into Darwin Core fields.\n"
    "- voucher_text: all readable printed and handwritten label text on one line, parts separated by \" | \".\n"
    "- voucher: catalogNumber, recordedBy, recordNumber, eventDate (YYYY-MM-DD, YYYY-MM or YYYY), country, "
    "stateProvince, county, locality, habitat, verbatimElevation, identifiedBy, scientificName. Use \"\" for "
    "anything not written on the label.")

