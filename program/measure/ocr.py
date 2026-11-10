"""Built-in label reading: open-source OCR that runs on this computer, with no vision model or internet.

The engine is RapidOCR (Apache-2.0): the PaddleOCR text detector and recognizer converted to ONNX and
run on the CPU. Its models are inside the package, so nothing is downloaded when it runs. It is
optional: install it from Model setup (or `python main.py models install-ocr`).

What it does and does not do. It reads printed text well: typed labels, stamps, barcode numbers. It
reads handwriting poorly. It returns the text of the label; the fields it can pull out of that text
with simple rules are listed in FIELDS_FOUND, the rest stay blank and the full text is kept in
voucher_text. A vision model understands a label better, so it stays available as a choice.
"""
import importlib
import importlib.util
import re
import statistics
from datetime import date

import numpy as np

from . import downloads
from .locator import VOUCHER_FIELDS

ENGINE_NAME = "rapidocr"
# onnxruntime and friends first; rapidocr itself with --no-deps, because it asks for opencv-python and
# the app already has opencv-python-headless (a second copy would sit on top of it).
INSTALL_STEPS = (["onnxruntime", "pyclipper", "shapely", "pyyaml", "six"], ["--no-deps", "rapidocr-onnxruntime"])
MIN_SCORE = 0.5                 # lines the engine is less sure of than this are dropped
MAX_SIDE = 2000                 # regions are shrunk to this long side before reading
STRIP_FROM = 0.60               # with no label boxes, read the bottom 40% of the sheet
MAX_REGIONS = 6
MIN_REGION_CM2 = 3.0            # label boxes smaller than this are skipped (stamps, specks)
FIELDS_FOUND = ("catalogNumber", "eventDate", "scientificName", "recordedBy", "recordNumber", "county",
                "locality", "habitat")


def available():
    return all(importlib.util.find_spec(name) is not None for name in ("rapidocr_onnxruntime", "onnxruntime"))


def install_commands(python=None):
    import sys

    return [[python or sys.executable, "-m", "pip", "install", *step] for step in INSTALL_STEPS]


def install(log=print, cancel=None):
    for step in INSTALL_STEPS:
        downloads.run_pip(step, log, cancel)
    importlib.invalidate_caches()
    downloads.verify_imports(("rapidocr_onnxruntime",), log)
    log("Label reader installed.")


# --------------------------------------------------------------------------- the engine

class Reader:
    """RapidOCR on a numpy RGB image. Loading the models takes about a second, so keep one."""

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR

        self.engine = RapidOCR()

    def lines(self, rgb):
        """[{"text", "score", "x", "y", "h"}] for the text lines found in `rgb`, in its own pixels."""
        import cv2

        image, scale = rgb, 1.0
        if max(image.shape[:2]) > MAX_SIDE:
            scale = MAX_SIDE / max(image.shape[:2])
            image = cv2.resize(image, (max(1, int(image.shape[1] * scale)), max(1, int(image.shape[0] * scale))),
                               interpolation=cv2.INTER_AREA)
        result, _ = self.engine(np.ascontiguousarray(image[..., ::-1]))
        found = []
        for box, text, score in result or []:
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            found.append({"text": str(text).strip(), "score": float(score), "x": float(np.mean(xs)) / scale,
                          "y": float(np.mean(ys)) / scale, "h": float(max(ys) - min(ys)) / scale})
        return found


_reader = None


def default_reader():
    global _reader
    if _reader is None:
        _reader = Reader()
    return _reader


# --------------------------------------------------------------------------- where to read

