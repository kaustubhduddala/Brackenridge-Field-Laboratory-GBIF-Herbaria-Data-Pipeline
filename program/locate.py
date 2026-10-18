"""Ask a vision model WHERE things are on a herbarium sheet. It never measures anything.

The model answers with points and boxes; plant.py and calibration.py then measure from the
pixels. Points use named x/y fields so models cannot mix up the axis order, and each provider's
coordinate convention is converted back to full-resolution pixels here.
"""
from PIL import Image

# How each provider reports positions. Qwen-VL (LM Studio's usual vision models) and Gemini are
# trained on 0-1000 normalised coordinates; current Claude models answer in pixels of the image sent.
COORDINATES = {"lmstudio": "norm1000", "custom": "norm1000", "openai": "norm1000", "gemini": "norm1000",
               "anthropic": "pixels"}

SYSTEM = ("You are a botanist helping to measure dried Megathyrsus maximus (Guinea grass) herbarium specimens. "
          "You only point at things; software does the measuring. Be precise: put points exactly on the "
          "structure described. Answer with JSON only.")


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
                "visible": {"type": "string", "enum": ["whole", "partly covered", "unsure"]},
            },
            "required": ["tip", "base", "visible"], "additionalProperties": False}},
    },
    "required": ["panicles"],
    "additionalProperties": False,
}


def _convention_text(convention, size):
    w, h = size
    if convention == "pixels":
        return (f"The image is {w} x {h} pixels. Give every coordinate in pixels of this image: x from 0 (left) "
                f"to {w} (right), y from 0 (top) to {h} (bottom).")
    return ("Give every coordinate normalised to 0-1000: x from 0 (left edge) to 1000 (right edge), "
            "y from 0 (top edge) to 1000 (bottom edge).")


class Locator:
    """Wraps a provider client; converts the model's coordinates to full-resolution pixels."""

    def __init__(self, client):
        self.client = client
        self.convention = COORDINATES.get(client.provider, "norm1000")

    def _prepare(self, image, crop=None):
        """(image to send, function mapping model coordinates to full-res pixels)."""
        region = image.crop(tuple(int(round(v)) for v in crop)) if crop else image
        sent = region.copy()
        side = self.client.max_image_side
        if max(sent.size) > side:
            sent.thumbnail((side, side), Image.Resampling.LANCZOS)
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

    def panicles(self, image, control=None, status=None):
        prompt = (
            "Find each panicle (the flowering seed head: a loose, branched cluster of thin branches carrying "
            "small oval spikelets) on this sheet. Do not confuse it with leaves (flat ribbons) or stems.\n"
            "For each panicle (at most 2, the most complete first) give:\n"
            "- tip: the top end of the panicle's main axis;\n"
            "- base: the lowest panicle node, where the lowest branch of the panicle leaves the main stem;\n"
            "- visible: whole, partly covered, or unsure.\n"
            "Return an empty list if there is no panicle.")
        parsed, raw, to_full = self._ask(image, prompt, PANICLE_SCHEMA, "panicles", control, status,
                                         "Locating panicles")
        found = []
        for item in (parsed.get("panicles") or [])[:2]:
            tip, base = item.get("tip") or {}, item.get("base") or {}
            found.append({"tip": to_full(tip.get("x", 0), tip.get("y", 0)),
                          "base": to_full(base.get("x", 0), base.get("y", 0)),
                          "visible": item.get("visible", "unsure")})
        return found, raw

    def voucher(self, image, control=None, status=None):
        sent, _ = self._prepare(image)
        parsed, raw = self.client.ask_json([sent], SYSTEM, VOUCHER_PROMPT, VOUCHER_SCHEMA, "specimen_voucher", control,
                                           status, "Reading the label")
        voucher = parsed.get("voucher") if isinstance(parsed.get("voucher"), dict) else {}
        return {"voucher_text": str(parsed.get("voucher_text") or ""),
                "voucher": {f: str(voucher.get(f) or "") for f in VOUCHER_FIELDS}}, raw


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

