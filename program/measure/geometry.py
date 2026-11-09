"""Plant geometry on herbarium sheets: plant mask, snapping points to the plant, tracing lengths
along the plant, skeletons and their longest geodesic, widths and fold checks.

Lengths follow the plant (arc length along the mask), never a bounding box, so a bent leaf or a
curved panicle is not shortened to its chord. PlantImage methods take and return full-resolution
pixel coordinates and work on a downscaled copy sized for the job; the mask functions below it work
in the pixels of the mask they are given.
"""
import math

import numpy as np

TRACE_PX_PER_CM = 40.0      # 0.25 mm per pixel: enough to follow a panicle rachis
WIDTH_PX_PER_CM = 100.0     # 0.1 mm per pixel for leaf widths
EXCLUDE_PAD_CM = 0.12       # plant mask is cleared this far beyond a label or envelope box


def _cv2():
    import cv2
    return cv2


def paper_reference(light, a, b):
    """(L, a, b) of the sheet's paper in LAB (OpenCV 0-255 scale), from the commonest bright, uncoloured colour.

    Paper may be cream or neutral white, and a sheet is mostly paper, so the peak of the brightness histogram among
    bright, weakly coloured pixels is the paper whatever the tint. (Taking pixels that look cream, as this used to,
    picks out stains and plants on a white sheet and puts the paper too dark.)
    """
    bright = (light > 120) & (np.abs(a - 128) < 16) & (b > 118) & (b < 175)
    if bright.sum() < 100:
        return 220.0, 128.0, 136.0
    hist = np.bincount(light[bright].astype(np.int64), minlength=256)
    peak = int(np.convolve(hist, np.ones(5), mode="same").argmax())
    near = bright & (np.abs(light - peak) <= 12)
    return float(np.median(light[near])), float(np.median(a[near])), float(np.median(b[near]))


def plant_mask(rgb, exclude_boxes=(), sheet_box=None):
    """Pixels that are plant material (darker or more yellow/green than the paper), as uint8 0/1.

    Neutral grey/black ink (text, rulers, barcodes), red stamps and blue ink are removed, as is
    everything outside the sheet and inside `exclude_boxes` (labels, envelopes, colour charts).
    Boxes are [x0, y0, x1, y1] in the coordinates of `rgb`.
    """
    cv2 = _cv2()
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.int16)
    light, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    paper_l, _, paper_b = paper_reference(light, a, b)
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