def _iou(a, b):
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def regions_for(shape, layout=None, px_per_cm=None):
    """[(name, (x0, y0, x1, y1))] to read, in pixels.

    The label and envelope boxes of the layout when there are any (the ruler's box is left out, and
    boxes too small to hold a label). With none, the bottom of the sheet in overlapping halves,
    because labels are almost always in a bottom corner.
    """
    h, w = shape[:2]
    layout = layout or {}
    objects = [o for o in layout.get("objects") or [] if o.get("cls") in ("label", "envelope") and o.get("box")]
    boxes = [o["box"] for o in objects] or list(layout.get("non_plant_boxes") or [])
    ruler = (layout.get("ruler") or {}).get("box")
    boxes = [b for b in boxes if not (ruler and _iou(b, ruler) > 0.5)]
    smallest = MIN_REGION_CM2 * px_per_cm ** 2 if px_per_cm else 0.002 * w * h
    boxes = sorted((b for b in boxes if (b[2] - b[0]) * (b[3] - b[1]) >= smallest),
                   key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))[:MAX_REGIONS]
    regions = []
    for n, box in enumerate(boxes, start=1):
        pad_x, pad_y = 0.03 * (box[2] - box[0]), 0.03 * (box[3] - box[1])
        region = (max(0, int(box[0] - pad_x)), max(0, int(box[1] - pad_y)),
                  min(w, int(box[2] + pad_x)), min(h, int(box[3] + pad_y)))
        if region[2] - region[0] >= 20 and region[3] - region[1] >= 20:
            regions.append((f"label {n}", region))
    if regions:
        return regions
    pieces = max(1, round(w / 2600))
    step = w / pieces
    top = int(STRIP_FROM * h)
    return [(f"bottom of the sheet {n}", (max(0, int(n * step - 0.04 * step)), top,
                                           min(w, int((n + 1) * step + 0.04 * step)), h)) for n in range(pieces)]


def assemble(lines):
    """One text from the lines of a region: rows top to bottom, words left to right, rows joined by ' | '."""
    keep = [l for l in lines if l["score"] >= MIN_SCORE and len(l["text"]) >= 2]
    if not keep:
        return ""
    keep.sort(key=lambda l: l["y"])
    rows, current = [], [keep[0]]
    for line in keep[1:]:
        height = statistics.median(l["h"] for l in current) or 1.0
        if abs(line["y"] - statistics.fmean(l["y"] for l in current)) < 0.6 * height:
            current.append(line)
        else:
            rows.append(current)
            current = [line]
    rows.append(current)
    return " | ".join(" ".join(l["text"] for l in sorted(row, key=lambda l: l["x"])) for row in rows)


# --------------------------------------------------------------------------- fields from the text

MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"), start=1)}
MONTH_WORDS = "|".join(MONTHS)
CATALOG = re.compile(r"\b([A-Z]{1,6}-?\d{5,})\b")
NOT_CATALOG = ("ADBC", "NSF", "ISBN", "ISSN", "DOI", "HTTP", "WWW")   # grant numbers and the like
SCIENTIFIC = re.compile(r"\b([A-Z][a-z]{3,})\s+([a-z]{4,})\b")
KEYWORDS = {                    # a field name printed on the label; the text after it is the value
    "recordedBy": r"(?:collectors?|collected by|coll\.|leg\.)",
    "recordNumber": r"(?:collection no\.?|coll\.? no\.?|collector'?s? no\.?|number|no\.)",
    "county": r"(?:county|co\.)",
    "locality": r"(?:locality|loc\.)",
    "habitat": r"(?:habitat|ecology)",
}


def _valid_year(year):
    return 1700 <= year <= date.today().year


def _iso(year, month=None, day=None):
    if not _valid_year(year):
        return ""
    if month and 1 <= month <= 12:
        if day and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}"


