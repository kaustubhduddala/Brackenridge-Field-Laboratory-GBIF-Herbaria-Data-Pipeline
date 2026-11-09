"""The picture of what was measured: outlines, the measured path and the value for every object.

One look at the overlay shows whether a number can be trusted, which is how the measurements earn
trust. Accepted objects are drawn in their trait's colour, objects needing review in the same colour
with a "?", lower bounds with ">=", and rejected objects in grey.
"""
from .decision import ACCEPT, LOWER_BOUND, NEEDS_REVIEW
from .taxa import TRAIT_LABELS, TRAITS

COLOURS = {"panicle_length": (255, 61, 0), "leaf_width": (0, 200, 110), "seed_length": (255, 214, 0),
           "ruler": (0, 229, 255), "sheet": (120, 144, 156), "excluded": (213, 0, 249), "rejected": (158, 158, 158)}


def entry_label(entry, n=None):
    """'panicle 1: 23.40 cm ?' for an object."""
    name = TRAIT_LABELS[entry["trait"]] + (f" {n}" if n else "")
    if entry.get("value_cm") is None:
        return f"{name}: NA"
    digits = 3 if entry["trait"] == "seed_length" else 2
    status = entry.get("status")
    prefix = ">= " if status == LOWER_BOUND else ""
    suffix = " ?" if status == NEEDS_REVIEW else ("" if status in (ACCEPT, LOWER_BOUND) else " x")
    return f"{name}: {prefix}{entry['value_cm']:.{digits}f} cm{suffix}"


def entry_colour(entry):
    return COLOURS[entry["trait"]] if entry.get("status") in (ACCEPT, NEEDS_REVIEW, LOWER_BOUND) else COLOURS["rejected"]


def draw_overlay(image_path, result, out_path, max_side=2200):
    """A downscaled JPEG of the sheet with everything that was found and measured drawn on it."""
    from PIL import Image, ImageDraw, ImageFont

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(image_path) as im:
        canvas = im.convert("RGB")
        canvas.thumbnail((max_side, max_side))
    s = canvas.width / result["size"][0]
    tint = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    tint_draw = ImageDraw.Draw(tint)
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=max(12, canvas.width // 75))
    except TypeError:
        font = ImageFont.load_default()

    def pt(p):
        return (p[0] * s, p[1] * s)

    for trait in TRAITS:                                    # outlines first, as a light tint
        for entry in result["traits"][trait]:
            polygon = entry.get("polygon")
            if polygon and len(polygon) >= 3:
                tint_draw.polygon([pt(p) for p in polygon], fill=(*entry_colour(entry), 70),
                                  outline=(*entry_colour(entry), 220))
    canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), tint).convert("RGB"))
    draw = ImageDraw.Draw(canvas)

    cal = result.get("calibration") or {}
    if cal.get("sheet_box"):
        draw.rectangle([*pt(cal["sheet_box"][:2]), *pt(cal["sheet_box"][2:])], outline=COLOURS["sheet"], width=2)
    for box in (result.get("layout") or {}).get("non_plant_boxes", []):
        draw.rectangle([*pt(box[:2]), *pt(box[2:])], outline=COLOURS["excluded"], width=2)
    if cal.get("ruler_line"):
        a, b = map(pt, cal["ruler_line"])
        draw.line([a, b], fill=COLOURS["ruler"], width=4)
    for trait in TRAITS:
        for n, entry in enumerate(result["traits"][trait], start=1):
            colour = entry_colour(entry)
            points = [pt(p) for p in entry.get("points") or []]
            if len(points) >= 2:
                draw.line(points, fill=colour, width=3)
            for p in (entry.get("anchors") or {}).values():
                x, y = pt(p)
                draw.ellipse((x - 5, y - 5, x + 5, y + 5), outline=colour, width=2)
            for p in entry.get("via") or []:
                x, y = pt(p)
                draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=colour, outline=(0, 0, 0))
            if points:
                draw.text((points[0][0] + 8, points[0][1] - font.size), entry_label(entry, n), fill=colour,
                          font=font, stroke_width=2, stroke_fill=(0, 0, 0))

    scale = (f"{cal.get('px_per_cm') or 0:.1f} px/cm ({cal.get('source')})" if cal.get("px_per_cm") else "no scale")
    summary = []
    for trait in TRAITS:
        decision = (result.get("decisions") or {}).get(trait)
        if decision:
            value = (f"{decision['value_cm']:.2f}" if decision["value_cm"] is not None else
                     f">={decision['lower_bound_cm']:.2f}" if decision["lower_bound_cm"] is not None else "NA")
            summary.append(f"{TRAIT_LABELS[trait]} {value} [{decision['status']}]")
    lines = [f"{result['image']}   {scale}", "   ".join(summary)]
    draw.rectangle((0, 0, canvas.width, 2 * (font.size + 6) + 4), fill=(0, 0, 0))
    for i, line in enumerate(lines):
        draw.text((6, 4 + i * (font.size + 6)), line, fill=(255, 255, 255), font=font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "JPEG", quality=88)
    return out_path