def faint_mask(rgb, exclude_boxes=(), sheet_box=None):
    """A more sensitive plant mask for tracing: anything even slightly darker or more coloured than the paper.

    plant_mask is strict on purpose (it feeds measurements and checks), so it drops pale stems, and grey or purple
    spikelets that look like ink. A route that a person drew along such a plant is still a route on the plant;
    this mask lets the tracer see it. Boxes and the sheet edge are cleared as in plant_mask.
    """
    cv2 = _cv2()
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.int16)
    light, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    paper_l, paper_a, paper_b = paper_reference(light, a, b)
    chroma = np.hypot(a - 128, b - 128)
    paper_chroma = float(np.hypot(paper_a - 128, paper_b - 128))
    mask = ((light < paper_l - 12) | (b > paper_b + 7) | (chroma > paper_chroma + 10)) & (light > 25)
    mask &= ~(a > 150)
    mask &= ~(b < 120)
    h, w = mask.shape
    inside = np.zeros_like(mask)
    if sheet_box is not None:
        x0, y0, x1, y1 = _clip_box(sheet_box, w, h)
    else:
        x0, y0, x1, y1 = int(0.04 * w), int(0.04 * h), w - int(0.04 * w), h - int(0.04 * h)
    inside[y0:y1, x0:x1] = True
    mask &= inside
    for box in exclude_boxes or ():
        bx0, by0, bx1, by1 = _clip_box(box, w, h)
        mask[by0:by1, bx0:bx1] = False
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
        kernel = np.ones((3, 3), np.uint8)
        # `raw` keeps the plant inside label and envelope boxes; occlusion checks need to know whether
        # the plant runs on top of a label or disappears under it.
        self.raw = cv2.morphologyEx(plant_mask(small, (), sb), cv2.MORPH_CLOSE, kernel)
        self.mask = self.raw.copy()
        self._small = small
        self._faint = None                  # built on first use: only a route the strict mask cannot follow needs it
        h_work, w_work = self.mask.shape
        self.work_px_per_cm = px_per_cm * self.scale if px_per_cm else None
        # A label's edge and the shadow beside it look like a thin stem; clear a little beyond each box.
        pad = EXCLUDE_PAD_CM * self.work_px_per_cm if self.work_px_per_cm else 0
        self._excluded = []
        for box in exclude_boxes or ():
            x0, y0, x1, y1 = [v * self.scale for v in box]
            x0, y0, x1, y1 = _clip_box([x0 - pad, y0 - pad, x1 + pad, y1 + pad], w_work, h_work)
            self.mask[y0:y1, x0:x1] = 0
            self._excluded.append((x0, y0, x1, y1))

    @property
    def faint(self):
        """The sensitive mask (see faint_mask), a superset of the strict one, at the working scale."""
        if self._faint is None:
            cv2 = _cv2()
            # not clipped to the sheet box: when the sheet was detected too small, the plant outside it is still
            # plant, and the person has put the points there
            faint = faint_mask(self._small, (), None)
            for x0, y0, x1, y1 in self._excluded:
                faint[y0:y1, x0:x1] = 0
            self._faint = cv2.morphologyEx(faint, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)) | self.mask
        return self._faint

    def to_work(self, point):
        return (point[0] * self.scale, point[1] * self.scale)

    def to_full(self, point):
        return [round(point[0] / self.scale, 1), round(point[1] / self.scale, 1)]

    def snap(self, point, radius_cm=0.6, faint=False):
        """Nearest plant pixel to a full-res point within radius_cm, or None (`faint`: use the sensitive mask)."""
        x, y = self.to_work(point)
        r = max(2, int(radius_cm * self.work_px_per_cm))
        mask = self.faint if faint else self.mask
        h, w = mask.shape
        x0, x1, y0, y1 = max(0, int(x) - r), min(w, int(x) + r + 1), max(0, int(y) - r), min(h, int(y) + r + 1)
        ys, xs = np.nonzero(mask[y0:y1, x0:x1])
        if not len(xs):
            return None
        d = (xs + x0 - x) ** 2 + (ys + y0 - y) ** 2
        i = int(d.argmin())
        if d[i] > r * r:
            return None
        return self.to_full((xs[i] + x0, ys[i] + y0))

    def _cost(self, box, prefer=None, faint=False):
        """Travel cost in a work-scale box: cheap along thick plant parts, expensive across paper.

        `prefer` is a full-resolution outline (the object's own mask); plant outside it costs three
        times as much, so the path stays on the object instead of jumping to a neighbouring stem.
        """
        cv2 = _cv2()
        x0, y0, x1, y1 = box
        sub = self.mask[y0:y1, x0:x1]
        dist = cv2.distanceTransform(sub, cv2.DIST_L2, 3)
        cost = np.where(sub > 0, 1.0 / (1.0 + 0.5 * dist), 40.0)
        if faint:                                      # pale plant: dearer than solid plant, far cheaper than paper
            cost = np.where((sub == 0) & (self.faint[y0:y1, x0:x1] > 0), 2.0, cost)
        if prefer is not None and len(prefer) >= 3:
            inside = polygon_mask(prefer, sub.shape, offset=(x0, y0), scale=self.scale)
            reach = max(1, int(0.3 * self.work_px_per_cm))
            inside = cv2.dilate(inside, np.ones((2 * reach + 1, 2 * reach + 1), np.uint8))
            cost = np.where((sub > 0) & (inside == 0), cost * 3.0, cost)
        return cost.astype(np.float64)

    def _box_around(self, points, margin_cm):
        h, w = self.mask.shape
        m = int(margin_cm * self.work_px_per_cm)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (max(0, int(min(xs)) - m), max(0, int(min(ys)) - m),
                min(w, int(max(xs)) + m + 1), min(h, int(max(ys)) + m + 1))

    def trace(self, start, end, margin_cm=4.0, prefer=None, faint=False):
        """Lowest-cost path along the plant between two full-res points (the geodesic between landmarks).

        Returns (path in full-res pixels, length in cm, fraction of the path on plant pixels).
        With `faint` the route may also use pale plant (the sensitive mask), and counts it as plant.
        """
        from skimage.graph import route_through_array

        a, b = self.to_work(start), self.to_work(end)
        box = self._box_around([a, b], margin_cm)
        cost = self._cost(box, prefer, faint)
        x0, y0 = box[0], box[1]
        h, w = cost.shape
        ra = (min(h - 1, max(0, int(a[1]) - y0)), min(w - 1, max(0, int(a[0]) - x0)))
        rb = (min(h - 1, max(0, int(b[1]) - y0)), min(w - 1, max(0, int(b[0]) - x0)))
        indices, _ = route_through_array(cost, ra, rb, fully_connected=True, geometric=True)
        path = np.array([(c + x0, r + y0) for r, c in indices], dtype=np.float64)
        seen = self.faint if faint else self.mask
        on_plant = float(np.mean([seen[int(y), int(x)] > 0 for x, y in path])) if len(path) else 0.0
        smooth = smooth_path(path, window=max(3, int(0.2 * self.work_px_per_cm)))
        length_cm = polyline_length(smooth) / self.work_px_per_cm
        return [self.to_full(p) for p in simplify(smooth, 0.5)], length_cm, on_plant

    def entry_to_box(self, anchor, target, box, band_cm=0.3):
        """Where the plant that starts at `anchor` meets `box` on its way to `target` (which is inside the box).

        Used for a landmark the model placed under a label: among the plant pixels in a thin band
        around the box, the one that is reached from the anchor along the plant and lies nearest the
        target. Plant that can only be reached across paper (another stem, a leaf) costs far more
        and is not chosen. Returns a full-res point or None.
        """
        from skimage.graph import MCP_Geometric

        a, t = self.to_work(anchor), self.to_work(target)
        bx0, by0, bx1, by1 = [v * self.scale for v in box]
        pad = EXCLUDE_PAD_CM * self.work_px_per_cm
        band = band_cm * self.work_px_per_cm
        region = self._box_around([a, t, (bx0, by0), (bx1, by1)], 1.0)
        x0, y0 = region[0], region[1]
        cost = self._cost(region)
        h, w = cost.shape
        start = (min(h - 1, max(0, int(a[1]) - y0)), min(w - 1, max(0, int(a[0]) - x0)))
        reached, _ = MCP_Geometric(cost).find_costs([start])
        yy, xx = np.mgrid[y0:y0 + h, x0:x0 + w]
        outside = np.maximum.reduce([bx0 - pad - xx, xx - (bx1 + pad), by0 - pad - yy, yy - (by1 + pad)])
        near_box = (outside >= 0) & (outside <= band) & (self.mask[y0:y0 + h, x0:x0 + w] > 0)
        if not near_box.any():
            return None
        score = np.where(near_box, reached + 0.5 * np.hypot(xx - t[0], yy - t[1]), np.inf)
        iy, ix = np.unravel_index(int(score.argmin()), score.shape)
        # reaching it must have been along the plant: far cheaper than walking the same distance on paper
        if reached[iy, ix] > 5.0 * math.hypot(ix + x0 - a[0], iy + y0 - a[1]):
            return None
        return self.to_full((ix + x0, iy + y0))

    def continues_past(self, anchor, tip, radius_cm=3.0):
        """How far (cm) the plant carries on beyond `tip`, walking along the plant away from `anchor`.

        Distances are measured along plant pixels only. Around a true tip nothing is further from the
        base than the tip itself; when the tip was placed part-way along a panicle (or on the end of
        a side branch) the main axis runs on, and its extra length within radius_cm is returned.
        """
        cv2 = _cv2()
        from skimage.graph import MCP_Geometric

        a, t = self.to_work(anchor), self.to_work(tip)
        x0, y0, x1, y1 = self._box_around([a, t], radius_cm + 0.5)
        walkable = cv2.dilate(self.mask[y0:y1, x0:x1], np.ones((3, 3), np.uint8))   # tolerate one-pixel breaks
        cost = np.where(walkable > 0, 1.0, np.inf)
        h, w = cost.shape
        start = (min(h - 1, max(0, int(a[1]) - y0)), min(w - 1, max(0, int(a[0]) - x0)))
        end = (min(h - 1, max(0, int(t[1]) - y0)), min(w - 1, max(0, int(t[0]) - x0)))
        if not walkable[start] or not walkable[end]:
            return 0.0
        reached, _ = MCP_Geometric(cost).find_costs([start])
        if not np.isfinite(reached[end]):
            return 0.0
        yy, xx = np.ogrid[:h, :w]
        near = (xx - end[1]) ** 2 + (yy - end[0]) ** 2 <= (radius_cm * self.work_px_per_cm) ** 2
        beyond = np.where(near & np.isfinite(reached), reached - reached[end], 0.0)
        return float(max(beyond.max(), 0.0) / self.work_px_per_cm)

    def share_on(self, points, raw=False):
        """Share of full-res points that lie on plant pixels (`raw`: counting plant inside label boxes too)."""
        mask = self.raw if raw else self.mask
        h, w = mask.shape
        hits = [mask[min(h - 1, max(0, int(y))), min(w - 1, max(0, int(x)))] > 0
                for x, y in map(self.to_work, points)]
        return float(np.mean(hits)) if hits else 0.0

    def on_raw(self, points):
        return self.share_on(points, raw=True)

    def region_near(self, point, radius_cm=4.0):
        """(mask, offset, scale) of the plant pixels connected to `point` within radius_cm.

        The colour-based stand-in for a model's object mask when SAM 2 and trained masks are absent.
        """
        cv2 = _cv2()
        snapped = self.snap(point, radius_cm=0.4)
        if snapped is None:
            return None
        x, y = self.to_work(snapped)
        x0, y0, x1, y1 = self._box_around([(x, y)], radius_cm)
        sub = self.mask[y0:y1, x0:x1]
        count, labels = cv2.connectedComponents(sub)
        label = labels[min(sub.shape[0] - 1, int(y) - y0), min(sub.shape[1] - 1, int(x) - x0)]
        if not label:
            return None
        return (labels == label).astype(np.uint8), (x0, y0), self.scale

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