DATE_PATTERNS = (               # (pattern, how to read it); lookarounds instead of \\b so "Date_8-17-1936" works
    (re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"), "ymd"),
    (re.compile(rf"(?<!\d)(\d{{1,2}})[ .-]*({MONTH_WORDS})[a-z]*\.?,?[ .-]*(\d{{4}})(?!\d)", re.I), "dmy-name"),
    (re.compile(rf"\b({MONTH_WORDS})[a-z]*\.?[ ]*(?:(\d{{1,2}})(?:st|nd|rd|th)?)?,?[ ]*(\d{{4}})(?!\d)", re.I),
     "mdy-name"),
    (re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?!\d)"), "numeric"),
)


def _dates(text):
    """Every date in the text as (year, month or None, day or None)."""
    found = []
    for pattern, kind in DATE_PATTERNS:
        for m in pattern.finditer(text):
            g = m.groups()
            if kind == "ymd":
                found.append((int(g[0]), int(g[1]), int(g[2])))
            elif kind == "dmy-name":
                found.append((int(g[2]), MONTHS[g[1].lower()], int(g[0])))
            elif kind == "mdy-name":
                found.append((int(g[2]), MONTHS[g[0].lower()], int(g[1]) if g[1] else None))
            else:
                a, b, year = int(g[0]), int(g[1]), int(g[2])
                if b > 12 >= a:
                    found.append((year, a, b))
                elif a > 12 >= b:
                    found.append((year, b, a))
                else:
                    found.append((year, None, None))        # 3-4-1936 could be either way round: keep the year
    found = [d for d in found if _valid_year(d[0])]
    for m in re.finditer(r"(?<!\d)(1[7-9]\d\d|20\d\d)(?!\d)", re.sub(CATALOG, " ", text)):
        if _valid_year(int(m[1])):
            found.append((int(m[1]), None, None))
    return found


def find_date(text):
    """The collection date as YYYY-MM-DD, YYYY-MM or YYYY, or ''.

    The text after the word "Date" comes first. Otherwise the earliest date wins, since a label also
    carries the later dates of re-identifications and annotations, and a full date beats a bare year.
    """
    after = re.search(r"(?i)\bdated?[\W_]*(.{0,30})", text)
    for scope in ([after[1]] if after else []) + [text]:
        dates = _dates(scope)
        if dates:
            fullest = max(sum(part is not None for part in d) for d in dates)
            best = min(d for d in dates if sum(part is not None for part in d) == fullest)
            return _iso(*best)
    return ""


def find_name(text, genera):
    """The first 'Genus epithet' in the text whose genus is one of `genera`; names are never guessed."""
    for genus, epithet in SCIENTIFIC.findall(text):
        if genus in genera:
            return f"{genus} {epithet}"
    return ""


def find_keyword(text, pattern, limit=50):
    m = re.search(rf"(?i)\b{pattern}(?![a-z])[\s:_.]*([^|]+)", text)
    if not m:
        return ""
    value = re.split(r"(?i)\s+(?:no\.?|date|county|locality|habitat|collector|elev(?:ation)?|det(?:ermined)?)\b",
                     m[1].strip())[0]
    return value.strip(" .:;,-")[:limit]


def parse_fields(text, taxon_names=()):
    """Darwin Core fields that simple rules can pull out of label text; every other field is ''."""
    fields = {name: "" for name in VOUCHER_FIELDS}
    catalog = [c for c in CATALOG.findall(text) if not c.startswith(NOT_CATALOG)]
    if catalog:
        fields["catalogNumber"] = max(catalog, key=lambda c: sum(ch.isdigit() for ch in c))
    fields["eventDate"] = find_date(text)
    genera = {name.split()[0] for name in taxon_names if name.strip()}
    fields["scientificName"] = find_name(text, genera) if genera else ""
    for field, pattern in KEYWORDS.items():
        value = find_keyword(text, pattern)
        if field == "recordNumber":
            m = re.match(r"\d{1,6}[A-Za-z]?\b", value)
            value = m[0] if m else ""
        fields[field] = value
    return fields


def read_label(rgb, layout=None, px_per_cm=None, taxon_names=(), reader=None):
    """({"voucher_text", "voucher"}, raw) for a sheet image, like the vision model's answer."""
    reader = reader or default_reader()
    parts, seen, regions = [], set(), []
    for name, (x0, y0, x1, y1) in regions_for(rgb.shape, layout, px_per_cm):
        lines = reader.lines(rgb[y0:y1, x0:x1])
        text = assemble([l for l in lines if l["text"].lower() not in seen])
        seen.update(l["text"].lower() for l in lines)
        regions.append({"region": name, "box": [x0, y0, x1, y1], "lines": len(lines)})
        if text:
            parts.append(text)
    text = " | ".join(parts)
    return ({"voucher_text": text, "voucher": parse_fields(text, taxon_names)},
            {"engine": ENGINE_NAME, "regions": regions, "text": text})
