"""Is the end of a structure real, or hidden?

A panicle that runs under a label, an envelope or tape, or off the edge of the sheet, has no visible
end: the length that can be measured is a minimum (a censored value), not the length. This module
decides which of three things happened at an end of a traced path:

    free       paper around the end: a real tip or base
    over       the end is inside a label's box but the plant lies on top of the label, in full view
    hidden     the end stops at an occluder or at the sheet/image edge -> the length is a lower bound

and reports where a path crosses an occluder and comes out the other side, so the bridged stretch
can be marked as interpolated.
"""
import math

OCCLUDER_KINDS = ("label", "envelope", "ruler", "color_chart", "tape", "other")
EDGE_TOLERANCE_CM = 0.35        # an end this close to an occluder or edge counts as touching it
OVER_SHARE = 0.6                # share of plant pixels on the stretch inside a box for "lies on top"
MIN_HIDDEN_CM = 0.4             # shorter stretches inside a box are not worth reporting


def occluders_from_layout(layout):
    """[{"kind", "box"}] from the layout: detected label/envelope objects, other non-plant boxes, the ruler."""
    layout = layout or {}
    found, seen = [], set()

    def add(kind, box):
        key = tuple(round(v) for v in box)
        if key not in seen:
            seen.add(key)
            found.append({"kind": kind, "box": [float(v) for v in box]})

    for obj in layout.get("objects") or ():
        if obj.get("cls") in OCCLUDER_KINDS and obj.get("box"):
            add(obj["cls"].replace("_", " "), obj["box"])
    for box in layout.get("non_plant_boxes") or ():
        add("label", box)
    ruler = (layout.get("ruler") or {}).get("box")
    if ruler:
        add("ruler", ruler)
    return found


def box_distance(point, box):
    """Distance from a point to a box (0 inside it)."""
    dx = max(box[0] - point[0], 0.0, point[0] - box[2])
    dy = max(box[1] - point[1], 0.0, point[1] - box[3])
    return math.hypot(dx, dy)


def _edge_distance(point, box):
    """Distance from a point inside a box to the box's nearest side (negative outside)."""
    return min(point[0] - box[0], box[2] - point[0], point[1] - box[1], box[3] - point[1])


def touching(point, occluders, sheet_box, image_size, px_per_cm, tolerance_cm=EDGE_TOLERANCE_CM):
    """What an end point touches: {"kind", "box"} for an occluder, {"kind": "sheet edge"|"image edge"} or None."""
    tolerance = tolerance_cm * px_per_cm
    w, h = image_size
    if _edge_distance(point, [0, 0, w, h]) <= tolerance:
        return {"kind": "image edge", "box": None}
    nearest = min(occluders or (), key=lambda o: box_distance(point, o["box"]), default=None)
    if nearest and box_distance(point, nearest["box"]) <= tolerance:
        return {"kind": nearest["kind"], "box": nearest["box"]}
    if sheet_box and _edge_distance(point, sheet_box) <= tolerance:
        return {"kind": "sheet edge", "box": None}
    return None


def point_back(order, distance):
    """A point `distance` px back from the last point of a path (to get the heading at that end)."""
    travelled, last = 0.0, order[-1]
    for point in reversed(order[:-1]):
        travelled += math.hypot(point[0] - last[0], point[1] - last[1])
        last = point
        if travelled >= distance:
            break
    return last


def assess_end(plant, path, which, occluders, sheet_box, image_size):
    """State of one end of a traced path ("tip" = last point, "base" = first).

    Returns {"state": "free"|"over"|"hidden", "by": kind or "", "continues": bool}. `continues` says
    that plant shows again just beyond the occluder in the direction the path was heading, which is
    worth telling the reviewer: the structure probably carries on under the label.
    """
    if len(path) < 2:
        return {"state": "free", "by": "", "continues": False}
    end = path[-1] if which == "tip" else path[0]
    px_per_cm = plant.px_per_cm
    hit = touching(end, occluders, sheet_box, image_size, px_per_cm)
    if hit is None:
        return {"state": "free", "by": "", "continues": False}
    if hit["box"] is None:
        return {"state": "hidden", "by": hit["kind"], "continues": False}

    box, step = hit["box"], max(1.0, 0.05 * px_per_cm)
    order = list(path) if which == "tip" else list(path)[::-1]
    dense = densify(order, step)
    if box_distance(end, box) == 0:
        # The end is inside the box. If the path's last stretch in there runs over visible plant, the
        # plant lies on top of the label and the end is in full view.
        tail = []
        for point in reversed(dense):
            if box_distance(point, box) > 0 or len(tail) * step > 1.0 * px_per_cm:
                break
            tail.append(point)
        if plant.on_raw(tail) >= OVER_SHARE:
            return {"state": "over", "by": hit["kind"], "continues": False}
    behind = point_back(order, 1.0 * px_per_cm)
    norm = math.hypot(end[0] - behind[0], end[1] - behind[1]) or 1.0
    ux, uy = (end[0] - behind[0]) / norm, (end[1] - behind[1]) / norm
    inside, beyond, position, travelled = [], [], list(end), 0.0
    limit = math.hypot(box[2] - box[0], box[3] - box[1]) + 2.0 * px_per_cm
    while travelled < limit:
        position = [position[0] + ux * step, position[1] + uy * step]
        travelled += step
        if not (0 <= position[0] < image_size[0] and 0 <= position[1] < image_size[1]):
            break
        if box_distance(position, box) == 0:
            inside.append(position)
        elif inside:
            beyond.append(position)
            if len(beyond) * step > 1.5 * px_per_cm:
                break
    ahead = inside[:max(1, int(1.0 * px_per_cm / step))]
    if box_distance(end, box) > 0 and ahead and plant.on_raw(ahead) >= OVER_SHARE:
        # The end stops at the box's edge but visible plant carries on across the label.
        return {"state": "over", "by": hit["kind"], "continues": True}
    continues = bool(beyond) and plant.on_raw(beyond) >= 0.25
    return {"state": "hidden", "by": hit["kind"], "continues": continues}


def crossings(path, occluders, px_per_cm, plant=None):
    """Stretches of a path that run through an occluder and come out again.

    Returns [{"kind", "length_cm", "over": bool}] for stretches of at least MIN_HIDDEN_CM. `over` is
    True where the plant is visible on top of the occluder along the stretch. A stretch that reaches
    either end of the path is an end inside the box, which assess_end handles.
    """
    step = max(1.0, 0.05 * px_per_cm)
    dense = densify(list(path), step)
    found = []
    for occluder in occluders or ():
        box = occluder["box"]
        inside = [box_distance(point, box) == 0 for point in dense]
        i = 0
        while i < len(dense):
            if not inside[i]:
                i += 1
                continue
            j = i
            while j < len(dense) and inside[j]:
                j += 1
            if i > 0 and j < len(dense) and (j - i) * step >= MIN_HIDDEN_CM * px_per_cm:
                over = bool(plant) and plant.on_raw(dense[i:j]) >= OVER_SHARE
                found.append({"kind": occluder["kind"], "length_cm": round((j - i) * step / px_per_cm, 2),
                              "over": over})
            i = j
    return found


def densify(points, step):
    out = []
    for a, b in zip(points, points[1:]):
        n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / step))
        out += [[a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n] for i in range(n)]
    return out + [list(points[-1])] if points else out