def spikelet_axis(rgb, box, pad=0.15):
    """(base, tip, length_px, width_px) of the spikelet inside `box` ([x0, y0, x1, y1] in rgb's pixels), or None.

    Pixels that differ in hue from the paper (grey shadows do not) are grouped and the group nearest
    the box centre is kept. Anything much thinner than the spikelet's body (its pedicel, a branch, an
    awn) is then opened away, so the length is the body's own long axis and not body plus stalk.
    Base and tip are the axis ends, ordered so that the tip is the upper end.
    """
    cv2 = _cv2()
    h, w = rgb.shape[:2]
    bw, bh = box[2] - box[0], box[3] - box[1]
    x0, y0 = max(0, int(box[0] - pad * bw)), max(0, int(box[1] - pad * bh))
    x1, y1 = min(w, int(box[2] + pad * bw)), min(h, int(box[3] + pad * bh))
    sub = rgb[y0:y1, x0:x1]
    if sub.shape[0] < 6 or sub.shape[1] < 6:
        return None
    lab = cv2.cvtColor(sub, cv2.COLOR_RGB2LAB).astype(np.float32)
    ring = np.ones(sub.shape[:2], bool)
    ring[int(0.25 * sub.shape[0]):int(0.75 * sub.shape[0]), int(0.25 * sub.shape[1]):int(0.75 * sub.shape[1])] = False
    light = lab[..., 0]
    paper = ring & (light >= np.percentile(light[ring], 40))
    if paper.sum() < 20:
        return None
    paper_ab = np.median(lab[..., 1:][paper], axis=0)
    dist = np.hypot(*(lab[..., 1:] - paper_ab).transpose(2, 0, 1))
    mask = (dist > max(6.0, np.percentile(dist[paper], 95) * 1.5)).astype(np.uint8)
    centre = np.array([(box[0] + box[2]) / 2 - x0, (box[1] + box[3]) / 2 - y0])

    def nearest_blob(binary):
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary)
        if count < 2:
            return None
        best = max(range(1, count), key=lambda i: stats[i, cv2.CC_STAT_AREA] / (
            1 + np.linalg.norm(centroids[i] - centre) / max(bw, bh)))
        return (labels == best).astype(np.uint8)

    def without_thin_parts(binary, reach):
        """The blob nearest the centre once everything thinner than 2 x reach is opened away.

        Opening also rounds off a spikelet's pointed ends, so the result is grown back into the
        original a little further than the reach: the ends return in full, and of a stalk only a
        short stub, which body_axis then recognises and leaves out.
        """
        if reach < 1:
            return nearest_blob(binary)
        disc = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * reach + 1, 2 * reach + 1))
        kept = nearest_blob(cv2.morphologyEx(binary, cv2.MORPH_OPEN, disc))
        if kept is None or kept.sum() < 30:
            return nearest_blob(binary)
        grow = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * reach + 7, 2 * reach + 7))
        return cv2.dilate(kept, grow) & binary

    # First against the box (pedicels and fine branches are thin beside it), which separates the
    # spikelet from what it hangs on; then against the spikelet's own body width.
    blob = without_thin_parts(mask, max(1, int(0.03 * min(bw, bh))))
    if blob is None or blob.sum() < 30:
        return None
    body = float(cv2.distanceTransform(np.pad(blob, 1), cv2.DIST_L2, 5).max())    # half the body's width
    blob = without_thin_parts(blob, int(0.25 * body))
    if blob is None:
        return None
    axis = body_axis(blob)
    if axis is None:
        return None
    ends = sorted((axis[0], axis[1]), key=lambda e: e[1])                 # upper end first = tip
    tip, base = [[round(e[0] + x0, 1), round(e[1] + y0, 1)] for e in ends]
    return base, tip, axis[2], axis[3]


