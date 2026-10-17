import base64
import io
import json
import math
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from openai import APIConnectionError, BadRequestError, NotFoundError, OpenAI

from config import (DEFAULT_TOKEN_LIMIT, LMSTUDIO_MODEL, LMSTUDIO_URL, MAX_IMAGE_SIDE, MIN_CONTEXT_LENGTH,
                    REQUEST_TIMEOUT_S, RESPONSES_DIR_NAME)
from .utils import ItemSkipped, check, gbif_ids, index_images, is_blank, read_csv, save_csv

TRAITS = ("panicle_length", "leaf_width", "seed_length")
FIELDS = [f"{trait}_{n}_{kind}" for trait in TRAITS for n in (1, 2) for kind in ("cm", "confidence")]
POINT_FIELDS = [f"{trait}_{n}_points" for trait in TRAITS for n in (1, 2)]
VOUCHER_FIELDS = ["catalogNumber", "recordedBy", "recordNumber", "eventDate", "country", "stateProvince",
                  "county", "locality", "habitat", "verbatimElevation", "identifiedBy", "scientificName"]

TRAIT_BOUNDS_CM = {
    "panicle_length": (3.0, 75.0),
    "leaf_width": (0.15, 5.0),
    "seed_length": (0.15, 0.65),
}

RESULT_COLUMNS = (
    ["ai_image"]
    + [f"ai_{field}" for field in FIELDS]
    + [f"ai_{trait}_mean_cm" for trait in TRAITS]
    + ["ai_calibration", "ai_notes", "voucher_text"]
    + [f"voucher_{field}" for field in VOUCHER_FIELDS]
    + ["ai_model", "ai_measured_at", "ai_error"]
)
MEASUREMENT_FILE_COLUMNS = ["gbifID", *RESULT_COLUMNS]

POINT_LIST_SCHEMA = {
    "type": "array",
    "items": {
        "type": "array",
        "items": {"type": "number"},
    },
}

# Pass 1A: Geometry ONLY (keeps schema small so LM Studio does not collapse into dummy numbers)
GEOMETRY_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {"type": "string"},
        "calibration": {"type": "string"},
        "ruler_points": POINT_LIST_SCHEMA,
        "ruler_length_cm": {"type": ["number", "null"]},
        "panicle_length_1_points": POINT_LIST_SCHEMA,
        "panicle_length_1_confidence": {"type": ["number", "null"]},
        "panicle_length_2_points": POINT_LIST_SCHEMA,
        "panicle_length_2_confidence": {"type": ["number", "null"]},
        "leaf_width_1_points": POINT_LIST_SCHEMA,
        "leaf_width_1_confidence": {"type": ["number", "null"]},
        "leaf_width_2_points": POINT_LIST_SCHEMA,
        "leaf_width_2_confidence": {"type": ["number", "null"]},
        "spikelet_cluster_center": {
            "type": "array",
            "items": {"type": "number"},
        },
    },
    "required": [
        "notes",
        "calibration",
        "ruler_points",
        "ruler_length_cm",
        "panicle_length_1_points",
        "panicle_length_1_confidence",
        "panicle_length_2_points",
        "panicle_length_2_confidence",
        "leaf_width_1_points",
        "leaf_width_1_confidence",
        "leaf_width_2_points",
        "leaf_width_2_confidence",
        "spikelet_cluster_center",
    ],
    "additionalProperties": False,
}

GEOMETRY_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "specimen_geometry", "strict": True, "schema": GEOMETRY_SCHEMA},
}

# Pass 1B: Voucher Label OCR ONLY
VOUCHER_SCHEMA = {
    "type": "object",
    "properties": {
        "voucher_text": {"type": "string"},
        "voucher": {
            "type": "object",
            "properties": {field: {"type": "string"} for field in VOUCHER_FIELDS},
            "required": VOUCHER_FIELDS,
            "additionalProperties": False,
        },
    },
    "required": ["voucher_text", "voucher"],
    "additionalProperties": False,
}

VOUCHER_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "specimen_voucher", "strict": True, "schema": VOUCHER_SCHEMA},
}

# Pass 2: High-res zoomed seed crop
SEED_CROP_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {"type": "string"},
        "seed_length_1_points": POINT_LIST_SCHEMA,
        "seed_length_1_confidence": {"type": ["number", "null"]},
        "seed_length_2_points": POINT_LIST_SCHEMA,
        "seed_length_2_confidence": {"type": ["number", "null"]},
    },
    "required": [
        "notes",
        "seed_length_1_points",
        "seed_length_1_confidence",
        "seed_length_2_points",
        "seed_length_2_confidence",
    ],
    "additionalProperties": False,
}

SEED_CROP_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "seed_crop_measurements", "strict": True, "schema": SEED_CROP_SCHEMA},
}

# Legacy alias preserved for external imports
RESPONSE_SCHEMA = GEOMETRY_SCHEMA
RESPONSE_FORMAT = GEOMETRY_RESPONSE_FORMAT

