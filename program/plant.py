"""Plant geometry on herbarium sheets: plant mask, snapping points to the plant, tracing lengths
along the plant and measuring widths. All public functions take and return full-resolution
pixel coordinates; work is done on a downscaled copy sized for the job.
"""
import math

import numpy as np

TRACE_PX_PER_CM = 40.0      # 0.25 mm per pixel: enough to follow a panicle rachis
WIDTH_PX_PER_CM = 100.0     # 0.1 mm per pixel for leaf widths


def _cv2():
    import cv2
    return cv2


def plant_mask(rgb, exclude_boxes=(), sheet_box=None):
    """Pixels that are plant material (darker or more yellow/green than the paper), as uint8 0/1.

    Neutral grey/black ink (text, rulers, barcodes), red stamps and blue ink are removed, as is
    everything outside the sheet and inside `exclude_boxes` (labels, envelopes, colour charts).
    Boxes are [x0, y0, x1, y1] in the coordinates of `rgb`.
    """
    cv2 = _cv2()
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.int16)
    light, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    paper_like = (light > 120) & (b >= 130)
    paper_l = np.median(light[paper_like]) if paper_like.any() else 220
    paper_b = np.median(b[paper_like]) if paper_like.any() else 136
    chroma = np.hypot(a - 128, b - 128)
    mask = ((light < paper_l - 30) | (b > paper_b + 14)) & (light > 25)
    mask &= ~((chroma < 8) & (light < paper_l - 30))
    mask &= ~(a > 150)
    mask &= ~(b < 120)
    h, w = mask.shape
    if sheet_box is not None:
        x0, y0, x1, y1 = _clip_box(sheet_box, w, h)
        inside = np.zeros_like(mask)
        inside[y0:y1, x0:x1] = True
        mask &= inside
    else:
        ey, ex = int(0.04 * h), int(0.04 * w)
        mask[:ey], mask[-ey:], mask[:, :ex], mask[:, -ex:] = False, False, False, False
    for box in exclude_boxes or ():
        x0, y0, x1, y1 = _clip_box(box, w, h)
        mask[y0:y1, x0:x1] = False
    return mask.astype(np.uint8)


def _clip_box(box, w, h):
    x0, y0, x1, y1 = box
    x0, x1 = sorted((int(round(x0)), int(round(x1))))
    y0, y1 = sorted((int(round(y0)), int(round(y1))))
    return max(0, x0), max(0, y0), min(w, x1), min(h, y1)


