"""The decision layer: what each measurement is worth.

Every measured object carries flags. A flag has a code, a sentence for people and a severity, and the
severities decide the status with four rules that can be read off this file:

    not measurable   any REJECT flag (folded, too few pixels, no scale, outside the possible range ...)
    lower bound      otherwise, any CENSOR flag (an end is hidden by a label, tape or the sheet edge)
    needs review     otherwise, any REVIEW flag (unusual value, two methods disagree, low confidence ...)
    accept           no flags of those kinds

A sheet's value for a trait is then built from its objects (decide_trait). The confidence is a
transparent score, not a calibrated probability: detector score x geometric quality x scale quality,
reduced for every review flag.
"""
import statistics

from .taxa import TRAIT_NAMES

ACCEPT = "accept"
LOWER_BOUND = "lower bound"
NOT_MEASURABLE = "not measurable"
NEEDS_REVIEW = "needs review"
STATUSES = (ACCEPT, NEEDS_REVIEW, LOWER_BOUND, NOT_MEASURABLE)

REJECT, CENSOR, REVIEW, INFO = "reject", "censor", "review", "info"

# code: (severity, what it means)
FLAGS = {
    # the object cannot give a value
    "no_scale": (REJECT, "no ruler and no usable sheet width, so there is no scale"),
    "low_resolution": (REJECT, "the image resolution is too low for this trait"),
    "too_small": (REJECT, "the structure spans too few pixels to measure"),
    "folded": (REJECT, "the leaf is folded or overlapped"),
    "out_of_range": (REJECT, "the value is outside the possible range for this taxon"),
    "off_plant": (REJECT, "the traced path does not follow the plant"),
    "degenerate": (REJECT, "the two ends are almost the same point"),
    "not_on_plant": (REJECT, "a marked point is not on the plant"),
    "copied": (REJECT, "all marks have the same length (the model copied one segment)"),
    "not_this_structure": (REJECT, "this is not the structure it was taken for"),
    "excluded": (REJECT, "excluded by the reviewer"),
    "not_found": (REJECT, "not found on the sheet"),
    # the value is real but only a minimum
    "occluded_tip": (CENSOR, "the tip is hidden, so the length is a minimum"),
    "occluded_base": (CENSOR, "the base is hidden, so the length is a minimum"),
    "cut_off": (CENSOR, "marked as cut off by the reviewer"),
    # the value is kept, a person should look
    "unusual": (REVIEW, "the value is unusual for this taxon"),
    "interpolated": (REVIEW, "part of the path is hidden and was bridged"),
    "winding": (REVIEW, "the path winds much more than the straight distance"),
    "tip_not_at_end": (REVIEW, "the plant continues beyond the marked tip"),
    "tip_inside_mask": (REVIEW, "the tip lies inside the outline instead of at its end"),
    "partly_covered": (REVIEW, "the model reports the structure as partly covered"),
    "edges_unconfirmed": (REVIEW, "the edges could not be confirmed on the image"),
    "methods_disagree": (REVIEW, "two ways of measuring this disagree"),
    "second_opinion": (REVIEW, "the second-opinion check disagrees"),
    "width_jump": (REVIEW, "the width changes suddenly along the leaf (possible fold or overlap)"),
    "low_solidity": (REVIEW, "the outline is ragged (possible damage or overlap)"),
    "odd_shape": (REVIEW, "the outline does not look like this structure"),
    "scale_uncertain": (REVIEW, "the scale is uncertain"),
    "few_seeds": (REVIEW, "few seeds could be measured"),
    "seed_spread": (REVIEW, "the seed lengths vary a lot"),
    "low_confidence": (REVIEW, "low confidence"),
    # notes
    "sheet_scale": (INFO, "scale from the sheet width"),
    "bridged_over": (INFO, "the plant lies on top of a label here"),
}
DEFAULT_SCORE = 0.7           # detector score when the detector gives none (vision-language models)
REVIEW_PENALTY = 0.75         # confidence is multiplied by this for every review flag
MIN_ACCEPT_CONFIDENCE = 0.5
SEED_SPREAD = 0.15            # robust coefficient of variation above which seed lengths "vary a lot"
SCALE_QUALITY = {"ruler": 1.0, "manual": 1.0, "sheet": 0.9, "none": 0.0}
UNCERTAIN_SCALE_QUALITY = 0.75