GEOMETRY_PROMPT = """
Role: You are a botanical measurement assistant analyzing a Megathyrsus maximus (Guinea grass) herbarium sheet.
IMPORTANT: A cyan/yellow coordinate grid from 0 to 1000 is drawn directly on the image.
- X runs horizontally from 0 (left) to 1000 (right).
- Y runs vertically from 0 (top) to 1000 (bottom).
- Read the exact [x, y] coordinates using the visible grid lines and labels! NEVER invent round placeholder sequences like [100,300], [200,350], [300,400].

1. Anatomical Identification Rules (Read Carefully!)
- RULER (`ruler_points`): Look for the printed centimeter scale bar (often black-and-white alternating 1 cm blocks labeled 0..10 cm in the border or corner). Place 2 points `[[x_start, y_start], [x_end, y_end]]` at the exact `0 cm` tick and `10 cm` tick (or `5 cm` tick) and set `ruler_length_cm` to `10.0` (or `5.0`). If no printed ruler is present, set `ruler_points` to `[]` and `ruler_length_cm` to `null` (the software will automatically measure the white paper width).
- PANICLE (`panicle_length_1_points`, `panicle_length_2_points`):
  * What a panicle looks like: The wispy, feathery, multi-branched seed-head cluster bearing tiny oval spikelets (seeds).
  * Do NOT confuse thick, smooth, folded grass stems (culms) or flat ribbon-like leaves with the panicle!
  * Trace the central main axis (rachis) of the panicle from its lowest branch whorl (base of the inflorescence) to the very tip of the panicle using 3 to 6 waypoints `[[x0, y0], [x1, y1], ...]`.
  * If there is only 1 panicle on the sheet, set `panicle_length_2_points` to `[]` and `panicle_length_2_confidence` to `null`.
- LEAF BLADE WIDTH (`leaf_width_1_points`, `leaf_width_2_points`):
  * What a leaf blade looks like: A flat, dark-green or brownish strap/ribbon-like leaf lamina.
  * Do NOT measure across panicle branches or stems!
  * Find the broadest flat part of a leaf blade and place 2 points `[[x1, y1], [x2, y2]]` directly across its width (perpendicular to the length of the leaf, from one margin to the other margin). Note: a leaf width is narrow (typically only 10 to 35 grid units wide!).
- SPIKELET CLUSTER (`spikelet_cluster_center`):
  * Provide a single `[x, y]` coordinate right in the middle of the densest group of individual oval seeds (spikelets) on the panicle so the camera can zoom in for Pass 2.

Respond ONLY with valid JSON.
"""

SEED_CROP_PROMPT = """
Role: You are inspecting a zoomed-in close-up of Megathyrsus maximus panicle branches to measure 2 individual mature spikelets (seeds).
A coordinate grid from 0 to 1000 is drawn on this cropped image ([0,0] = top-left, [1000,1000] = bottom-right).

Instructions:
1. A spikelet (seed) is a small, oval/elliptical grain (~2.5 to 3.5 mm long) attached to a thin panicle branch.
2. Locate 2 distinct, clearly visible individual spikelets.
3. For each spikelet, read the grid and place 2 exact endpoints `[[x_base, y_base], [x_tip, y_tip]]` from the base of the single grain to its apex tip.
4. Do NOT measure an entire branch—only a single oval grain!
5. Respond ONLY with valid JSON.
"""

VOUCHER_PROMPT = """
Role: Transcribe the herbarium specimen voucher label (usually in the bottom corner) into Darwin Core fields.
- voucher_text: Transcribe all readable printed and handwritten label text into a single line separated by " | ".
- voucher: Populate catalogNumber, recordedBy, recordNumber, eventDate (strictly YYYY-MM-DD, YYYY-MM, or YYYY), country, stateProvince, county, locality, habitat, verbatimElevation, identifiedBy, scientificName. Use "" for any field not explicitly written on the label.
Respond ONLY with valid JSON.
"""

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _detect_white_sheet_span_px(image):
    """
    Deterministically finds the left and right pixel edges of the bright herbarium mounting
    sheet (ignoring black scanner borders) using horizontal brightness profiling.
    Returns (x_left_px, x_right_px, y_mid_px) or None.
    """
    gray = image.convert("L")
    w, h = gray.size
    # Sample the middle 50% of image height where the paper mount always spans full width
    y0, y1 = int(h * 0.25), int(h * 0.75)
    strip = gray.crop((0, y0, w, y1)).resize((w, 1))
    col_means = list(strip.getdata())

    # Paper sheet is bright (> 150 out of 255); black scanner background is dark (< 80)
    threshold = 145
    bright_cols = [x for x, val in enumerate(col_means) if val >= threshold]
    if len(bright_cols) < w * 0.5:
        return None
    left_px, right_px = bright_cols[0], bright_cols[-1]
    if (right_px - left_px) < w * 0.6:
        return None
    return left_px, right_px, int(h * 0.50)


def _draw_vlm_grid(image):
    """
    Overlays a semi-transparent 0..1000 coordinate grid with numeric labels every 100 units
    so local VLMs can visually read exact coordinates instead of guessing blind numbers.
    """
    from PIL import Image, ImageDraw

    base = image.convert("RGBA")
    w, h = base.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for g in range(50, 1000, 50):
        x = int(round(g * w / 1000.0))
        y = int(round(g * h / 1000.0))
        is_major = (g % 100 == 0)
        line_color = (0, 210, 255, 85) if is_major else (0, 210, 255, 38)
        width = 2 if is_major else 1
        draw.line([(x, 0), (x, h)], fill=line_color, width=width)
        draw.line([(0, y), (w, y)], fill=line_color, width=width)

        if is_major:
            # Top & bottom X labels
            draw.rectangle((x - 14, 2, x + 16, 15), fill=(0, 0, 0, 170))
            draw.text((x - 11, 3), str(g), fill=(255, 255, 0, 255))
            draw.rectangle((x - 14, h - 16, x + 16, h - 3), fill=(0, 0, 0, 170))
            draw.text((x - 11, h - 15), str(g), fill=(255, 255, 0, 255))

            # Left & right Y labels
            draw.rectangle((2, y - 7, 30, y + 7), fill=(0, 0, 0, 170))
            draw.text((4, y - 6), str(g), fill=(0, 255, 200, 255))
            draw.rectangle((w - 31, y - 7, w - 2, y + 7), fill=(0, 0, 0, 170))
            draw.text((w - 29, y - 6), str(g), fill=(0, 255, 200, 255))

    # Interior [x,y] intersections every 200 units for immediate local reference
    for gx in (200, 400, 600, 800):
        for gy in (200, 400, 600, 800):
            x = int(round(gx * w / 1000.0))
            y = int(round(gy * h / 1000.0))
            lbl = f"{gx},{gy}"
            draw.rectangle((x + 2, y + 2, x + 46, y + 14), fill=(0, 0, 0, 130))
            draw.text((x + 4, y + 3), lbl, fill=(255, 255, 255, 220))

    return Image.alpha_composite(base, overlay).convert("RGB")


