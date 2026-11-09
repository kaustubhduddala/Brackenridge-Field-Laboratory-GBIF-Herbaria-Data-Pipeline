"""The traits that are measured and, for each taxon, the values that are plausible.

A value outside `hard` is rejected; a value inside `hard` but outside `typical` is kept and sent to
review. Add a Profile to PROFILES to measure another taxon.
"""
from dataclasses import dataclass, field

TRAITS = ("panicle_length", "leaf_width", "seed_length")
TRAIT_LABELS = {"panicle_length": "panicle", "leaf_width": "leaf", "seed_length": "seed"}
TRAIT_NAMES = {"panicle_length": "Panicle length", "leaf_width": "Leaf width", "seed_length": "Seed length"}
# The object class each trait is measured on (also the class names the trained models use).
TRAIT_CLASS = {"panicle_length": "panicle", "leaf_width": "leaf", "seed_length": "seed"}
CLASS_TRAIT = {cls: trait for trait, cls in TRAIT_CLASS.items()}
# The two landmarks that define each measurement (the keypoints of the pose model).
LANDMARKS = {"panicle_length": ("base", "tip"), "leaf_width": ("a", "b"), "seed_length": ("base", "tip")}


@dataclass(frozen=True)
class Profile:
    name: str
    hard_cm: dict                    # outside these limits a value is rejected
    typical_cm: dict                 # outside this range a value is kept and flagged for review
    reference_cm: dict               # hand measurements outside these are data-entry errors
    # How several measured objects become the sheet's value.
    aggregate: dict = field(default_factory=lambda: {"panicle_length": "mean", "leaf_width": "max",
                                                     "seed_length": "median"})
    # Below this image resolution a trait is not measurable (1 px is then >3% of a typical value).
    min_px_per_cm: dict = field(default_factory=lambda: {"panicle_length": 10.0, "leaf_width": 30.0,
                                                         "seed_length": 60.0})
    # The measured extent of one object must span at least this many pixels.
    min_object_px: dict = field(default_factory=lambda: {"panicle_length": 60.0, "leaf_width": 12.0,
                                                         "seed_length": 18.0})
    min_seeds: int = 3               # fewer measured seeds than this sends the median to review
    names: tuple = ()                # other names the taxon goes by on labels (synonyms), for reading labels


# Megathyrsus maximus: limits from the lab's 163 hand measurements
# (01-06-2025_GBIFdownload_CFmeasurements.csv). Hand values: panicle 8.0-60.7 cm, leaf 0.38-3.79 cm,
# seed 0.25-0.61 cm; `typical` is their 5th-95th percentile.
MEGATHYRSUS_MAXIMUS = Profile(
    name="Megathyrsus maximus",
    hard_cm={"panicle_length": (6.0, 75.0), "leaf_width": (0.3, 4.5), "seed_length": (0.22, 0.75)},
    typical_cm={"panicle_length": (12.0, 48.0), "leaf_width": (0.5, 2.4), "seed_length": (0.33, 0.55)},
    reference_cm={"panicle_length": (3.0, 75.0), "leaf_width": (0.15, 5.0), "seed_length": (0.15, 0.65)},
    names=("Panicum maximum", "Urochloa maxima", "Panicum maximum var. trichoglume", "Guinea grass"),
)

PROFILES = {MEGATHYRSUS_MAXIMUS.name: MEGATHYRSUS_MAXIMUS}
DEFAULT_PROFILE = MEGATHYRSUS_MAXIMUS


def taxon_names(profile):
    """The names to look for on a label: the taxon and its synonyms."""
    return (profile.name, *profile.names)


def profile_for(name=None):
    """The profile for a taxon name; the default profile when the taxon has none of its own."""
    wanted = str(name or "").strip().lower()
    for key, profile in PROFILES.items():
        if wanted and (wanted == key.lower() or wanted.startswith(key.lower())):
            return profile
    return DEFAULT_PROFILE