# --------------------------------------------------------------------------- masks, skeletons and widths

SQRT2 = math.sqrt(2.0)


def polygon_mask(polygon, shape, offset=(0, 0), scale=1.0):
    """uint8 0/1 mask of `shape` for a full-resolution outline, in a crop at `offset` drawn at `scale`."""
    cv2 = _cv2()
    mask = np.zeros(shape[:2], np.uint8)
    points = np.asarray(polygon, dtype=np.float64) * scale - np.asarray(offset, dtype=np.float64)
    if len(points) >= 3:
        cv2.fillPoly(mask, [np.round(points).astype(np.int32)], 1)
    return mask


def mask_polygon(mask, offset=(0, 0), scale=1.0, tolerance=1.0):
    """Outline of the largest blob in a mask as [[x, y], ...] in full-resolution pixels (or None).

    `offset` is the crop's top-left corner in the mask's own pixels and `scale` its pixels per
    full-resolution pixel, so a mask made on a downscaled crop maps back to the sheet.
    """
    cv2 = _cv2()
    contours, _ = cv2.findContours((np.asarray(mask) > 0).astype(np.uint8), cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < 4:
        return None
    contour = cv2.approxPolyDP(contour, tolerance, True)[:, 0, :].astype(np.float64)
    if len(contour) < 3:
        return None
    return [[round(float((x + offset[0]) / scale), 1), round(float((y + offset[1]) / scale), 1)] for x, y in contour]


def point_along(points, share):
    """The point a given share (0 to 1) of the way along a path."""
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:])]
    target, walked = share * sum(lengths), 0.0
    for (a, b), length in zip(zip(points, points[1:]), lengths):
        if length and walked + length >= target:
            t = (target - walked) / length
            return [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]
        walked += length
    return list(points[-1])