def _encode_pil_jpeg(pil_img, quality=92):
    buffer = io.BytesIO()
    pil_img.convert("RGB").save(buffer, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def _prepare_images(image_path):
    """
    Returns (gridded_data_url, clean_data_url, (orig_w, orig_h), sheet_span_norm).
    """
    path = Path(image_path)
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as image:
        orig_w, orig_h = image.size
        sheet_px = _detect_white_sheet_span_px(image)
        sheet_span_norm = None
        if sheet_px:
            lx, rx, my = sheet_px
            sheet_span_norm = [
                [round(lx * 1000.0 / orig_w, 2), round(my * 1000.0 / orig_h, 2)],
                [round(rx * 1000.0 / orig_w, 2), round(my * 1000.0 / orig_h, 2)],
            ]

        work = image.copy()
        work.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        clean_url = _encode_pil_jpeg(work, quality=92)
        gridded_url = _encode_pil_jpeg(_draw_vlm_grid(work), quality=92)

    return gridded_url, clean_url, (orig_w, orig_h), sheet_span_norm


def _image_data_url(image_path):
    _, clean_url, _, _ = _prepare_images(image_path)
    return clean_url


def _is_hallucinated_pattern(points):
    """
    Catches dummy arithmetic sequences like [[100, 300], [200, 350], [300, 400], [400, 450]]
    or placeholder multiples of 50/100 emitted when a local VLM fails to ground visually.
    """
    if not points or len(points) < 2:
        return True
    # Check if all coordinates are exact multiples of 50
    if all((p[0] % 50 == 0 and p[1] % 50 == 0) for p in points):
        return True
    # Check if 3+ points have identical constant step sizes in both X and Y
    if len(points) >= 3:
        dxs = [round(points[i + 1][0] - points[i][0], 1) for i in range(len(points) - 1)]
        dys = [round(points[i + 1][1] - points[i][1], 1) for i in range(len(points) - 1)]
        if len(set(dxs)) == 1 and len(set(dys)) == 1 and (abs(dxs[0]) >= 50 or abs(dys[0]) >= 50):
            return True
    return False


def _clean_points(points):
    if not isinstance(points, list):
        return []
    cleaned = []
    for pt in points:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            x, y = _to_number(pt[0]), _to_number(pt[1])
            if x is not None and y is not None and 0.0 <= x <= 1000.0 and 0.0 <= y <= 1000.0:
                if not cleaned or (abs(x - cleaned[-1][0]) > 1e-3 or abs(y - cleaned[-1][1]) > 1e-3):
                    cleaned.append([round(x, 2), round(y, 2)])
    if _is_hallucinated_pattern(cleaned):
        return []
    return cleaned


def _polyline_px(points, width, height):
    cleaned = _clean_points(points)
    if len(cleaned) < 2 or not width or not height:
        return None
    px_pts = [(p[0] * width / 1000.0, p[1] * height / 1000.0) for p in cleaned]
    total = sum(
        math.hypot(px_pts[i + 1][0] - px_pts[i][0], px_pts[i + 1][1] - px_pts[i][1])
        for i in range(len(px_pts) - 1)
    )
    return total if total > 1.0 else None


def _within_bounds(trait, value_cm):
    if value_cm is None:
        return False
    low, high = TRAIT_BOUNDS_CM.get(trait, (0.01, 200.0))
    return low <= value_cm <= high


def _crop_seed_roi_gridded(image_path, geom_parsed, orig_size):
    """
    Always extracts a zoomed crop around the panicle spikelets, upscales it to 1000x1000
    if needed, and draws a fine coordinate grid so Pass 2 can resolve 3 mm seeds on ANY image size.
    """
    if not orig_size:
        return None, None
    orig_w, orig_h = orig_size
    from PIL import Image

    center = geom_parsed.get("spikelet_cluster_center")
    cx_norm, cy_norm = None, None
    if isinstance(center, (list, tuple)) and len(center) >= 2:
        x, y = _to_number(center[0]), _to_number(center[1])
        if x is not None and y is not None and 10 < x < 990 and 10 < y < 990 and not (x % 50 == 0 and y % 50 == 0):
            cx_norm, cy_norm = x, y

    if cx_norm is None:
        p1 = _clean_points(geom_parsed.get("panicle_length_1_points"))
        if len(p1) >= 2:
            mid = p1[len(p1) // 2]
            cx_norm, cy_norm = mid[0], mid[1]

    if cx_norm is None:
        return None, None

    # Crop 22% of the image width/height around the spikelet cluster (~6 to 8 cm real-world window)
    box_side = int(max(180, min(orig_w, orig_h) * 0.24))
    cx_px = cx_norm * orig_w / 1000.0
    cy_px = cy_norm * orig_h / 1000.0

    x0 = int(max(0, min(orig_w - box_side, round(cx_px - box_side / 2))))
    y0 = int(max(0, min(orig_h - box_side, round(cy_px - box_side / 2))))

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(image_path) as image:
        crop = image.crop((x0, y0, x0 + box_side, y0 + box_side)).convert("RGB")
        # Upscale small crops to 1000x1000 so 3 mm seeds are large and easy for VLM patches to resolve
        upscaled = crop.resize((1000, 1000), Image.Resampling.LANCZOS)
        gridded_crop = _draw_vlm_grid(upscaled)
        data_url = _encode_pil_jpeg(gridded_crop, quality=95)

    return data_url, (x0, y0, box_side, box_side)


def _parse_json(content):
    text = re.sub(r"<think>.*?</think>", "", str(content or ""), flags=re.DOTALL | re.IGNORECASE)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    if not text.strip():
        raise ValueError("Model returned an empty response")

    decoder = json.JSONDecoder()
    found = None
    for match in re.finditer(r"\{", text):
        try:
            payload, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload:
            found = payload
    if found is None:
        raise ValueError("No valid JSON object found in model response")
    return found


def _to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER.search(str(value or ""))
    return float(match.group()) if match else None


def _as_text(value, keep_lines=False):
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if keep_lines:
        lines = [" ".join(line.split()) for line in re.split(r"[\r\n]+", text)]
        return " | ".join(line for line in lines if line)
    return " ".join(text.split())


def _clean_date(value):
    text = value.strip()
    match = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", text)
    if not match:
        return text
    year, month, day = match.groups()
    parts = [year]
    if month and 1 <= int(month) <= 12:
        parts.append(f"{int(month):02d}")
        if day and 1 <= int(day) <= 31:
            parts.append(f"{int(day):02d}")
    return "-".join(parts)


def _resolve_scale(geom_parsed, orig_size, sheet_span_norm):
    """
    Validates the VLM's ruler points against the deterministically detected 29 cm white sheet.
    If the VLM missed the ruler or hallucinated a span across the whole sheet, falls back
    to the exact white herbarium paper width = 29.0 cm.
    """
    orig_w, orig_h = orig_size if orig_size else (None, None)
    sheet_px = _polyline_px(sheet_span_norm, orig_w, orig_h) if sheet_span_norm else None
    sheet_px_per_cm = (sheet_px / 29.0) if sheet_px else None

    ruler_pts = _clean_points(geom_parsed.get("ruler_points"))
    ruler_cm = _to_number(geom_parsed.get("ruler_length_cm"))
    ruler_px = _polyline_px(ruler_pts, orig_w, orig_h) if (orig_w and orig_h and len(ruler_pts) == 2) else None

    if ruler_px and ruler_cm and 1.0 <= ruler_cm <= 20.0:
        ruler_px_per_cm = ruler_px / ruler_cm
        # Sanity check: a true 10 cm ruler on a 29 cm sheet must be roughly 1/2.9th of sheet width
        if not sheet_px_per_cm or (0.65 * sheet_px_per_cm <= ruler_px_per_cm <= 1.45 * sheet_px_per_cm):
            return ruler_pts, round(ruler_cm, 2), ruler_px_per_cm, f"Measured {ruler_cm:g} cm scale bar."

    if sheet_span_norm and sheet_px_per_cm:
        return (
            sheet_span_norm,
            29.0,
            sheet_px_per_cm,
            "Auto-calibrated from detected white herbarium sheet width (29.0 cm).",
        )

    return [], None, None, _as_text(geom_parsed.get("calibration"))


def _normalise_result(result, orig_size=None, sheet_span_norm=None):
    output = {}
    orig_w, orig_h = orig_size if orig_size else (None, None)

    cal_pts, cal_len_cm, px_per_cm, cal_desc = _resolve_scale(result, orig_size, sheet_span_norm)
    output["calibration_points"] = cal_pts
    output["calibration_length_cm"] = cal_len_cm
    output["px_per_cm"] = round(px_per_cm, 2) if px_per_cm else None

    for trait in TRAITS:
        for n in (1, 2):
            pts_key = f"{trait}_{n}_points"
            cm_key = f"{trait}_{n}_cm"
            conf_key = f"{trait}_{n}_confidence"

            pts = _clean_points(result.get(pts_key))
            output[pts_key] = pts

            coord_cm = None
            if px_per_cm and orig_w and orig_h and len(pts) >= 2:
                trait_px = _polyline_px(pts, orig_w, orig_h)
                if trait_px:
                    candidate_cm = trait_px / px_per_cm
                    if _within_bounds(trait, candidate_cm):
                        coord_cm = candidate_cm

            if coord_cm is None:
                output[pts_key] = []
            output[cm_key] = None if coord_cm is None else round(coord_cm, 2)

            conf = _to_number(result.get(conf_key))
            if conf is not None:
                conf = round(min(max(conf, 0.0), 1.0), 2)
            output[conf_key] = None if output[cm_key] is None else (conf if conf is not None else 0.75)

    if px_per_cm and cal_len_cm:
        cal_px = cal_len_cm * px_per_cm
        output["calibration"] = f"{cal_desc} [{cal_len_cm:.1f} cm = {cal_px:.0f} px ({px_per_cm:.1f} px/cm)]".strip()
    else:
        output["calibration"] = cal_desc

    output["notes"] = _as_text(result.get("notes"))
    output["voucher_text"] = _as_text(result.get("voucher_text"), keep_lines=True)

    voucher = result.get("voucher") if isinstance(result.get("voucher"), dict) else {}
    output["voucher"] = {field: _as_text(voucher.get(field)) for field in VOUCHER_FIELDS}
    output["voucher"]["eventDate"] = _clean_date(output["voucher"]["eventDate"])
    if "_seed_crop_box_px" in result:
        output["_seed_crop_box_px"] = result["_seed_crop_box_px"]
    return output


def save_debug_overlay(image_path, result, out_path, orig_size=None, max_side=1400):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    try:
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(image_path) as im:
            orig_w, orig_h = im.size if not orig_size else orig_size
            canvas = im.copy()
            canvas.thumbnail((max_side, max_side))
            canvas = canvas.convert("RGB")

        w, h = canvas.size
        draw = ImageDraw.Draw(canvas)

        colors = {
            "calibration": "#00E5FF",     # Cyan
            "panicle_length": "#FF3D00",  # Red-Orange
            "leaf_width": "#00E676",      # Bright Green
            "seed_length": "#FFEA00",     # Yellow
            "crop_box": "#D500F9",        # Magenta
        }

        crop_box = result.get("_seed_crop_box_px")
        if isinstance(crop_box, (list, tuple)) and len(crop_box) == 4 and orig_w and orig_h:
            x0, y0, cw, ch = crop_box
            rx0, ry0 = x0 * w / orig_w, y0 * h / orig_h
            rx1, ry1 = (x0 + cw) * w / orig_w, (y0 + ch) * h / orig_h
            draw.rectangle((rx0, ry0, rx1, ry1), outline=colors["crop_box"], width=2)
            draw.text((rx0 + 4, max(2, ry0 - 14)), "Pass 2 Seed Zoom", fill=colors["crop_box"])

        cal_pts = result.get("calibration_points") or []
        if len(cal_pts) >= 2:
            px_pts = [(p[0] * w / 1000.0, p[1] * h / 1000.0) for p in cal_pts]
            draw.line(px_pts, fill=colors["calibration"], width=4)
            for x, y in px_pts:
                draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=colors["calibration"])
            cal_cm = result.get("calibration_length_cm")
            draw.text((px_pts[0][0] + 6, max(4, px_pts[0][1] - 16)), f"Cal: {cal_cm} cm", fill=colors["calibration"])

        short_names = {"panicle_length": "Panicle", "leaf_width": "Leaf", "seed_length": "Seed"}
        for trait in TRAITS:
            color = colors[trait]
            for n in (1, 2):
                pts = result.get(f"{trait}_{n}_points") or []
                val_cm = result.get(f"{trait}_{n}_cm")
                if len(pts) >= 2 and val_cm is not None:
                    px_pts = [(p[0] * w / 1000.0, p[1] * h / 1000.0) for p in pts]
                    draw.line(px_pts, fill=color, width=3)
                    for x, y in px_pts:
                        r = 3 if trait == "seed_length" else 4
                        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
                    lbl = f"{short_names[trait]} {n}: {val_cm:.2f} cm"
                    draw.text((px_pts[0][0] + 5, px_pts[0][1] - 12), lbl, fill=color)

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "JPEG", quality=90)
        return out_path
    except Exception:
        return None