class PlantImage:
    """A sheet image with a cached plant mask at a working scale."""

    def __init__(self, rgb, px_per_cm, exclude_boxes=(), sheet_box=None, work_px_per_cm=TRACE_PX_PER_CM):
        cv2 = _cv2()
        self.full_shape = rgb.shape[:2]
        self.px_per_cm = px_per_cm
        self.scale = min(1.0, work_px_per_cm / px_per_cm) if px_per_cm else 1.0
        h, w = self.full_shape
        small = rgb if self.scale >= 1 else cv2.resize(rgb, (max(1, int(w * self.scale)), max(1, int(h * self.scale))),
                                                        interpolation=cv2.INTER_AREA)
        sb = [v * self.scale for v in sheet_box] if sheet_box else None
        boxes = [[v * self.scale for v in box] for box in exclude_boxes or ()]
        mask = plant_mask(small, boxes, sb)
        self.mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        self.work_px_per_cm = px_per_cm * self.scale if px_per_cm else None

    def to_work(self, point):
        return (point[0] * self.scale, point[1] * self.scale)

    def to_full(self, point):
        return [round(point[0] / self.scale, 1), round(point[1] / self.scale, 1)]

    def snap(self, point, radius_cm=0.6):
        """Nearest plant pixel to a full-res point within radius_cm, or None."""
        x, y = self.to_work(point)
        r = max(2, int(radius_cm * self.work_px_per_cm))
        h, w = self.mask.shape
        x0, x1, y0, y1 = max(0, int(x) - r), min(w, int(x) + r + 1), max(0, int(y) - r), min(h, int(y) + r + 1)
        ys, xs = np.nonzero(self.mask[y0:y1, x0:x1])
        if not len(xs):
            return None
        d = (xs + x0 - x) ** 2 + (ys + y0 - y) ** 2
        i = int(d.argmin())
        if d[i] > r * r:
            return None
        return self.to_full((xs[i] + x0, ys[i] + y0))

    def _cost(self, box):
        """Travel cost in a work-scale box: cheap along thick plant parts, expensive across paper."""
        cv2 = _cv2()
        x0, y0, x1, y1 = box
        sub = self.mask[y0:y1, x0:x1]
        dist = cv2.distanceTransform(sub, cv2.DIST_L2, 3)
        cost = np.where(sub > 0, 1.0 / (1.0 + 0.5 * dist), 40.0)
        return cost.astype(np.float64)

    def _box_around(self, points, margin_cm):
        h, w = self.mask.shape
        m = int(margin_cm * self.work_px_per_cm)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (max(0, int(min(xs)) - m), max(0, int(min(ys)) - m),
                min(w, int(max(xs)) + m + 1), min(h, int(max(ys)) + m + 1))

    def trace(self, start, end, margin_cm=4.0):
        """Lowest-cost path along the plant between two full-res points.

        Returns (path in full-res pixels, length in cm, fraction of the path on plant pixels).
        """
        from skimage.graph import route_through_array

        a, b = self.to_work(start), self.to_work(end)
        box = self._box_around([a, b], margin_cm)
        cost = self._cost(box)
        x0, y0 = box[0], box[1]
        h, w = cost.shape
        ra = (min(h - 1, max(0, int(a[1]) - y0)), min(w - 1, max(0, int(a[0]) - x0)))
        rb = (min(h - 1, max(0, int(b[1]) - y0)), min(w - 1, max(0, int(b[0]) - x0)))
        indices, _ = route_through_array(cost, ra, rb, fully_connected=True, geometric=True)
        path = np.array([(c + x0, r + y0) for r, c in indices], dtype=np.float64)
        on_plant = float(np.mean([self.mask[int(y), int(x)] > 0 for x, y in path])) if len(path) else 0.0
        smooth = smooth_path(path, window=max(3, int(0.2 * self.work_px_per_cm)))
        length_cm = polyline_length(smooth) / self.work_px_per_cm
        return [self.to_full(p) for p in simplify(smooth, 0.5)], length_cm, on_plant

    def width_across(self, a, b, reach=0.5):
        """Width (cm) of the plant run along the line a-b (full-res points), measured on the plant mask.

        The line is extended by `reach` of its length each way; the run of plant pixels that contains
        (or lies nearest to) the midpoint is the blade. Returns (width_cm, edge_a, edge_b) or None.
        """
        (ax, ay), (bx, by) = self.to_work(a), self.to_work(b)
        length = math.hypot(bx - ax, by - ay)
        if length < 2:
            return None
        n = int(length * (1 + 2 * reach)) * 2 + 1
        t = np.linspace(-reach, 1 + reach, n)
        xs, ys = ax + t * (bx - ax), ay + t * (by - ay)
        h, w = self.mask.shape
        ok = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
        on = np.zeros(n, bool)
        on[ok] = self.mask[ys[ok].astype(int), xs[ok].astype(int)] > 0
        if not on.any():
            return None
        mid = int(np.argmin(np.abs(t - 0.5)))
        if not on[mid]:
            near = np.flatnonzero(on)
            mid = int(near[np.argmin(np.abs(near - mid))])
        lo, hi = mid, mid
        while lo > 0 and on[lo - 1]:
            lo -= 1
        while hi < n - 1 and on[hi + 1]:
            hi += 1
        step = length * (1 + 2 * reach) / (n - 1)
        edge_a, edge_b = (xs[lo], ys[lo]), (xs[hi], ys[hi])
        return ((hi - lo) * step / self.work_px_per_cm, self.to_full(edge_a), self.to_full(edge_b))

    def extremity_near(self, anchor, near, radius_cm=1.5, margin_cm=4.0):
        """The plant pixel within radius_cm of `near` that is farthest (along the plant) from `anchor`.

        Used to put a panicle tip on the real end of the rachis rather than where the AI pointed.
        """
        from skimage.graph import MCP_Geometric

        a, n = self.to_work(anchor), self.to_work(near)
        box = self._box_around([a, n], margin_cm)
        cost = self._cost(box)
        x0, y0 = box[0], box[1]
        h, w = cost.shape
        mcp = MCP_Geometric(cost)
        start = (min(h - 1, max(0, int(a[1]) - y0)), min(w - 1, max(0, int(a[0]) - x0)))
        dist, _ = mcp.find_costs([start])
        r = int(radius_cm * self.work_px_per_cm)
        yy, xx = np.ogrid[:h, :w]
        window = ((xx + x0 - n[0]) ** 2 + (yy + y0 - n[1]) ** 2 <= r * r)
        sub_mask = self.mask[y0:y0 + h, x0:x0 + w] > 0
        candidates = window & sub_mask & np.isfinite(dist) & (dist < 1e6)
        if not candidates.any():
            return None
        score = np.where(candidates, dist, -1)
        iy, ix = np.unravel_index(int(score.argmax()), score.shape)
        return self.to_full((ix + x0, iy + y0))


def smooth_path(path, window):
    if len(path) < window + 2:
        return path
    kernel = np.ones(window) / window
    pad = window // 2
    padded = np.vstack([np.repeat(path[:1], pad, 0), path, np.repeat(path[-1:], pad, 0)])
    xs = np.convolve(padded[:, 0], kernel, mode="valid")
    ys = np.convolve(padded[:, 1], kernel, mode="valid")
    out = np.stack([xs, ys], axis=1)[:len(path)]
    out[0], out[-1] = path[0], path[-1]
    return out


def polyline_length(points):
    pts = np.asarray(points, dtype=np.float64)
    if len(pts) < 2:
        return 0.0
    return float(np.hypot(*np.diff(pts, axis=0).T).sum())


def simplify(points, tolerance):
    """Douglas-Peucker simplification (keeps the drawn overlay small; length uses the full path)."""
    pts = np.asarray(points, dtype=np.float64)
    if len(pts) < 3:
        return pts
    start, end = pts[0], pts[-1]
    seg = end - start
    norm = math.hypot(*seg) or 1.0
    rel = pts - start
    d = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / norm
    i = int(d.argmax())
    if d[i] <= tolerance:
        return np.array([start, end])
    return np.vstack([simplify(pts[:i + 1], tolerance)[:-1], simplify(pts[i:], tolerance)])


def segment_length_cm(a, b, px_per_cm):
    return math.hypot(b[0] - a[0], b[1] - a[1]) / px_per_cm