def severity(code):
    return FLAGS.get(code, (REVIEW, ""))[0]


def add_flag(entry, code, text=None):
    """Attach a flag to an object (once per code). Returns the entry."""
    flags = entry.setdefault("flags", [])
    if not any(f["code"] == code for f in flags):
        flags.append({"code": code, "text": text or FLAGS.get(code, (REVIEW, code))[1]})
    return entry


def has_flag(entry, code):
    return any(f["code"] == code for f in entry.get("flags", []))


def flag_codes(entry):
    return [f["code"] for f in entry.get("flags", [])]


def flag_texts(entries):
    texts = [f["text"] for e in entries for f in e.get("flags", []) if severity(f["code"]) != INFO]
    return list(dict.fromkeys(texts))


def _status_from_flags(codes):
    kinds = {severity(c) for c in codes}
    if REJECT in kinds:
        return NOT_MEASURABLE
    if CENSOR in kinds:
        return LOWER_BOUND
    if REVIEW in kinds:
        return NEEDS_REVIEW
    return ACCEPT


def scale_quality(calibration):
    """0-1: how far the scale can be trusted (calibration is the stored dict or None)."""
    if not calibration or not calibration.get("px_per_cm"):
        return 0.0
    if calibration.get("uncertain"):
        return UNCERTAIN_SCALE_QUALITY
    return SCALE_QUALITY.get(calibration.get("source"), 0.9)


def decide_entry(entry, calibration=None):
    """Set an object's status and confidence from its flags. A reviewed object is trusted as marked."""
    if entry.get("reviewed"):
        attrs = entry.get("attrs") or {}
        entry["flags"] = [f for f in entry.get("flags", []) if f["code"] in ("degenerate", "out_of_range")]
        for attr, code in (("exclude", "excluded"), ("folded", "folded"), ("cut_off", "cut_off")):
            if attrs.get(attr):
                add_flag(entry, code)
        measured = entry.get("value_cm") is not None
        entry["status"] = _status_from_flags(flag_codes(entry)) if measured else NOT_MEASURABLE
        entry["confidence"] = 1.0 if measured else 0.0
        return entry
    codes = flag_codes(entry)
    entry["status"] = _status_from_flags(codes) if entry.get("value_cm") is not None else NOT_MEASURABLE
    score = entry.get("score")
    confidence = (DEFAULT_SCORE if score is None else float(score)) * float(entry.get("quality", 1.0))
    confidence *= REVIEW_PENALTY ** sum(1 for c in codes if severity(c) == REVIEW)
    confidence *= scale_quality(calibration) if calibration is not None else 1.0
    entry["confidence"] = round(min(max(confidence, 0.0), 0.99), 2) if entry.get("value_cm") is not None else 0.0
    return entry


def _aggregate(values, how):
    if how == "max":
        return max(values)
    if how == "median":
        return statistics.median(values)
    return statistics.fmean(values)


def _spread(values):
    """Robust coefficient of variation (1.4826 x MAD / median)."""
    middle = statistics.median(values)
    if not middle:
        return 0.0
    return 1.4826 * statistics.median(abs(v - middle) for v in values) / middle