def _format_number(value):
    return "" if value is None else f"{value:.2f}"


def _result_columns(result, model, image):
    columns = {"ai_image": image.name, **{f"ai_{field}": _format_number(result[field]) for field in FIELDS}}
    for trait in TRAITS:
        values = [result[f"{trait}_{n}_cm"] for n in (1, 2) if result[f"{trait}_{n}_cm"] is not None]
        columns[f"ai_{trait}_mean_cm"] = _format_number(sum(values) / len(values) if values else None)
    columns.update({
        "ai_calibration": result["calibration"],
        "ai_notes": result["notes"],
        "voucher_text": result["voucher_text"],
        **{f"voucher_{field}": value for field, value in result["voucher"].items()},
        "ai_model": model,
        "ai_measured_at": _now(),
        "ai_error": "",
    })
    return columns


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _extra(obj, name):
    value = getattr(obj, name, None)
    if value is None:
        value = (getattr(obj, "model_extra", None) or {}).get(name)
    return value


class ModelOutputError(ValueError):
    def __init__(self, message, raw):
        super().__init__(message)
        self.raw = raw


def save_response(gbif_id, image, raw, error, responses_dir):
    responses_dir = Path(responses_dir)
    responses_dir.mkdir(parents=True, exist_ok=True)
    record = {"gbifID": gbif_id, "image": image.name, "saved_at": _now(), "error": error, **raw}
    path = responses_dir / f"{gbif_id}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    if not error and isinstance(raw.get("normalised"), dict):
        save_debug_overlay(
            image,
            raw["normalised"],
            responses_dir / f"{gbif_id}_overlay.jpg",
            orig_size=raw.get("image_size_px"),
        )
    return path