def largest_blob(mask):
    cv2 = _cv2()
    count, labels, stats, _ = cv2.connectedComponentsWithStats((np.asarray(mask) > 0).astype(np.uint8))
    if count < 2:
        return np.zeros(np.asarray(mask).shape, np.uint8)
    best = 1 + int(stats[1:, cv2.CC_STAT_AREA].argmax())
    return (labels == best).astype(np.uint8)


def skeleton_path(mask):
    """The longest geodesic of a mask's skeleton: (path as an (N, 2) x/y array in mask pixels, length in px).

    The mask is thinned to a one-pixel skeleton, the skeleton becomes a graph (8-connected pixels,
    diagonal steps cost sqrt 2) and the longest shortest path between two of its ends is found with
    two sweeps. For a leaf this is the midrib from base to tip, however the leaf bends.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components, dijkstra
    from skimage.morphology import skeletonize

    skeleton = skeletonize(np.asarray(mask) > 0)
    ys, xs = np.nonzero(skeleton)
    n = len(xs)
    if n < 2:
        return np.zeros((0, 2)), 0.0
    h, w = skeleton.shape
    index = np.full((h, w), -1, np.int64)
    index[ys, xs] = np.arange(n)
    rows, cols, weights = [], [], []
    for dy, dx, weight in ((0, 1, 1.0), (1, 0, 1.0), (1, 1, SQRT2), (1, -1, SQRT2)):
        y2, x2 = ys + dy, xs + dx
        ok = (y2 >= 0) & (y2 < h) & (x2 >= 0) & (x2 < w)
        other = np.full(n, -1, np.int64)
        other[ok] = index[y2[ok], x2[ok]]
        hit = np.nonzero(other >= 0)[0]
        rows.append(hit)
        cols.append(other[hit])
        weights.append(np.full(len(hit), weight))
    graph = coo_matrix((np.concatenate(weights), (np.concatenate(rows), np.concatenate(cols))), shape=(n, n)).tocsr()
    count, component = connected_components(graph, directed=False)
    main = int(np.bincount(component).argmax())
    start = int(np.nonzero(component == main)[0][0])
    first = dijkstra(graph, directed=False, indices=start)
    first[~np.isfinite(first)] = -1
    a = int(first.argmax())
    second, previous = dijkstra(graph, directed=False, indices=a, return_predecessors=True)
    second[~np.isfinite(second)] = -1
    b = int(second.argmax())
    nodes = [b]
    while nodes[-1] != a and previous[nodes[-1]] >= 0:
        nodes.append(int(previous[nodes[-1]]))
    path = np.stack([xs[nodes], ys[nodes]], axis=1).astype(np.float64)
    return path, float(second[b])


def arc_length(path, window=5):
    """Length of a pixel path after light smoothing (a raw pixel staircase reads a few percent long)."""
    path = np.asarray(path, dtype=np.float64)
    return polyline_length(smooth_path(path, window)) if len(path) >= 2 else 0.0


def width_along(mask, path):
    """Width of the mask (px) at each point of a path that runs along its middle."""
    cv2 = _cv2()
    dist = cv2.distanceTransform((np.asarray(mask) > 0).astype(np.uint8), cv2.DIST_L2, 5)
    path = np.asarray(path)
    if not len(path):
        return np.zeros(0)
    h, w = dist.shape
    xs = np.clip(np.round(path[:, 0]).astype(int), 0, w - 1)
    ys = np.clip(np.round(path[:, 1]).astype(int), 0, h - 1)
    return np.maximum(2.0 * dist[ys, xs] - 1.0, 0.0)


def widest_point(mask):
    """(width px, centre, edge a, edge b) at the widest part of a ribbon-like mask, or None.

    The width is the diameter of the largest circle that fits inside the mask, which is the
    margin-to-margin distance at the widest point of a blade whatever way the blade lies.
    """
    cv2 = _cv2()
    solid = (np.asarray(mask) > 0).astype(np.uint8)
    if not solid.any():
        return None
    padded = np.pad(solid, 1)
    dist = cv2.distanceTransform(padded, cv2.DIST_L2, 5)[1:-1, 1:-1]
    cy, cx = np.unravel_index(int(dist.argmax()), dist.shape)
    radius = float(dist[cy, cx])
    if radius < 1:
        return None
    edge = solid - cv2.erode(padded, np.ones((3, 3), np.uint8))[1:-1, 1:-1]
    ys, xs = np.nonzero(edge)
    nearest = int(((xs - cx) ** 2 + (ys - cy) ** 2).argmin())
    ax, ay = float(xs[nearest]), float(ys[nearest])
    norm = math.hypot(ax - cx, ay - cy) or 1.0
    ux, uy = (ax - cx) / norm, (ay - cy) / norm
    half = radius - 0.5
    return (2.0 * radius - 1.0, [float(cx), float(cy)], [cx + ux * half, cy + uy * half],
            [cx - ux * half, cy - uy * half])


def solidity(mask):
    """Mask area over the area of its convex hull (1 for a convex shape)."""
    cv2 = _cv2()
    solid = (np.asarray(mask) > 0).astype(np.uint8)
    points = cv2.findNonZero(solid)
    if points is None or len(points) < 3:
        return 0.0
    hull = cv2.contourArea(cv2.convexHull(points))
    return float(solid.sum() / hull) if hull > 0 else 0.0


WIDTH_JUMP = 0.5            # a change of half the median width over JUMP_SPAN_CM is "sudden"
JUMP_SPAN_CM = 0.3
WIDTH_BULGE = 1.4           # a blade this much wider at one place than its median width is two blades there
STRAIGHT = 0.9              # chord / arc above this is a straight blade
MIN_SOLIDITY = 0.6


def blade_checks(mask, px_per_cm):
    """Shape facts about a leaf-blade mask and the fold/overlap warnings they raise.

    Returns (facts, codes). facts: length_px (arc length of the midrib), chord_px, width_px (median),
    max_width_px, jump (largest sudden change in width relative to the median width), solidity.
    codes: "width_jump" where the width changes suddenly along the blade or bulges well beyond its
    usual width (a fold, or two blades overlapping), "low_solidity" where a straight blade has a
    ragged outline.
    """
    path, _ = skeleton_path(mask)
    facts = {"length_px": 0.0, "chord_px": 0.0, "width_px": 0.0, "max_width_px": 0.0, "jump": 0.0,
             "solidity": round(solidity(mask), 3)}
    if len(path) < 5:
        return facts, []
    widths = width_along(mask, path)
    trim = max(1, len(path) // 10)                 # the ends taper naturally
    core = widths[trim:-trim] if len(widths) > 2 * trim + 2 else widths
    median = float(np.median(core)) or 1.0
    span = max(2, int(JUMP_SPAN_CM * px_per_cm))
    smooth = np.convolve(core, np.ones(3) / 3, mode="same") if len(core) >= 3 else core
    jump = float(np.abs(smooth[span:] - smooth[:-span]).max() / median) if len(smooth) > span else 0.0
    length = arc_length(path)
    chord = float(np.hypot(*(path[-1] - path[0])))
    facts.update(length_px=round(length, 1), chord_px=round(chord, 1), width_px=round(median, 1),
                 max_width_px=round(float(core.max()), 1), jump=round(jump, 2))
    found = widest_point(mask)
    widest = found[0] if found else float(core.max())
    facts["max_width_px"] = round(widest, 1)
    codes = []
    if jump > WIDTH_JUMP or widest > WIDTH_BULGE * median:
        codes.append("width_jump")
    if length and chord / length > STRAIGHT and facts["solidity"] < MIN_SOLIDITY:
        codes.append("low_solidity")
    return facts, codes


def principal_axis(mask):
    """(end a, end b, length px, width px) of a compact blob along its long axis, or None."""
    ys, xs = np.nonzero(np.asarray(mask) > 0)
    if len(xs) < 12:
        return None
    points = np.stack([xs, ys], axis=1).astype(np.float64)
    mean = points.mean(axis=0)
    _, vectors = np.linalg.eigh(np.cov((points - mean).T))
    along, across = (points - mean) @ vectors[:, 1], (points - mean) @ vectors[:, 0]
    lo, hi = np.percentile(along, [0.2, 99.8])
    wlo, whi = np.percentile(across, [1, 99])
    ends = [mean + vectors[:, 1] * lo, mean + vectors[:, 1] * hi]
    return ([float(ends[0][0]), float(ends[0][1])], [float(ends[1][0]), float(ends[1][1])],
            float(hi - lo) + 1.0, float(whi - wlo) + 1.0)


def body_axis(mask, min_width=3, body_share=0.3):
    """(end a, end b, length px, width px) of a spikelet's body along its long axis, without its stalk.

    The blob is sliced across its long axis. Walking out from the middle, the body is where a slice
    is at least `body_share` of the body's full width (and at least min_width px). A natural tip
    narrows to nothing within a few percent of the length and is kept; anything thin that runs on
    further, such as a stalk or an awn in line with the spikelet, is left out.
    """
    ys, xs = np.nonzero(np.asarray(mask) > 0)
    if len(xs) < 12:
        return None
    points = np.stack([xs, ys], axis=1).astype(np.float64)
    mean = points.mean(axis=0)
    _, vectors = np.linalg.eigh(np.cov((points - mean).T))
    along, across = (points - mean) @ vectors[:, 1], (points - mean) @ vectors[:, 0]
    start = along.min()
    counts = np.bincount(np.floor(along - start).astype(int))
    full = float(np.percentile(counts[counts > 0], 90))
    threshold = max(min_width, body_share * full)
    middle = int(np.argmax(counts * (np.abs(np.arange(len(counts)) + start) < 0.5 * len(counts))))
    if counts[middle] < threshold:
        return principal_axis(mask)
    lo = hi = middle
    while lo > 0 and counts[lo - 1] >= threshold:
        lo -= 1
    while hi < len(counts) - 1 and counts[hi + 1] >= threshold:
        hi += 1
    tip_px = max(2, int(round(0.05 * (hi - lo + 1))))

    def thin_run(index, step):
        length = 0
        while 0 <= index < len(counts) and 0 < counts[index] < threshold:
            length, index = length + 1, index + step
        return length

    below, above = thin_run(lo - 1, -1), thin_run(hi + 1, 1)
    lo -= min(below, tip_px)            # a tip keeps its point; a stalk keeps no more than a tip would
    hi += min(above, tip_px)
    body = (along - start >= lo) & (along - start < hi + 1)
    wlo, whi = np.percentile(across[body], [1, 99])
    ends = [mean + vectors[:, 1] * (start + lo), mean + vectors[:, 1] * (start + hi + 1)]
    return ([float(ends[0][0]), float(ends[0][1])], [float(ends[1][0]), float(ends[1][1])],
            float(hi - lo + 1), float(whi - wlo) + 1.0)


def distance_to_edge(mask, point):
    """How far inside the mask a point lies, in px (0 on the outline or outside it)."""
    cv2 = _cv2()
    solid = np.pad((np.asarray(mask) > 0).astype(np.uint8), 1)
    dist = cv2.distanceTransform(solid, cv2.DIST_L2, 3)[1:-1, 1:-1]
    h, w = dist.shape
    x, y = int(round(point[0])), int(round(point[1]))
    return float(dist[y, x]) if 0 <= x < w and 0 <= y < h else 0.0