def decide_trait(trait, entries, profile, calibration=None, blocked=None, reviewed=False):
    """The sheet's value for one trait from its measured objects.

    `blocked` is (code, text) when the whole trait cannot be measured on this sheet (no scale, low
    resolution, nothing found). Clean objects are preferred: objects needing review are only used
    when no object is accepted. Returns a plain dict (see the keys below).
    """
    how = profile.aggregate[trait]
    decision = {"trait": trait, "status": NOT_MEASURABLE, "value_cm": None, "lower_bound_cm": None, "unit": "cm",
                "n": 0, "values": [], "confidence": 0.0, "method": "", "aggregate": how, "flags": [], "note": ""}
    if blocked and not any(e.get("reviewed") for e in entries):
        decision["flags"], decision["note"] = [blocked[0]], blocked[1]
        return decision
    for entry in entries:
        decide_entry(entry, calibration)

    accepted = [e for e in entries if e["status"] == ACCEPT]
    used = accepted or [e for e in entries if e["status"] == NEEDS_REVIEW]
    censored = [e for e in entries if e["status"] == LOWER_BOUND]
    if not used:
        if censored:
            best = max(censored, key=lambda e: e["value_cm"])
            decision.update(status=LOWER_BOUND, lower_bound_cm=round(best["value_cm"], 3), n=len(censored),
                            values=[e["value_cm"] for e in censored], method=best.get("method", ""),
                            confidence=round(statistics.fmean(e["confidence"] for e in censored), 2),
                            flags=list(dict.fromkeys(c for e in censored for c in flag_codes(e))),
                            note="; ".join(flag_texts(censored)))
        else:
            decision["flags"] = list(dict.fromkeys(c for e in entries for c in flag_codes(e))) or ["not_found"]
            decision["note"] = "; ".join(flag_texts(entries)) or (blocked[1] if blocked else "not found")
        return decision

    values = [e["value_cm"] for e in used]
    value = _aggregate(values, how)
    chosen = [max(used, key=lambda e: e["value_cm"])] if how == "max" else used
    flags = list(dict.fromkeys(c for e in chosen for c in flag_codes(e) if severity(c) != INFO))
    notes = flag_texts(chosen)
    confidence = statistics.fmean(e["confidence"] for e in chosen)

    def trait_flag(code, text):
        if code not in flags:
            flags.append(code)
            notes.append(text)

    human = reviewed or all(e.get("reviewed") for e in chosen)
    low, high = profile.typical_cm[trait]
    if not low <= value <= high and not human:
        trait_flag("unusual", f"{value:.2f} cm is unusual (the lab's hand measurements are mostly {low:g}-{high:g} cm)")
    if trait == "seed_length" and not human:
        if len(values) < profile.min_seeds:
            trait_flag("few_seeds", f"only {len(values)} seed{'s' if len(values) != 1 else ''} measured")
            confidence *= (len(values) / profile.min_seeds) ** 0.5
        elif _spread(values) > SEED_SPREAD:
            trait_flag("seed_spread", f"seed lengths vary a lot ({min(values):.2f}-{max(values):.2f} cm)")
    if calibration and calibration.get("uncertain") and not human:
        trait_flag("scale_uncertain", "scale is uncertain: " + "; ".join(calibration["uncertain"]))
    if confidence < MIN_ACCEPT_CONFIDENCE and not human:
        trait_flag("low_confidence", f"low confidence ({confidence:.2f})")

    methods = list(dict.fromkeys(e.get("method", "") for e in chosen if e.get("method")))
    label = {"mean": "mean", "median": "median", "max": "largest"}[how]
    method = "; ".join(methods)
    if len(used) > 1:
        method = f"{label} of {len(used)} ({method})"
    decision.update(status=_status_from_flags(flags), value_cm=round(value, 3), n=len(used),
                    values=[round(v, 3) for v in values], confidence=round(min(confidence, 1.0), 2),
                    method=method, flags=flags, note="; ".join(dict.fromkeys(notes)))
    if censored:
        decision["lower_bound_cm"] = round(max(e["value_cm"] for e in censored), 3)
    return decision


def describe_decision(decision):
    """One short phrase for logs: 'panicle 23.4 cm (accept, 0.63)'."""
    name = TRAIT_NAMES[decision["trait"]].split()[0].lower()
    if decision["value_cm"] is not None:
        return f"{name} {decision['value_cm']:.2f} cm ({decision['status']}, {decision['confidence']:.2f})"
    if decision["lower_bound_cm"] is not None:
        return f"{name} >= {decision['lower_bound_cm']:.2f} cm"
    return f"{name} - ({decision['note'] or decision['status']})"