def _api_root(base_url):
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def find_loaded_model(base_url=LMSTUDIO_URL):
    if LMSTUDIO_MODEL:
        return {"id": LMSTUDIO_MODEL, "type": "", "context": None}
    try:
        resp = requests.get(f"{_api_root(base_url)}/api/v0/models", timeout=10)
        resp.raise_for_status()
        models = resp.json().get("data", [])
    except (requests.RequestException, ValueError):
        models = None

    if models is not None:
        loaded = [m for m in models if m.get("state") == "loaded" and m.get("type") in ("vlm", "llm")]
        if not loaded:
            raise RuntimeError("LM Studio is running but no model is loaded. Load a vision model in LM Studio first.")
        best = next((m for m in loaded if m.get("type") == "vlm"), loaded[0])
        return {"id": best["id"], "type": best.get("type", ""), "context": best.get("loaded_context_length")}

    try:
        resp = requests.get(f"{base_url.rstrip('/')}/models", timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Could not reach LM Studio at {base_url}: {exc}") from exc
    if not data:
        raise RuntimeError("The server reports no models. Load a vision model in LM Studio first.")
    return {"id": data[0]["id"], "type": "", "context": None}


def model_warnings(model):
    warnings = []
    if model["type"] == "llm":
        warnings.append(f"{model['id']} is a text-only model; load a vision model to measure images.")
    if model["context"] and model["context"] < MIN_CONTEXT_LENGTH:
        warnings.append(f"{model['id']} is loaded with a {model['context']}-token context. Reload it in LM Studio "
                        f"with at least {MIN_CONTEXT_LENGTH} so the image, prompt and answer fit.")
    return warnings


class VisionModel:
    def __init__(self, base_url=LMSTUDIO_URL, log=print, token_limit=DEFAULT_TOKEN_LIMIT, reasoning_effort=None):
        self.client = OpenAI(base_url=base_url, api_key="lm-studio", timeout=REQUEST_TIMEOUT_S)
        self.base_url = base_url
        self.log = log
        self.token_limit = token_limit
        self.reasoning_effort = reasoning_effort
        self.structured = True
        self.model = find_loaded_model(base_url)
        for warning in model_warnings(self.model):
            log(f"Warning: {warning}")

    def _build_request(self, data_url, system_prompt, user_text, response_format):
        request = {
            "model": self.model["id"],
            "temperature": 0.1,
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]},
            ],
        }
        if self.token_limit:
            request["max_tokens"] = int(self.token_limit)
        if self.reasoning_effort:
            request["reasoning_effort"] = self.reasoning_effort
        if self.structured and response_format is not None:
            request["response_format"] = response_format
        return request

    def _run_with_fallback(self, request, control, status, stage_label="Thinking"):
        while True:
            try:
                return self._stream(request, control, status, stage_label=stage_label)
            except BadRequestError as exc:
                if "reasoning_effort" in request:
                    request.pop("reasoning_effort")
                    self.reasoning_effort = None
                    self.log(f"    The server rejected the thinking setting ({exc}); continuing without it")
                elif "response_format" in request:
                    request.pop("response_format")
                    self.structured = False
                    self.log(f"    The server rejected structured output ({exc}); continuing with plain JSON prompting")
                else:
                    raise

    def measure(self, image_path, control=None, status=None):
        gridded_url, clean_url, orig_size, sheet_span_norm = _prepare_images(image_path)

        # Pass 1A: Botanical Geometry on Gridded Image
        geom_req = self._build_request(
            gridded_url,
            GEOMETRY_PROMPT,
            "Read the 0..1000 coordinate grid on this image and locate the ruler, panicles, leaf blade widths, and spikelet cluster.",
            GEOMETRY_RESPONSE_FORMAT,
        )
        geom_parsed, model, raw = self._run_with_fallback(
            geom_req, control, status, stage_label="Pass 1/3 (gridded geometry)"
        )
        raw["image_size_px"] = list(orig_size) if orig_size else None
        raw["detected_sheet_span_norm"] = sheet_span_norm

        # Pass 1B: Voucher OCR on Clean Un-gridded Image
        try:
            voucher_req = self._build_request(
                clean_url,
                VOUCHER_PROMPT,
                "Transcribe the specimen voucher label into Darwin Core fields.",
                VOUCHER_RESPONSE_FORMAT,
            )
            voucher_parsed, _, _ = self._run_with_fallback(
                voucher_req, control, status, stage_label="Pass 2/3 (voucher OCR)"
            )
            geom_parsed["voucher_text"] = voucher_parsed.get("voucher_text", "")
            geom_parsed["voucher"] = voucher_parsed.get("voucher", {})
        except ItemSkipped:
            raise
        except Exception as exc:
            raw["voucher_ocr_error"] = str(exc)

        # Pass 2: High-res Gridded Seed Zoom Crop (runs on ALL images regardless of size)
        _, _, px_per_cm, _ = _resolve_scale(geom_parsed, orig_size, sheet_span_norm)
        if px_per_cm and orig_size:
            crop_url, crop_box = _crop_seed_roi_gridded(image_path, geom_parsed, orig_size)
            if crop_url and crop_box:
                x0, y0, cw, ch = crop_box
                orig_w, orig_h = orig_size
                try:
                    seed_req = self._build_request(
                        crop_url,
                        SEED_CROP_PROMPT,
                        "Read the 0..1000 grid on this zoomed panicle crop and place endpoints on 2 individual oval spikelets (seeds).",
                        SEED_CROP_RESPONSE_FORMAT,
                    )
                    seed_parsed, _, seed_raw = self._run_with_fallback(
                        seed_req, control, status, stage_label="Pass 3/3 (zoomed seed crop)"
                    )
                    raw["seed_crop_pass"] = {
                        "crop_box_px": list(crop_box),
                        "parsed": seed_parsed,
                    }
                    geom_parsed["_seed_crop_box_px"] = list(crop_box)

                    for n in (1, 2):
                        c_pts = _clean_points(seed_parsed.get(f"seed_length_{n}_points"))
                        if len(c_pts) == 2:
                            global_pts = [
                                [
                                    round(((x0 + (p[0] / 1000.0) * cw) / orig_w) * 1000.0, 2),
                                    round(((y0 + (p[1] / 1000.0) * ch) / orig_h) * 1000.0, 2),
                                ]
                                for p in c_pts
                            ]
                            geom_parsed[f"seed_length_{n}_points"] = global_pts
                            geom_parsed[f"seed_length_{n}_confidence"] = seed_parsed.get(f"seed_length_{n}_confidence")
                except ItemSkipped:
                    raise
                except Exception as exc:
                    raw["seed_crop_error"] = str(exc)

        normalised = _normalise_result(geom_parsed, orig_size=orig_size, sheet_span_norm=sheet_span_norm)
        raw["normalised"] = normalised
        return normalised, model, raw

    def _stream(self, request, control, status, stage_label="Thinking"):
        content, reasoning = [], []
        finish_reason, model, tokens, reported = None, self.model["id"], 0, 0
        stream = self.client.chat.completions.create(**request)
        try:
            for chunk in stream:
                check(control)
                model = getattr(chunk, "model", None) or model
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta is not None:
                    if delta.content:
                        content.append(delta.content)
                        tokens += 1
                    thought = _extra(delta, "reasoning_content") or _extra(delta, "reasoning")
                    if thought:
                        reasoning.append(thought)
                        tokens += 1
                finish_reason = choice.finish_reason or finish_reason
                if status and tokens - reported >= 20:
                    reported = tokens
                    phase = "Writing answer" if content else stage_label
                    status(f"{phase}, about {tokens} tokens")
        finally:
            stream.close()

        text = "".join(content)
        raw = {"model": model, "finish_reason": finish_reason, "approx_tokens": tokens,
               "structured_output": "response_format" in request, "content": text, "reasoning": "".join(reasoning)}
        try:
            raw["parsed"] = _parse_json(text if text.strip() else raw["reasoning"])
        except ValueError as exc:
            message = str(exc)
            if finish_reason == "length":
                message = (f"The model stopped after about {tokens} tokens without finishing its answer. Raise the "
                           "token limit, raise the context length for this model in LM Studio, or set Thinking to "
                           "Low or Off.")
            raise ModelOutputError(message, raw) from exc
        return raw["parsed"], model, raw


def load_measurements(csv_path):
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if "gbifID" not in df.columns:
        raise ValueError(f"{csv_path} has no gbifID column")
    return {str(r["gbifID"]).strip(): r for r in df.to_dict("records")}


def _save_measurements(rows, csv_path):
    df = pd.DataFrame(list(rows.values()))
    extra = [c for c in df.columns if c not in MEASUREMENT_FILE_COLUMNS]
    save_csv(df.reindex(columns=MEASUREMENT_FILE_COLUMNS + extra).fillna(""), csv_path)


def _is_done(row):
    return bool(row) and not is_blank(row.get("ai_measured_at")) and is_blank(row.get("ai_error"))


def _describe(row):
    parts = []
    for trait, label in zip(TRAITS, ("panicle", "leaf", "seed")):
        value = row.get(f"ai_{trait}_mean_cm")
        parts.append(f"{label} {value} cm" if not is_blank(value) else f"{label} -")
    return ", ".join(parts)


def measurement_status(media_dir, csv_path):
    rows = load_measurements(csv_path)
    responses_dir = Path(csv_path).parent / RESPONSES_DIR_NAME
    items = []
    for gbif_id, image in index_images(media_dir).items():
        row = rows.get(gbif_id)
        if _is_done(row):
            status, detail = "Measured", _describe(row)
        elif row and not is_blank(row.get("ai_error")):
            status, detail = "Failed", str(row["ai_error"])
        else:
            status, detail = "Not measured", ""
        notes = "" if not row else "\n".join(
            str(row.get(k)) for k in ("ai_calibration", "ai_notes", "voucher_text") if not is_blank(row.get(k)))
        response = responses_dir / f"{gbif_id}.json"
        items.append({"id": gbif_id, "status": status, "detail": detail, "image": image, "notes": notes,
                      "response": response if response.exists() else None})
    return items


def check_connection(base_url=LMSTUDIO_URL):
    model = find_loaded_model(base_url)
    context = f", {model['context']}-token context" if model["context"] else ""
    lines = [f"Connected. Measurements will use {model['id']}{context}."]
    lines += [f"Warning: {w}" for w in model_warnings(model)]
    return "\n".join(lines)


def measure_images(media_dir, csv_path, log=print, control=None, progress=None,
                   status=None, base_url=LMSTUDIO_URL, token_limit=DEFAULT_TOKEN_LIMIT, reasoning_effort=None,
                   only_ids=None, force=False):
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    responses_dir = csv_path.parent / RESPONSES_DIR_NAME
    images = index_images(media_dir)
    rows = load_measurements(csv_path)
    targets = [(gbif_id, path) for gbif_id, path in images.items() if only_ids is None or gbif_id in only_ids]
    summary = {"total": len(targets), "measured": 0, "skipped": 0, "skipped_by_user": 0, "failed": 0,
               "cancelled": False}
    if not images:
        log(f"No downloaded images found in {media_dir}. Download images first.")
        return summary

    vision = VisionModel(base_url, log, token_limit, reasoning_effort)
    scope = "selected " if only_ids is not None else ""
    log(f"Measuring {len(targets)} {scope}images from {media_dir} with {vision.model['id']}")

    for number, (gbif_id, image) in enumerate(targets, start=1):
        if control and control.cancelled:
            summary["cancelled"] = True
            break
        if control:
            control.new_item()
        if progress:
            progress(number - 1, len(targets))
        if _is_done(rows.get(gbif_id)) and not force:
            summary["skipped"] += 1
            continue

        log(f"[{number}/{len(targets)}] Measuring {image.name}")
        started = datetime.now()
        try:
            result, model, raw = vision.measure(image, control, status)
            save_response(gbif_id, image, raw, "", responses_dir)
            rows[gbif_id] = {"gbifID": gbif_id, **_result_columns(result, model, image)}
            summary["measured"] += 1
            log(f"    Done in {(datetime.now() - started).seconds} s: {_describe(rows[gbif_id])}")
        except ItemSkipped:
            if control and control.cancelled:
                summary["cancelled"] = True
                break
            log(f"    Skipped {image.name}")
            summary["skipped_by_user"] += 1
            continue
        except (APIConnectionError, NotFoundError) as exc:
            raise RuntimeError(f"Could not reach LM Studio at {base_url}: {exc}. "
                               "Check that the server is running with a vision model loaded.") from exc
        except Exception as exc:
            if isinstance(exc, ModelOutputError):
                save_response(gbif_id, image, exc.raw, str(exc), responses_dir)
            previous = rows.get(gbif_id, {})
            rows[gbif_id] = {**previous, "gbifID": gbif_id, "ai_image": image.name, "ai_error": str(exc),
                             "ai_measured_at": _now()}
            summary["failed"] += 1
            log(f"    Failed: {exc}")
        _save_measurements(rows, csv_path)

    if summary["cancelled"]:
        log("Measurement cancelled")
    elif progress:
        progress(len(targets), len(targets))
    log(f"Measurement complete: {summary['measured']} measured, {summary['skipped']} already done, "
        f"{summary['skipped_by_user']} skipped by you, {summary['failed']} failed. Saved to {csv_path}")
    return summary


def csv_columns(csv_path):
    return [str(c).strip() for c in pd.read_csv(csv_path, nrows=0, encoding="utf-8-sig").columns]


def _key_values(csv_path, column):
    values = pd.read_csv(csv_path, usecols=[column], dtype=str, encoding="utf-8-sig")[column]
    return values.fillna("").str.strip()


def count_matches(master_csv, master_key, measurements_csv, measurement_key):
    master_ids = _key_values(master_csv, master_key)
    measured = set(_key_values(measurements_csv, measurement_key)) - {""}
    return int(master_ids.isin(measured).sum()), len(master_ids), len(measured)


def join_measurements(master_csv, measurements_csv, log=print, fill_blanks=False,
                      master_key="gbifID", measurement_key="gbifID", master_columns=None,
                      measurement_columns=None, output_csv=None, matched_only=False):
    master_csv, measurements_csv = Path(master_csv), Path(measurements_csv)
    if not measurements_csv.exists():
        raise FileNotFoundError(f"{measurements_csv} does not exist yet. Measure some images first.")
    master = read_csv(master_csv, log)
    master.columns = master.columns.str.strip()
    measurements = read_csv(measurements_csv, log)
    measurements.columns = measurements.columns.str.strip()
    for name, df, key in (("dataset", master, master_key), ("measurements", measurements, measurement_key)):
        if key not in df.columns:
            raise ValueError(f"The {name} CSV has no column named {key}")

    master_ids = master[master_key].fillna("").astype(str).str.strip()
    measurement_ids = measurements[measurement_key].fillna("").astype(str).str.strip()
    measurements = (measurements.assign(_join_key=measurement_ids)[measurement_ids != ""]
                    .drop_duplicates("_join_key", keep="last").set_index("_join_key"))

    if measurement_columns is None:
        added = [c for c in RESULT_COLUMNS if c in measurements.columns]
    else:
        added = [c for c in measurement_columns if c in measurements.columns and c != measurement_key]
    kept = list(master.columns) if master_columns is None else [c for c in master.columns if c in set(master_columns)]
    if master_key not in kept:
        kept.insert(0, master_key)
    replaced = [c for c in added if c in kept and c != master_key]
    added = [c for c in added if c != master_key]
    if replaced:
        log(f"  {len(replaced)} columns exist in both files; the measurement values are used for: "
            + ", ".join(replaced[:6]) + (" and more" if len(replaced) > 6 else ""))

    had_fills = "voucher_filled_columns" in master.columns
    fills = (master["voucher_filled_columns"].fillna("") if had_fills
             else pd.Series("", index=master.index)).astype("object").copy()
    base = master[[c for c in kept if c not in replaced and c != "voucher_filled_columns"]]
    joined = measurements.reindex(master_ids.values)[added]
    joined.index = master.index
    result = pd.concat([base, joined.astype("object")], axis=1)
    matched = master_ids.isin(measurements.index) & (master_ids != "")

    if fill_blanks:
        pairs = [(f, f"voucher_{f}") for f in VOUCHER_FIELDS if f in result.columns and f"voucher_{f}" in result.columns]
        if not pairs:
            log("  Fill empty fields: no voucher_ columns were added, or the dataset has no matching columns")
        for idx in result.index[matched]:
            filled = [f for f in str(fills[idx]).split(", ") if f]
            for field, source in pairs:
                value = result.at[idx, source]
                if not is_blank(value) and is_blank(result.at[idx, field]):
                    result.at[idx, field] = value
                    filled.append(field)
            fills[idx] = ", ".join(dict.fromkeys(filled))
    if fill_blanks or had_fills:
        result["voucher_filled_columns"] = fills

    if matched_only:
        result = result[matched]
    output_csv = Path(output_csv) if output_csv else master_csv
    note = ""
    if output_csv.resolve() == master_csv.resolve():
        backup = master_csv.with_name(master_csv.stem + "_before_join.csv")
        backup.write_bytes(master_csv.read_bytes())
        note = f" (previous version kept as {backup.name})"
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    save_csv(result, output_csv)

    unmatched = sorted(set(measurements.index) - set(master_ids))
    if unmatched:
        preview = ", ".join(unmatched[:5]) + (" and more" if len(unmatched) > 5 else "")
        log(f"  {len(unmatched)} measured {measurement_key} values are not in the dataset and were not joined: {preview}")
    log(f"Join complete: {int(matched.sum())} of {len(master)} records matched on {master_key} = {measurement_key}; "
        f"{len(result.columns)} columns and {len(result)} rows saved to {output_csv}{note}")
    return {"matched": int(matched.sum()), "total": len(master), "unmatched": len(unmatched),
            "rows": len(result), "output": str(output_csv)}