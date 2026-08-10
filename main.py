"""
GBIF Herbaria Data Pipeline
============================

Downloads GBIF occurrence data, cleans it in two phases, and
exports datasets ready for ImageJ measurements. Optionally
automates trait measurement using vision-language models.

Usage:
    python gbif_pipeline.py                  Interactive launcher (GUI or CLI)
    python gbif_pipeline.py cleaner 1 ...    Run Phase 1 directly
    python gbif_pipeline.py cleaner 2 ...    Run Phase 2 directly
    python gbif_pipeline.py measure ...      Run auto-measurement directly
"""

import io
import os
import sys
import csv
import json
import time
import re
import zipfile
import threading
import tkinter as tk
import traceback
import urllib3
import time
import copy
from urllib.parse import urljoin, urlparse
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, scrolledtext
from bs4 import BeautifulSoup

# Suppress the console warnings caused by bypassing SSL verification
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests
import pandas as pd
from pygbif import occurrences

# ---------------------------------------------------------------------------
# Optional ML dependencies — the rest of the pipeline works without them.
# ---------------------------------------------------------------------------
try:
    import torch
    import torchvision  # noqa: F401 — required by Qwen3-VL processor
    from transformers import AutoProcessor, AutoModelForImageTextToText
    from PIL import Image

    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False


# ==============================================================================
# Configuration
# ==============================================================================

GBIF_USER = "bfl_ut_austin"
GBIF_PASSWORD = "qwertyuiop123"
GBIF_EMAIL = "kaustubhduddala@utexas.edu"

DEFAULT_EXCLUDE_TAXA = [
    "Megathyrsus maximus var. coloratus (C.T.White) B.K.Simon & S.W.L.Jacobs",
    "Megathyrsus maximus var. pubiglumis (K.Schum.) B.K.Simon & S.W.L.Jacobs",
    "Panicum compressum Biv.",
    "Panicum trichoglume K.Schum.",
    "Panicum maximum var. effusum A.Camus",
    "Panicum mahafalense A.Camus",
    "Panicum maximum var. pubiglume K.Schum",
    "Panicum maximum var. trichoglume Robyns",
]

BAD_GEOSPATIAL_ISSUES = [
    "COORDINATE_ROUNDED",
    "GEODETIC_DATUM_INVALID",
    "GEODETIC_DATUM_ASSUMED_WGS84",
    "COORDINATE_PRECISION_INVALID",
    "COORDINATE_UNCERTAINTY_METERS_INVALID",
    "FOOTPRINT_INVALID",
    "FOOTPRINT_WKT_MISMATCH",
    "FOOTPRINT_SRS_INVALID",
    "CONTINENT_COORDINATE_MISMATCH",
    "COUNTRY_COORDINATE_MISMATCH",
    "CONTINENT_COUNTRY_MISMATCH",
]

INSPECTION_ISSUES = [
    "COUNTRY_MISMATCH",
    "RECORDED_DATE_MISMATCH",
    "RECORDED_DATE_INVALID",
    "RECORDED_DATE_UNLIKELY",
    "OCCURRENCE_STATUS_UNPARSABLE",
]

DOWNLOAD_ISSUE_EXCLUSIONS = [
    "COORDINATE_REPROJECTED",
    "COUNTRY_MISMATCH",
    "COUNTRY_INVALID",
    "COUNTRY_DERIVED_FROM_COORDINATES",
    "CONTINENT_INVALID",
    "CONTINENT_DERIVED_FROM_COORDINATES",
    "RECORDED_DATE_MISMATCH",
    "RECORDED_DATE_INVALID",
    "RECORDED_DATE_UNLIKELY",
    "TAXON_MATCH_FUZZY",
    "TAXON_MATCH_HIGHERRANK",
    "SCIENTIFIC_NAME_ID_NOT_FOUND",
    "TAXON_ID_NOT_FOUND",
    "TAXON_CONCEPT_ID_NOT_FOUND",
    "ELEVATION_MIN_MAX_SWAPPED",
    "ELEVATION_NON_NUMERIC",
    "MODIFIED_DATE_INVALID",
    "IDENTIFIED_DATE_INVALID",
    "TYPE_STATUS_INVALID",
    "MULTIMEDIA_DATE_INVALID",
    "MULTIMEDIA_URI_INVALID",
    "REFERENCES_URI_INVALID",
    "INDIVIDUAL_COUNT_CONFLICTS_WITH_OCCURRENCE_STATUS",
    "OCCURRENCE_STATUS_UNPARSABLE",
    "OCCURRENCE_STATUS_INFERRED_FROM_INDIVIDUAL_COUNT",
    "AMBIGUOUS_INSTITUTION",
    "AMBIGUOUS_COLLECTION",
    "INSTITUTION_MATCH_NONE",
    "COLLECTION_MATCH_NONE",
    "INSTITUTION_MATCH_FUZZY",
    "COLLECTION_MATCH_FUZZY",
    "INSTITUTION_COLLECTION_MISMATCH",
]

ALLOWED_LICENSES = [
    "CC0_1_0",
    "CC_BY_4_0",
    "CC_BY_NC_4_0",
]

ISSUE_DESCRIPTIONS = {
    "COORDINATE_ROUNDED":
        "Coordinates were rounded; original precision lost.",
    "GEODETIC_DATUM_INVALID":
        "Geodetic datum is unrecognized or invalid.",
    "GEODETIC_DATUM_ASSUMED_WGS84":
        "No datum provided; GBIF assumed WGS84.",
    "COORDINATE_PRECISION_INVALID":
        "Stated coordinate precision value is invalid.",
    "COORDINATE_UNCERTAINTY_METERS_INVALID":
        "Coordinate uncertainty in metres is invalid.",
    "FOOTPRINT_INVALID":
        "Spatial footprint geometry is invalid.",
    "FOOTPRINT_WKT_MISMATCH":
        "Footprint WKT doesn't match the coordinates.",
    "FOOTPRINT_SRS_INVALID":
        "Footprint spatial reference system is invalid.",
    "CONTINENT_COORDINATE_MISMATCH":
        "Coordinates fall outside the stated continent.",
    "COUNTRY_COORDINATE_MISMATCH":
        "Coordinates fall outside the stated country.",
    "CONTINENT_COUNTRY_MISMATCH":
        "Stated continent and country contradict each other.",
    "COORDINATE_REPROJECTED":
        "Coordinates were reprojected successfully to WGS84.",
    "COUNTRY_MISMATCH":
        "Interpreted country and country code contradict each other.",
    "COUNTRY_INVALID":
        "Country code is unrecognized or doesn't match GBIF's list.",
    "COUNTRY_DERIVED_FROM_COORDINATES":
        "Country was inferred from coordinates, not provided.",
    "CONTINENT_INVALID":
        "Continent value is unrecognized by GBIF.",
    "CONTINENT_DERIVED_FROM_COORDINATES":
        "Continent was inferred from coordinates, not provided.",
    "CONTINENT_DERIVED_FROM_COUNTRY":
        "Continent was inferred from the country, not provided directly.",
    "RECORDED_DATE_MISMATCH":
        "Date components (year/month/day) are internally inconsistent.",
    "RECORDED_DATE_INVALID":
        "Date is unparseable, in the future, or impossible.",
    "RECORDED_DATE_UNLIKELY":
        "Date is before Linnean taxonomy or otherwise suspect.",
    "TAXON_MATCH_FUZZY":
        "Scientific name matched via fuzzy/approximate lookup.",
    "TAXON_MATCH_HIGHERRANK":
        "Name matched only at a higher taxonomic rank.",
    "SCIENTIFIC_NAME_ID_NOT_FOUND":
        "Supplied scientific name ID could not be resolved.",
    "TAXON_CONCEPT_ID_NOT_FOUND":
        "Taxon concept ID not found in GBIF backbone.",
    "TAXON_ID_NOT_FOUND":
        "Taxon ID not found in GBIF backbone.",
    "ELEVATION_MIN_MAX_SWAPPED":
        "Minimum elevation is greater than maximum (likely swapped).",
    "ELEVATION_NON_NUMERIC":
        "Elevation value contains text or symbols instead of a number.",
    "MODIFIED_DATE_INVALID":
        "Record modification date is invalid or unparseable.",
    "MODIFIED_DATE_UNLIKELY":
        "Record modification date is suspiciously old or in the future.",
    "IDENTIFIED_DATE_INVALID":
        "Identification date is invalid or unparseable.",
    "TYPE_STATUS_INVALID":
        "Specimen type status has a typo or isn't in GBIF's list.",
    "SUSPECTED_TYPE":
        "Record is suspected of being a type specimen but status is uncertain.",
    "MULTIMEDIA_DATE_INVALID":
        "Multimedia date is invalid, in the future, or unparseable.",
    "MULTIMEDIA_URI_INVALID":
        "Multimedia URI is malformed; voucher image may be inaccessible.",
    "REFERENCES_URI_INVALID":
        "Reference URI is malformed or contains invalid characters.",
    "INDIVIDUAL_COUNT_CONFLICTS_WITH_OCCURRENCE_STATUS":
        "Count is 0 but status is 'Present', or vice versa.",
    "OCCURRENCE_STATUS_UNPARSABLE":
        "Status is something other than 'Present' or 'Absent'.",
    "OCCURRENCE_STATUS_INFERRED_FROM_INDIVIDUAL_COUNT":
        "No status provided; inferred from individual count.",
    "PRESUMED_NEGATED_LATITUDE":
        "Latitude sign appears to be wrong (likely negated).",
    "PRESUMED_NEGATED_LONGITUDE":
        "Longitude sign appears to be wrong (likely negated).",
    "AMBIGUOUS_INSTITUTION":
        "Institution code matches multiple entries in GRSciColl.",
    "AMBIGUOUS_COLLECTION":
        "Collection code matches multiple entries in GRSciColl.",
    "INSTITUTION_MATCH_NONE":
        "No matching institution found in GRSciColl.",
    "COLLECTION_MATCH_NONE":
        "No matching collection found in GRSciColl.",
    "INSTITUTION_MATCH_FUZZY":
        "Institution matched approximately, not exactly.",
    "COLLECTION_MATCH_FUZZY":
        "Collection matched approximately, not exactly.",
    "INSTITUTION_COLLECTION_MISMATCH":
        "Collection code doesn't belong to the stated institution.",
}

LINK_COLUMNS = [
    "identifier",
    "references_multimedia",
    "bibliographicCitation",
    "references",
    "associatedReferences",
    "occurrenceID",
]

PRECISION_NONE = "none"
PRECISION_RELAXED = "relaxed"
PRECISION_STRICT = "strict"

PRESETS = {
    "G064 (Default)": {
        "species": "Megathyrsus maximus",
        "precision": PRECISION_STRICT,
        "exclude_taxa": list(DEFAULT_EXCLUDE_TAXA),
        "bad_issues": True,
        "inspect_issues": True,
        "download_predicate": {
            "type": "and",
            "predicates": [
                {
                    "type": "equals",
                    "key": "TAXON_KEY",
                    "value": "4108550",
                    "matchCase": False,
                },
                {
                    "type": "equals",
                    "key": "BASIS_OF_RECORD",
                    "value": "PRESERVED_SPECIMEN",
                    "matchCase": False,
                },
                {
                    "type": "equals",
                    "key": "HAS_COORDINATE",
                    "value": "True",
                    "matchCase": False,
                },
                {
                    "type": "equals",
                    "key": "HAS_GEOSPATIAL_ISSUE",
                    "value": "false",
                    "matchCase": False,
                },
                {
                    "type": "equals",
                    "key": "OCCURRENCE_STATUS",
                    "value": "PRESENT",
                    "matchCase": False,
                },
            ],
        },
    },
    "G024": { 
        "species": "Megathyrsus maximus",
        "precision": PRECISION_NONE,
        "exclude_taxa": [],
        "bad_issues": [],
        "inspect_issues": True,
        "download_predicate": {
            "type": "and",
            "predicates": [
                {"type": "equals", "key": "TAXON_KEY", "value": "__TAXON_KEY__"},
                {"type": "equals", "key": "BASIS_OF_RECORD", "value": "PRESERVED_SPECIMEN"},
                {"type": "equals", "key": "HAS_COORDINATE", "value": True},
                {"type": "equals", "key": "HAS_GEOSPATIAL_ISSUE", "value": False},
                {"type": "in", "key": "ISSUE", "values": DOWNLOAD_ISSUE_EXCLUSIONS},
                {"type": "in", "key": "LICENSE", "values": ALLOWED_LICENSES},
                {"type": "equals", "key": "OCCURRENCE_STATUS", "value": "PRESENT"},
            ],
        },
    },
    "Custom": {
        "species": "",
        "precision": PRECISION_RELAXED,
        "exclude_taxa": [],
        "bad_issues": True,
        "inspect_issues": True,
    },
}

# ---------------------------------------------------------------------------
# ML / Model configuration
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".gbif_pipeline"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Models are downloaded here, next to the script, instead of the
# default HuggingFace cache (~/.cache/huggingface/).  Change this
# to any absolute or relative path you prefer.
MODELS_DIR = Path(__file__).resolve().parent / "models"

# Voucher images are downloaded here, named by catalog number.
MEDIA_DIR = Path(__file__).resolve().parent / "media"

DEFAULT_MODEL_ID = "Qwen/Qwen3-VL-30B-A3B-Instruct"

# Columns checked (in priority order) for an image URL when measuring.
IMAGE_URL_COLUMNS = [
    "identifier",
    "references_multimedia",
    "references",
    "bibliographicCitation",
    "associatedReferences",
    "occurrenceID",
]

MEASUREMENT_PROMPT = """\
You are a botanical measurement assistant analyzing a herbarium specimen \
image of Guinea grass (Megathyrsus maximus).

CALIBRATION:
- Look for a ruler or scale bar in the image.  If found, use it to \
determine the pixel-to-centimetre ratio.
- If no scale bar is visible but the full herbarium sheet is visible, \
assume the standard sheet width is 29 cm.
- If neither a scale bar nor the full sheet is visible, set every \
measurement to null.

MEASUREMENTS (centimetres):
1. panicle_length_cm  -- from the lowest panicle node to the tip of \
the panicle.
2. leaf_width_cm      -- widest leaf, margin to margin at the broadest \
point.
3. seed_length_cm     -- from the branch point of an individual seed \
to its tip.

RULES:
- If a structure is not visible, obscured, or unmeasurable, use null.
- If the specimen has flowers instead of seeds, use null for seed_length_cm.
- Round to one decimal place.
- Respond with ONLY a JSON object -- no markdown, no commentary.

Required format:
{"panicle_length_cm": <number|null>, "leaf_width_cm": <number|null>, \
"seed_length_cm": <number|null>, "notes": "<brief note>"}
"""


# ==============================================================================
# GBIF Download Helpers
# ==============================================================================

def resolve_species(scientific_name):
    """Look up a species name and return its GBIF taxonKey."""
    print(f"Looking up GBIF taxonomic key for '{scientific_name}'...")
    resp = requests.get(
        "https://api.gbif.org/v1/species/match",
        params={"name": scientific_name},
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("matchType") in ("EXACT", "FUZZY") and "usageKey" in result:
        key = result["usageKey"]
        official = result.get("scientificName", scientific_name)
        print(f"Found '{official}' -- Taxon Key: {key}")
        return key
    raise ValueError(
        f"Could not find a reliable taxonomic match for '{scientific_name}'.\n"
        f"GBIF response: {result}"
    )


def trigger_download(queries, download_format, user, password, email):
    """Submit a download request to GBIF and return the download key."""
    print("Submitting download request to GBIF...")
    result = occurrences.download(
        queries, format=download_format, user=user, pwd=password, email=email
    )
    return result[0] if isinstance(result, tuple) else result


def wait_and_download(download_key, output_dir="."):
    """Poll GBIF until the download is ready, then fetch the ZIP."""
    meta = occurrences.download_meta(download_key)
    while meta["status"] in ("RUNNING", "PREPARING"):
        print("Waiting for GBIF to generate the file...")
        time.sleep(30)
        meta = occurrences.download_meta(download_key)
    if meta["status"] == "SUCCEEDED":
        print("File is ready -- downloading ZIP...")
        occurrences.download_get(download_key, path=output_dir)
        return f"{output_dir}/{download_key}.zip"
    raise RuntimeError(f"Download failed with status: {meta['status']}")


def extract_archive(zip_path, extract_dir):
    """Extract a Darwin Core Archive ZIP."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)
    print(f"Data extracted to {extract_dir}")
    return extract_dir


def _build_gbif_queries(taxon_key, preset_name=None):
    # Use a raw GBIF download predicate dict and align it with the
    # checklist-based filters in the API documentation.
    if preset_name and preset_name in PRESETS:
        preset = PRESETS[preset_name]
        download_predicate = preset.get("download_predicate")
        if download_predicate:
            predicate = copy.deepcopy(download_predicate)
            for clause in predicate.get("predicates", []):
                if (clause.get("key") == "TAXON_KEY" and
                        clause.get("value") == "__TAXON_KEY__"):
                    clause["value"] = str(taxon_key)
            return predicate
    return {
        "type": "and",
        "predicates": [
            {"type": "equals", "key": "TAXON_KEY", "value": str(taxon_key)},
            {"type": "equals", "key": "BASIS_OF_RECORD", "value": "PRESERVED_SPECIMEN"},
            {"type": "equals", "key": "HAS_COORDINATE", "value": True},
            {"type": "equals", "key": "HAS_GEOSPATIAL_ISSUE", "value": False},
            {"type": "in", "key": "ISSUE", "values": DOWNLOAD_ISSUE_EXCLUSIONS},
            {"type": "in", "key": "LICENSE", "values": ALLOWED_LICENSES},
            {"type": "equals", "key": "OCCURRENCE_STATUS", "value": "PRESENT"},
        ],
    }


# ==============================================================================
# Coordinate Cleaning (equivalent to R CoordinateCleaner)
# ==============================================================================

# Country centroids — records sitting exactly on a country centroid
# are almost always default/placeholder coordinates, not real locations.
# Source: approximate centroids for countries with GBIF occurrence data.
_COUNTRY_CENTROIDS = {
    "AD": (42.55, 1.58), "AE": (24.00, 54.00), "AF": (33.00, 65.00),
    "AG": (17.05, -61.80), "AL": (41.00, 20.00), "AM": (40.00, 45.00),
    "AO": (-12.50, 18.50), "AR": (-34.00, -64.00), "AT": (47.33, 13.33),
    "AU": (-25.00, 135.00), "AZ": (40.50, 47.50), "BA": (44.00, 18.00),
    "BB": (13.17, -59.53), "BD": (24.00, 90.00), "BE": (50.83, 4.00),
    "BF": (13.00, -2.00), "BG": (43.00, 25.00), "BH": (26.00, 50.55),
    "BI": (-3.50, 30.00), "BJ": (9.50, 2.25), "BN": (4.50, 114.67),
    "BO": (-17.00, -65.00), "BR": (-10.00, -55.00), "BS": (24.25, -76.00),
    "BT": (27.50, 90.50), "BW": (-22.00, 24.00), "BY": (53.00, 28.00),
    "BZ": (17.25, -88.75), "CA": (60.00, -96.00), "CD": (-2.50, 23.50),
    "CF": (7.00, 21.00), "CG": (-1.00, 15.00), "CH": (47.00, 8.00),
    "CI": (8.00, -5.00), "CL": (-30.00, -71.00), "CM": (6.00, 12.00),
    "CN": (35.00, 105.00), "CO": (4.00, -72.00), "CR": (10.00, -84.00),
    "CU": (22.00, -79.50), "CV": (16.00, -24.00), "CY": (35.00, 33.00),
    "CZ": (49.75, 15.50), "DE": (51.00, 9.00), "DJ": (11.50, 43.00),
    "DK": (56.00, 10.00), "DM": (15.42, -61.33), "DO": (19.00, -70.67),
    "DZ": (28.00, 3.00), "EC": (-2.00, -77.50), "EE": (59.00, 26.00),
    "EG": (27.00, 30.00), "ER": (15.00, 39.00), "ES": (40.00, -4.00),
    "ET": (8.00, 38.00), "FI": (64.00, 26.00), "FJ": (-18.00, 178.00),
    "FR": (46.00, 2.00), "GA": (-1.00, 11.75), "GB": (54.00, -2.00),
    "GD": (12.12, -61.67), "GE": (42.00, 43.50), "GH": (8.00, -2.00),
    "GM": (13.47, -16.57), "GN": (11.00, -10.00), "GQ": (2.00, 10.00),
    "GR": (39.00, 22.00), "GT": (15.50, -90.25), "GW": (12.00, -15.00),
    "GY": (5.00, -59.00), "HN": (15.00, -86.50), "HR": (45.17, 15.50),
    "HT": (19.00, -72.42), "HU": (47.00, 20.00), "ID": (-5.00, 120.00),
    "IE": (53.00, -8.00), "IL": (31.50, 34.75), "IN": (20.00, 77.00),
    "IQ": (33.00, 44.00), "IR": (32.00, 53.00), "IS": (65.00, -18.00),
    "IT": (42.83, 12.83), "JM": (18.25, -77.50), "JO": (31.00, 36.00),
    "JP": (36.00, 138.00), "KE": (1.00, 38.00), "KG": (41.00, 75.00),
    "KH": (13.00, 105.00), "KM": (-12.17, 44.25), "KN": (17.33, -62.75),
    "KR": (37.00, 127.50), "KW": (29.50, 47.75), "KZ": (48.00, 68.00),
    "LA": (18.00, 105.00), "LB": (33.83, 35.83), "LC": (13.88, -60.97),
    "LI": (47.17, 9.53), "LK": (7.00, 81.00), "LR": (6.50, -9.50),
    "LS": (-29.50, 28.50), "LT": (56.00, 24.00), "LU": (49.75, 6.17),
    "LV": (57.00, 25.00), "LY": (25.00, 17.00), "MA": (32.00, -5.00),
    "MC": (43.73, 7.40), "MD": (47.00, 29.00), "ME": (42.50, 19.30),
    "MG": (-20.00, 47.00), "MK": (41.83, 22.00), "ML": (17.00, -4.00),
    "MM": (22.00, 98.00), "MN": (46.00, 105.00), "MR": (20.00, -12.00),
    "MT": (35.83, 14.58), "MU": (-20.28, 57.55), "MV": (3.25, 73.00),
    "MW": (-13.50, 34.00), "MX": (23.00, -102.00), "MY": (2.50, 112.50),
    "MZ": (-18.25, 35.00), "NA": (-22.00, 17.00), "NE": (16.00, 8.00),
    "NG": (10.00, 8.00), "NI": (13.00, -85.00), "NL": (52.50, 5.75),
    "NO": (62.00, 10.00), "NP": (28.00, 84.00), "NZ": (-42.00, 174.00),
    "OM": (21.00, 57.00), "PA": (9.00, -80.00), "PE": (-10.00, -76.00),
    "PG": (-6.00, 147.00), "PH": (13.00, 122.00), "PK": (30.00, 70.00),
    "PL": (52.00, 20.00), "PT": (39.50, -8.00), "PY": (-23.00, -58.00),
    "QA": (25.50, 51.25), "RO": (46.00, 25.00), "RS": (44.00, 21.00),
    "RU": (60.00, 100.00), "RW": (-2.00, 29.50), "SA": (25.00, 45.00),
    "SB": (-8.00, 159.00), "SC": (-4.58, 55.67), "SD": (16.00, 30.00),
    "SE": (62.00, 15.00), "SG": (1.37, 103.80), "SI": (46.12, 14.82),
    "SK": (48.67, 19.50), "SL": (8.50, -11.50), "SN": (14.00, -14.00),
    "SO": (10.00, 49.00), "SR": (4.00, -56.00), "SS": (7.00, 30.00),
    "SV": (13.83, -88.92), "SY": (35.00, 38.00), "SZ": (-26.50, 31.50),
    "TD": (15.00, 19.00), "TG": (8.00, 1.17), "TH": (15.00, 100.00),
    "TJ": (39.00, 71.00), "TL": (-8.83, 125.75), "TM": (40.00, 60.00),
    "TN": (34.00, 9.00), "TO": (-20.00, -175.00), "TR": (39.00, 35.00),
    "TT": (11.00, -61.00), "TW": (24.00, 121.00), "TZ": (-6.00, 35.00),
    "UA": (49.00, 32.00), "UG": (1.00, 32.00), "US": (38.00, -97.00),
    "UY": (-33.00, -56.00), "UZ": (41.00, 64.00), "VC": (13.25, -61.20),
    "VE": (8.00, -66.00), "VN": (16.00, 106.00), "VU": (-16.00, 167.00),
    "WS": (-13.58, -172.33), "YE": (15.00, 48.00), "ZA": (-29.00, 24.00),
    "ZM": (-15.00, 30.00), "ZW": (-20.00, 30.00),
}

# Major biodiversity institutions — specimens collected within ~500m
# of these are likely cultivated (botanical gardens, zoo grounds, etc.).
_INSTITUTIONS = [
    (-22.9711, -43.2265),  # Rio de Janeiro Botanical Garden
    (51.4775, -0.2953),    # Kew Gardens
    (41.8658, -87.6167),   # Field Museum / Chicago Botanic
    (40.7829, -73.9654),   # NYBG area
    (38.9072, -77.0369),   # Smithsonian NMNH
    (48.8441, 2.3620),     # MNHN Paris
    (52.4559, 13.3089),    # Berlin Botanical Garden
    (-33.8688, 151.2093),  # Royal Botanic Garden Sydney
    (1.3138, 103.8159),    # Singapore Botanic Gardens
    (-6.6000, 106.7994),   # Bogor Botanical Garden
    (18.9211, -99.2357),   # UNAM Botanical Garden
    (-15.8711, -47.8825),  # Brasilia Botanical Garden
    (-25.4428, -49.2375),  # Curitiba Botanical Garden
    (-22.4667, -42.9833),  # Herbarium Friburguense
    (51.4667, -0.3000),    # London (broader Kew area)
    (39.9526, -75.1652),   # Academy of Natural Sciences Philadelphia
    (48.1639, 11.5028),    # Munich Botanical Garden
    (47.5580, 7.5839),     # Basel Botanical Garden
    (52.3728, 4.9083),     # Leiden Naturalis
    (46.2276, 6.1464),     # CJBG Geneva
]


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two points."""
    import math
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _is_sea(lat, lon):
    """Quick heuristic: is this point likely in the ocean?

    Uses a simplified land-mask approach: known land bounding boxes
    for major continents.  Points that fall outside ALL of them are
    flagged.  This is intentionally conservative — it won't catch
    coastal misplacements but will catch obvious ocean points (0,0 etc.).
    """
    # Rough continental bounding boxes [lat_min, lat_max, lon_min, lon_max]
    _LAND = [
        (-56, 13, -82, -34),    # South America
        (7, 84, -170, -52),     # North America
        (-35, 38, -18, 52),     # Africa
        (-11, 72, -12, 45),     # Europe
        (-12, 75, 25, 180),     # Asia + Middle East
        (-48, -10, 112, 155),   # Australia
        (-47, -34, 165, 179),   # New Zealand
        (0, 22, 95, 142),       # SE Asia islands (broad)
        (-12, 6, 94, 141),      # Indonesia
        (4, 21, 117, 127),      # Philippines
        (-8, -1, 29, 41),       # East Africa lakes region
    ]
    for lat_min, lat_max, lon_min, lon_max in _LAND:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return False
    return True


def clean_coordinates(df, lat_col="decimalLatitude", lon_col="decimalLongitude",
                      country_col="countryCode",
                      tests=("centroids", "seas", "institutions"),
                      centroid_radius_km=1.0, institution_radius_km=0.5,
                      log=print):
    """Remove records with suspicious coordinates.

    Equivalent to R's CoordinateCleaner::clean_coordinates() with
    the three tests used in the protocol:

      centroids    — within centroid_radius_km of a country centroid
      seas         — in the ocean (heuristic land mask)
      institutions — within institution_radius_km of a known
                     biodiversity institution (botanical garden, museum)

    Returns (cleaned_df, removed_reasons) where removed_reasons is a
    Series indexed like df with the reason string for flagged rows
    (NaN for rows that passed).
    """
    log("   Running coordinate cleaning tests...")
    reasons = pd.Series("", index=df.index, dtype=str)

    lat = pd.to_numeric(df[lat_col], errors="coerce")
    lon = pd.to_numeric(df[lon_col], errors="coerce")
    cc = df[country_col].astype(str).str.strip().str.upper() if country_col in df.columns else None

    if "centroids" in tests:
        for idx in df.index:
            la, lo = lat.get(idx), lon.get(idx)
            if pd.isna(la) or pd.isna(lo):
                continue
            code = cc.get(idx) if cc is not None else None
            if code and code in _COUNTRY_CENTROIDS:
                clat, clon = _COUNTRY_CENTROIDS[code]
                if _haversine_km(la, lo, clat, clon) < centroid_radius_km:
                    reasons.at[idx] = f"Country centroid ({code})"
        n = (reasons != "").sum()
        log(f"     centroids: {n} records on country centroids")

    if "seas" in tests:
        for idx in df.index:
            if reasons.at[idx]:
                continue
            la, lo = lat.get(idx), lon.get(idx)
            if pd.isna(la) or pd.isna(lo):
                continue
            if _is_sea(la, lo):
                reasons.at[idx] = "Coordinates in ocean"
        n = (reasons.str.contains("ocean", na=False)).sum()
        log(f"     seas: {n} records in the ocean")

    if "institutions" in tests:
        for idx in df.index:
            if reasons.at[idx]:
                continue
            la, lo = lat.get(idx), lon.get(idx)
            if pd.isna(la) or pd.isna(lo):
                continue
            for ilat, ilon in _INSTITUTIONS:
                if _haversine_km(la, lo, ilat, ilon) < institution_radius_km:
                    reasons.at[idx] = "Near biodiversity institution"
                    break
        n = (reasons.str.contains("institution", na=False)).sum()
        log(f"     institutions: {n} records near biodiversity institutions")

    flagged = reasons != ""
    total = flagged.sum()
    log(f"     total flagged: {total}")
    return df[~flagged], reasons[flagged]


# ==============================================================================
# Phase 1 -- Initial Cleaning & Merging
# ==============================================================================

def phase_1_clean_and_merge(
    occurrence_file,
    multimedia_file,
    output_csv="GBIFdownload_inspectFlags.csv",
    removed_csv="GBIFdownload_removed.csv",
    exclude_taxa=None,
    coordinate_precision=PRECISION_RELAXED,
    bad_geospatial_issues=None,
    inspection_issues=None,
):
    if exclude_taxa is None:
        exclude_taxa = DEFAULT_EXCLUDE_TAXA
    if bad_geospatial_issues is None:
        bad_geospatial_issues = BAD_GEOSPATIAL_ISSUES
    if inspection_issues is None:
        inspection_issues = INSPECTION_ISSUES

    # Collects (dataframe_slice, reason_string) tuples for every step
    removed_parts = []

    def _remove(df, mask, reason):
        """Split df by mask, stash the removed rows, return the kept rows."""
        cut = df[mask].copy()
        if len(cut):
            cut.insert(cut.columns.get_loc("gbifID") + 1, "removal_reason", reason)
            removed_parts.append(cut)
        return df[~mask]

    print("=" * 60)
    print("PHASE 1: INITIAL DATA CLEANING AND MERGING")
    print("=" * 60 + "\n")

    print("1. Loading data files...")
    gbif_db = pd.read_csv(occurrence_file, sep="\t", low_memory=False,
                          quoting=csv.QUOTE_NONE, dtype=str)
    print(f"   Loaded occurrence.txt: {len(gbif_db)} rows")
    media_db = pd.read_csv(multimedia_file, sep="\t", low_memory=False,
                           quoting=csv.QUOTE_NONE, dtype=str)
    print(f"   Loaded multimedia.txt: {len(media_db)} rows")
    media_db = media_db.rename(columns={
        "references": "references_multimedia",
        "publisher": "publisher_multimedia", "license": "license_multimedia",
        "rightsHolder": "rightsHolder_multimedia",
    })

    print("\n2. Merging multimedia and occurrence data...")
    gbif_db = gbif_db.dropna(axis=1, how="all")
    media_db = media_db.dropna(axis=1, how="all")
    master_df = pd.merge(
        media_db,
        gbif_db,
        on="gbifID",
        how="inner",
        suffixes=("_multimedia", "_occurrence"),
    )
    print(f"   Merged dataset: {len(master_df)} rows")

    # -- Step 3: media filter --
    print("\n3. Keeping only rows with associated media...")
    before = len(master_df)
    media_evidence_cols = [
        c for c in (
            "mediaType", "type_multimedia", "type",
            "identifier", "format", "references_multimedia",
        )
        if c in master_df.columns
    ]
    if media_evidence_cols:
        has_any_media = pd.Series(False, index=master_df.index)
        for col_name in media_evidence_cols:
            non_empty = (
                master_df[col_name].notna()
                & master_df[col_name].astype(str).str.strip().ne("")
            )
            has_any_media = has_any_media | non_empty
        master_df = _remove(master_df, ~has_any_media, "No media evidence")
    print(f"   Rows remaining: {len(master_df)} (removed {before - len(master_df)})")

    # -- Step 4: coordinate precision --
    print("\n4. Filtering by coordinate precision...")
    before = len(master_df)
    if coordinate_precision == PRECISION_NONE:
        print("   Mode: none (no coordinate filtering)")
    else:
        pattern = r"\.\d{3,}" if coordinate_precision == PRECISION_STRICT else r"\.\d+"
        label = ">=3 decimal places" if coordinate_precision == PRECISION_STRICT else ">=1 decimal place"
        print(f"   Mode: {label}")
        low_precision = ~(
            master_df["decimalLatitude"].astype(str).str.contains(pattern, na=False, regex=True)
            & master_df["decimalLongitude"].astype(str).str.contains(pattern, na=False, regex=True)
        )
        master_df = _remove(master_df, low_precision,
                            f"Coordinate precision below threshold ({label})")
    print(f"   Rows remaining: {len(master_df)} (removed {before - len(master_df)})")

    # -- Step 5: excluded taxa --
    print("\n5. Removing excluded taxa...")
    before = len(master_df)
    if exclude_taxa:
        for col in ("scientificName", "infraspecificEpithet", "verbatimScientificName"):
            if col in master_df.columns:
                is_excluded = master_df[col].isin(exclude_taxa)
                if is_excluded.any():
                    cut = master_df[is_excluded].copy()
                    cut.insert(
                        cut.columns.get_loc("gbifID") + 1,
                        "removal_reason",
                        "Excluded taxon: " + cut[col].astype(str),
                    )
                    removed_parts.append(cut)
                    master_df = master_df[~is_excluded]
    print(f"   Rows remaining: {len(master_df)} (removed {before - len(master_df)})")

    # -- Step 6: clean coordinates --
    print("\n6. Cleaning coordinates (centroids, seas, institutions)...")
    before = len(master_df)
    pre_clean = master_df  # snapshot before cleaning
    master_df, coord_reasons = clean_coordinates(master_df, log=print)
    if len(coord_reasons):
        cut = pre_clean.loc[coord_reasons.index].copy()
        cut.insert(cut.columns.get_loc("gbifID") + 1, "removal_reason",
                   coord_reasons)
        removed_parts.append(cut)
    print(f"   Rows remaining: {len(master_df)} (removed {before - len(master_df)})")

    # -- Step 7: bad geospatial issues --
    print(f"\n7. Removing bad geospatial issues ({len(bad_geospatial_issues)} active)...")
    before = len(master_df)
    if bad_geospatial_issues:
        bad_pat = "|".join(bad_geospatial_issues)
        has_bad = (
            master_df["issue"].notna()
            & master_df["issue"].astype(str).str.contains(bad_pat, na=False, regex=True)
        )
        # Build a per-row reason listing the specific bad issues found
        def _match_issues(issue_str):
            if pd.isna(issue_str):
                return ""
            found = [i for i in bad_geospatial_issues if i in str(issue_str)]
            return "Bad geospatial issue: " + ", ".join(found)
        # Only compute for flagged rows to save time
        reasons_series = master_df.loc[has_bad, "issue"].apply(_match_issues)
        cut = master_df[has_bad].copy()
        cut.insert(cut.columns.get_loc("gbifID") + 1, "removal_reason",
                   reasons_series)
        removed_parts.append(cut)
        master_df = master_df[~has_bad]
    print(f"   Rows remaining: {len(master_df)} (removed {before - len(master_df)})")

    # -- Step 8: reference link validation --
    print("\n8. Validating reference links...")
    before = len(master_df)
    existing = [c for c in LINK_COLUMNS if c in master_df.columns]
    if existing:
        for c in existing:
            master_df[c] = master_df[c].replace(r"^\s*$", pd.NA, regex=True)
        all_blank = master_df[existing].isna().all(axis=1)
        master_df = _remove(master_df, all_blank,
                            "No valid reference links in any link column")
    print(f"   Rows remaining: {len(master_df)} (removed {before - len(master_df)})")

    # -- Step 9: inspection flags --
    print(f"\n9. Flagging records for manual inspection ({len(inspection_issues)} active)...")
    if inspection_issues:
        inspect_pat = "|".join(inspection_issues)
        master_df["inspect_flag"] = (
            master_df["issue"].notna()
            & master_df["issue"].astype(str).str.contains(inspect_pat, na=False, regex=True)
        )
    else:
        master_df["inspect_flag"] = False
    print(f"   Flagged: {master_df['inspect_flag'].sum()}")

    # -- Step 10: export --
    print("\n10. Exporting...")
    master_df = master_df.dropna(axis=1, how="all")
    master_df.to_csv(output_csv, index=False, quoting=csv.QUOTE_ALL)
    print(f"   Saved to: {output_csv}")

    # Build and export removed-records CSV
    if removed_parts:
        all_removed = pd.concat(removed_parts, ignore_index=True)
        # Ensure removal_reason is the second column (right after gbifID)
        if "removal_reason" in all_removed.columns:
            cols = list(all_removed.columns)
            cols.remove("removal_reason")
            gbif_pos = cols.index("gbifID") + 1 if "gbifID" in cols else 0
            cols.insert(gbif_pos, "removal_reason")
            all_removed = all_removed[cols]
        all_removed = all_removed.dropna(axis=1, how="all")
        all_removed.to_csv(removed_csv, index=False, quoting=csv.QUOTE_ALL)
        print(f"   Removed records: {removed_csv} ({len(all_removed)} rows)")
        # Summary by reason
        reason_counts = all_removed["removal_reason"].apply(
            lambda r: r.split(":")[0] if isinstance(r, str) else r
        ).value_counts()
        print(f"\n   Removal summary:")
        for reason, count in reason_counts.items():
            print(f"     {reason}: {count}")
    else:
        print("   No records were removed.")

    print(f"\n{'=' * 60}")
    print(f"PHASE 1 COMPLETE -- {len(master_df)} rows ready for manual inspection")
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Open the CSV in Excel")
    print("  2. Sort by 'inspect_flag' to review flagged records")
    print("  3. Put 'Remove' in the 'Action' column for bad rows")
    print("  4. Run Phase 2 when done\n")
    return output_csv


# ==============================================================================
# Phase 2 -- Deduplication & Finalization
# ==============================================================================

def phase_2_finalize_dataset(
    inspected_csv,
    final_master="master_cleaned.csv",
    final_duplicates="removed_duplicates.csv",
):
    print("=" * 60)
    print("PHASE 2: FINALIZATION AND DEDUPLICATION")
    print("=" * 60 + "\n")

    print(f"1. Loading: {inspected_csv}")
    try:
        master_df = pd.read_csv(inspected_csv, keep_default_na=True, low_memory=False)
    except pd.errors.ParserError:
        print("   Warning: standard parser failed, retrying with Python engine...")
        master_df = pd.read_csv(inspected_csv, keep_default_na=True,
                                engine="python", on_bad_lines="warn")
    print(f"   Loaded: {len(master_df)} rows")

    print("\n2. Removing records marked for deletion...")
    before = len(master_df)
    if "Action" in master_df.columns:
        master_df = master_df[
            master_df["Action"].isna() | (master_df["Action"] != "Remove")
        ]
        print(f"   Rows remaining: {len(master_df)} (removed {before - len(master_df)})")
    else:
        print("   No 'Action' column found -- skipping")

    print("\n3. Deduplicating by coordinate...")
    master_nd = master_df.drop_duplicates(
        subset=["decimalLatitude", "decimalLongitude"], keep="first"
    )
    removed = master_df[
        master_df.duplicated(subset=["decimalLatitude", "decimalLongitude"], keep="first")
    ].copy()
    print(f"   Master: {len(master_nd)} rows  |  Duplicates removed: {len(removed)}")

    print("\n4. Adding ImageJ measurement columns...")
    for col in ("Panicle length (cm)", "Leaf width (cm)", "Seed length (cm)"):
        if col not in master_nd.columns:
            master_nd[col] = ""
            print(f"   Added: {col}")

    print("\n5. Exporting final datasets...")
    master_nd = master_nd.dropna(axis=1, how="all")
    removed = removed.dropna(axis=1, how="all")
    master_nd.to_csv(final_master, index=False, quoting=csv.QUOTE_ALL)
    removed.to_csv(final_duplicates, index=False, quoting=csv.QUOTE_ALL)
    print(f"   Master:     {final_master}")
    print(f"   Duplicates: {final_duplicates}")
    print(f"\n{'=' * 60}")
    print(f"PHASE 2 COMPLETE -- {len(master_nd)} unique records")
    print("=" * 60)
    print(f"\nUse {final_master} for ImageJ measurements.\n")
    return final_master, final_duplicates


# ==============================================================================
# Media Download
# ==============================================================================

def _row_label(row, suffix=None):
    """Return a unique file-name label for a CSV row.

    Uses gbifID plus an optional suffix so that multiple multimedia
    records for the same specimen get distinct filenames.
    """
    gid = row.get("gbifID")
    if pd.notna(gid) and str(gid).strip():
        label = str(gid).strip()
        if suffix is not None:
            label = f"{label}_{suffix}"
        return label
    return None

def _find_image_url(row):
    """Return the first URL-like value found in the link columns."""
    for col in IMAGE_URL_COLUMNS:
        val = row.get(col)
        if pd.notna(val) and isinstance(val, str):
            val = val.strip()
            # Catch standard web links
            if val.startswith(("http://", "https://")):
                return val
    return None

_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "GBIF-HerbariaPipeline/1.0 "
        "(+https://github.com/; research-use; Python/requests)"
    ),
}


def _request_with_backoff(url, headers=None, max_retries=3, log=print):
    """GET with exponential back-off on 429 / 5xx and SSL bypass."""
    hdrs = dict(_DOWNLOAD_HEADERS)
    if headers:
        hdrs.update(headers)
    delay = 5
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=30, verify=False, headers=hdrs)
            if resp.status_code == 429 or resp.status_code >= 500:
                log(f"    {resp.status_code} on attempt {attempt}, "
                    f"retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < max_retries:
                log(f"    Request error on attempt {attempt}: {exc}. "
                    f"Retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
    raise last_exc or RuntimeError("max retries exceeded")


def _resolve_image_from_html(resp, original_url, log=print):
    """Try to extract a direct image URL from an HTML page.

    Search order:
      1. OpenGraph / Twitter / link meta tags
      2. IIIF Image API (look for /iiif/ or manifest JSON links)
      3. <img> tags whose src looks like a specimen image
      4. <a> tags pointing to a direct image file

    Returns a direct image URL (str) or None.
    """
    soup = BeautifulSoup(resp.text, "html.parser")

    # -- 1. Meta tags (og:image, twitter:image, link[rel=image_src]) --
    meta_img = (
        soup.find("meta", property="og:image")
        or soup.find("meta", attrs={"name": "twitter:image"})
        or soup.find("link", rel="image_src")
    )
    if meta_img:
        href = meta_img.get("content") or meta_img.get("href")
        if href and href.strip():
            return urljoin(original_url, href.strip())

    # -- 2. IIIF manifests --
    # Some pages embed a IIIF manifest link; the manifest JSON contains
    # the actual image URL under canvases -> images -> resource -> @id.
    iiif_link = soup.find("link", rel="alternate", type="application/ld+json")
    if iiif_link and iiif_link.get("href"):
        manifest_url = urljoin(original_url, iiif_link["href"].strip())
        img = _extract_iiif_image(manifest_url, log=log)
        if img:
            return img

    # -- 3. <img> tags whose src looks like a specimen image --
    for img_tag in soup.find_all("img", src=True):
        src = img_tag["src"].strip()
        abs_src = urljoin(original_url, src)
        # Heuristic: large specimen images usually have a recognizable
        # extension or path segment; skip tiny icons / logos.
        lower = abs_src.lower()
        if any(lower.endswith(ext) for ext in
               (".jpg", ".jpeg", ".png", ".tif", ".tiff")):
            # Skip very small images (width/height attrs < 100px)
            w = img_tag.get("width", "999")
            h = img_tag.get("height", "999")
            try:
                if int(w) < 100 or int(h) < 100:
                    continue
            except (ValueError, TypeError):
                pass
            return abs_src

    # -- 4. <a> tags pointing to a direct image file --
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        abs_href = urljoin(original_url, href)
        lower = abs_href.lower()
        if any(lower.endswith(ext) for ext in
               (".jpg", ".jpeg", ".png", ".tif", ".tiff")):
            return abs_href

    return None


def _extract_iiif_image(manifest_url, log=print):
    """Fetch a IIIF manifest and return the first full-size image URL."""
    try:
        resp = _request_with_backoff(manifest_url, log=log)
        ct = resp.headers.get("Content-Type", "")
        if "json" not in ct and "ld" not in ct:
            return None
        data = resp.json()

        # IIIF Presentation API 2 / 3 — walk to the image resource
        canvases = []
        # API 3
        items = data.get("items", [])
        if items:
            canvases = items
        # API 2
        sequences = data.get("sequences", [])
        for seq in sequences:
            canvases.extend(seq.get("canvases", []))

        for canvas in canvases:
            # API 3 path: items -> items -> body -> id
            for anno_page in canvas.get("items", []):
                for anno in anno_page.get("items", []):
                    body = anno.get("body", {})
                    img_id = body.get("id") or body.get("@id")
                    if img_id and img_id.startswith("http"):
                        return img_id

            # API 2 path: images -> resource -> @id
            for image in canvas.get("images", []):
                resource = image.get("resource", {})
                img_id = resource.get("@id") or resource.get("id")
                if img_id and img_id.startswith("http"):
                    return img_id

    except Exception as exc:
        log(f"    IIIF manifest parse failed: {exc}")
    return None


def _detect_extension(content_type, url=""):
    """Determine the file extension from Content-Type and URL."""
    ct = content_type.lower()
    if "jpeg" in ct or "jpg" in ct:
        return "jpg"
    if "png" in ct:
        return "png"
    if "tiff" in ct or "tif" in ct:
        return "tif"
    if "gif" in ct:
        return "gif"
    if "webp" in ct:
        return "webp"
    # Fall back to URL extension
    path = urlparse(url).path.lower()
    for ext in ("jpg", "jpeg", "png", "tif", "tiff", "gif", "webp"):
        if path.endswith(f".{ext}"):
            return "jpg" if ext == "jpeg" else ext
    return "jpg"


def download_media(csv_path, media_dir=None, log=print, cancel_flag=None):
    """
    Download voucher images for every row in a CSV.

    Improvements over a naive approach:
    - Unique filenames: appends a suffix for duplicate gbifIDs so every
      multimedia record gets its own file.
    - User-Agent header: avoids blocks from servers that reject bare
      Python/requests.
    - Exponential back-off: retries on 429 / 5xx up to 3 times.
    - Broad HTML scraping: meta tags → IIIF manifests → <img> tags →
      <a href> links.
    - IIIF manifest support: parses JSON to extract full-size image URL.
    - Robust relative URL handling via urljoin (handles /path,
      //domain/path, and ./relative).
    """
    media_dir = Path(media_dir) if media_dir else MEDIA_DIR
    media_dir.mkdir(parents=True, exist_ok=True)

    log("=" * 60)
    log("DOWNLOADING VOUCHER MEDIA")
    log("=" * 60 + "\n")
    log(f"Media folder: {media_dir}\n")

    try:
        df = pd.read_csv(csv_path, keep_default_na=True, low_memory=False)
    except pd.errors.ParserError:
        log("Warning: standard parser failed, retrying with Python engine...")
        df = pd.read_csv(csv_path, keep_default_na=True, engine="python",
                         on_bad_lines="warn")

    # Strip leading/trailing whitespace from all column names
    df.columns = df.columns.str.strip()

    if "media_path" not in df.columns:
        df["media_path"] = ""

    # Build unique labels: append _1, _2, … for duplicate gbifIDs
    # so every multimedia row gets a distinct filename.
    gbifid_col = df["gbifID"].astype(str).str.strip() if "gbifID" in df.columns else pd.Series("", index=df.index)
    occurrence_count = {}
    row_suffixes = []
    for gid in gbifid_col:
        n = occurrence_count.get(gid, 0)
        occurrence_count[gid] = n + 1
        row_suffixes.append(n)

    total = len(df)
    downloaded, skipped, failed = 0, 0, 0

    for idx in range(total):
        if cancel_flag and not cancel_flag():
            log(f"\nCancelled at row {idx + 1}/{total}")
            break

        row = df.iloc[idx]
        suffix = row_suffixes[idx] if row_suffixes[idx] > 0 else None
        label = _row_label(row, suffix=suffix)

        if not label:
            df.at[idx, "media_path"] = ""
            skipped += 1
            continue

        # Check if file already exists
        existing = list(media_dir.glob(f"{label}.*"))
        if existing:
            df.at[idx, "media_path"] = str(existing[0])
            skipped += 1
            continue

        url = _find_image_url(row)
        if not url:
            df.at[idx, "media_path"] = ""
            skipped += 1
            continue

        log(f"  [{idx + 1}/{total}] {label} -- {url[:70]}...")

        try:
            resp = _request_with_backoff(url, log=log)
            content_type = resp.headers.get("Content-Type", "")

            # ── JSON response → likely a IIIF manifest ──
            if "json" in content_type or "ld+json" in content_type:
                try:
                    img_url = _extract_iiif_image.__wrapped__(resp) \
                        if hasattr(_extract_iiif_image, "__wrapped__") \
                        else None
                except Exception:
                    img_url = None

                # Parse inline if _extract_iiif_image can't be reused
                if img_url is None:
                    try:
                        data = resp.json()
                        # Quick IIIF image extraction
                        for canvas in data.get("sequences", [{}])[0].get("canvases", []):
                            for image in canvas.get("images", []):
                                resource = image.get("resource", {})
                                img_url = resource.get("@id") or resource.get("id")
                                if img_url:
                                    break
                            if img_url:
                                break
                        # API 3 fallback
                        if not img_url:
                            for item in data.get("items", []):
                                for ap in item.get("items", []):
                                    for ann in ap.get("items", []):
                                        body = ann.get("body", {})
                                        img_url = body.get("id") or body.get("@id")
                                        if img_url:
                                            break
                                    if img_url:
                                        break
                                if img_url:
                                    break
                    except Exception as exc:
                        log(f"    IIIF JSON parse failed: {exc}")

                if img_url and img_url.startswith("http"):
                    log(f"    -> IIIF image: {img_url[:60]}...")
                    resp = _request_with_backoff(img_url, log=log)
                    content_type = resp.headers.get("Content-Type", "")
                else:
                    log("    FAILED: JSON response but could not extract image URL.")
                    df.at[idx, "media_path"] = ""
                    failed += 1
                    continue

            # ── HTML response → scrape for actual image URL ──
            elif "text/html" in content_type:
                direct_img_url = _resolve_image_from_html(resp, url, log=log)

                if direct_img_url:
                    log(f"    -> Resolved HTML to image: {direct_img_url[:60]}...")
                    resp = _request_with_backoff(direct_img_url, log=log)
                    content_type = resp.headers.get("Content-Type", "")
                else:
                    log("    FAILED: HTML page did not contain a "
                        "recognizable image (may require JS).")
                    df.at[idx, "media_path"] = ""
                    failed += 1
                    continue

            # ── Check that we actually got image bytes ──
            if "text/html" in content_type or "json" in content_type:
                log("    FAILED: final response is not an image "
                    f"(Content-Type: {content_type}).")
                df.at[idx, "media_path"] = ""
                failed += 1
                continue

            ext = _detect_extension(content_type, url=resp.url)
            filepath = media_dir / f"{label}.{ext}"
            filepath.write_bytes(resp.content)
            df.at[idx, "media_path"] = str(filepath)
            downloaded += 1

        except Exception as exc:
            log(f"    FAILED: {exc}")
            df.at[idx, "media_path"] = ""
            failed += 1

        # Save progress every 25 rows
        if (idx + 1) % 25 == 0:
            df.to_csv(csv_path, index=False, quoting=csv.QUOTE_ALL)

    # Final save
    df.to_csv(csv_path, index=False, quoting=csv.QUOTE_ALL)

    log(f"\nMedia download complete: {downloaded} downloaded, "
        f"{skipped} skipped, {failed} failed")
    log(f"Media folder: {media_dir}")
    log(f"CSV updated: {csv_path}\n")
    return downloaded, skipped, failed

# ==============================================================================
# ML -- Model Manager
# ==============================================================================

class ModelManager:
    """Manage VLM downloads and loading via HuggingFace transformers."""

    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._config = self._load_config()
        self._model = None
        self._processor = None
        self._loaded_id = None

    # -- Config persistence ----------------------------------------------------

    def _load_config(self):
        if CONFIG_FILE.exists():
            try:
                return json.loads(CONFIG_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "models": {
                DEFAULT_MODEL_ID: {
                    "description": "Qwen3 Vision-Language 30B MoE (3B active). Default.",
                }
            },
            "default": DEFAULT_MODEL_ID,
        }

    def _save_config(self):
        CONFIG_FILE.write_text(json.dumps(self._config, indent=2))

    # -- Registry --------------------------------------------------------------

    def list_models(self):
        """Return list of (model_id, description, is_downloaded) tuples."""
        out = []
        for mid, info in self._config.get("models", {}).items():
            out.append((mid, info.get("description", ""), self.is_downloaded(mid)))
        return out

    def default_model(self):
        return self._config.get("default", DEFAULT_MODEL_ID)

    def set_default(self, model_id):
        self._config["default"] = model_id
        self._save_config()

    def register_model(self, model_id, description=""):
        """Add a HuggingFace model ID or local path to the registry."""
        models = self._config.setdefault("models", {})
        if model_id not in models:
            models[model_id] = {"description": description}
            self._save_config()

    def remove_model(self, model_id):
        """Remove a model from the registry (does not delete cached files)."""
        self._config.get("models", {}).pop(model_id, None)
        if self._config.get("default") == model_id:
            remaining = list(self._config.get("models", {}).keys())
            self._config["default"] = remaining[0] if remaining else ""
        self._save_config()

    # -- Download / status -----------------------------------------------------

    @staticmethod
    def is_downloaded(model_id):
        """Check whether model files are locally available."""
        if not ML_AVAILABLE:
            return False
        # Check if model_id is itself a local directory (user-provided path)
        local = Path(model_id)
        if local.is_dir() and (local / "config.json").exists():
            return True
        # Check our local ./models cache
        try:
            from huggingface_hub import scan_cache_dir
            cache = scan_cache_dir(MODELS_DIR)
            for repo in cache.repos:
                if repo.repo_id == model_id:
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def download_model(model_id, log=print):
        """Download model weights into the local ./models directory."""
        if not ML_AVAILABLE:
            raise RuntimeError(
                "ML dependencies not installed. Run:\n"
                "  pip install torch torchvision transformers accelerate pillow"
            )
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        log(f"Downloading {model_id} to {MODELS_DIR} (this may take a while)...")
        from huggingface_hub import snapshot_download
        snapshot_download(model_id, cache_dir=MODELS_DIR)
        log(f"Download complete: {model_id}")

    # -- Load / unload ---------------------------------------------------------

    def load(self, model_id=None, log=print):
        """Load a model and its processor into memory."""
        if not ML_AVAILABLE:
            raise RuntimeError(
                "ML dependencies not installed. Run:\n"
                "  pip install torch torchvision transformers accelerate pillow"
            )
        model_id = model_id or self.default_model()
        if self._loaded_id == model_id and self._model is not None:
            log(f"Model already loaded: {model_id}")
            return

        self.unload()
        log(f"Loading model: {model_id}")
        log(f"   Cache: {MODELS_DIR}")
        dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
        device_map = "auto" if torch.cuda.is_available() else "cpu"
        log(f"   Device: {device_map} | Dtype: {dtype}")

        cache = str(MODELS_DIR)
        self._processor = AutoProcessor.from_pretrained(
            model_id, cache_dir=cache)
        self._model = AutoModelForImageTextToText.from_pretrained(
            model_id, torch_dtype=dtype, device_map=device_map,
            cache_dir=cache,
        )
        self._loaded_id = model_id
        log(f"Model ready: {model_id}")

    def unload(self):
        """Free model from memory."""
        if self._model is not None:
            del self._model
            del self._processor
            self._model = None
            self._processor = None
            self._loaded_id = None
            if ML_AVAILABLE and torch.cuda.is_available():
                torch.cuda.empty_cache()

    @property
    def loaded_id(self):
        return self._loaded_id

    @property
    def model(self):
        return self._model

    @property
    def processor(self):
        return self._processor


# ==============================================================================
# ML -- Measurement Engine
# ==============================================================================

class MeasurementEngine:
    """Run VLM inference on herbarium images to extract trait measurements."""

    def __init__(self, manager: ModelManager):
        self.manager = manager

    # -- Single image ----------------------------------------------------------

    @staticmethod
    def _fetch_image(url_or_path):
        """Download an image from a URL or load from a local path."""
        path = Path(url_or_path)
        if path.is_file():
            return Image.open(path).convert("RGB")

        if not url_or_path.startswith(("http://", "https://")):
            return None

        resp = requests.get(url_or_path, timeout=30)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGB")

    @staticmethod
    def _parse_response(text):
        """Extract a JSON dict from the model's text output."""
        # Strip markdown fences if present
        cleaned = re.sub(r"```(?:json)?", "", text).strip()
        # Find the first {...} block
        match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return None

    def measure_image(self, image, prompt=None):
        """
        Run measurement on a single PIL Image.

        Returns a dict with panicle_length_cm, leaf_width_cm,
        seed_length_cm, and notes.  Returns None on failure.
        """
        model = self.manager.model
        proc = self.manager.processor
        if model is None or proc is None:
            raise RuntimeError("No model loaded. Call manager.load() first.")

        prompt = prompt or MEASUREMENT_PROMPT

        messages = [
            {"role": "user", "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": prompt},
            ]},
        ]
        text_input = proc.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = proc(
            text=[text_input], images=[image],
            return_tensors="pt", padding=True,
        )
        device = next(model.parameters()).device
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.inference_mode():
            ids = model.generate(**inputs, max_new_tokens=512)

        # Decode only the newly generated tokens
        gen_ids = ids[:, inputs["input_ids"].shape[1]:]
        output = proc.batch_decode(gen_ids, skip_special_tokens=True)[0]
        return self._parse_response(output)

    # -- Batch CSV -------------------------------------------------------------

    @staticmethod
    def _resolve_image_path(row, media_dir):
        """Find the local image file for a row.

        Priority:
          1. media_path column (set by download_media)
          2. Glob media_dir by catalogNumber / gbifID
          3. Fall back to image URL for on-the-fly download
        """
        # 1. Explicit media_path column
        mp = row.get("media_path")
        if pd.notna(mp) and isinstance(mp, str) and Path(mp.strip()).is_file():
            return mp.strip()

        # 2. Glob by label
        label = _row_label(row)
        if label and media_dir:
            hits = list(Path(media_dir).glob(f"{label}.*"))
            if hits:
                return str(hits[0])

        # 3. URL fallback
        return _find_image_url(row)

    def measure_csv(self, csv_path, media_dir=None, output_path=None,
                    log=print, cancel_flag=None):
        """
        Measure traits for every row in a CSV.

        Looks for local images in media_dir first (matched by catalog
        number), then falls back to downloading from the URL columns.
        Saves the CSV after every successful measurement so progress
        is never lost.

        Returns the output path.
        """
        media_dir = Path(media_dir) if media_dir else MEDIA_DIR
        try:
            df = pd.read_csv(csv_path, keep_default_na=True, low_memory=False)
        except pd.errors.ParserError:
            log("Warning: standard parser failed, retrying with Python engine...")
            df = pd.read_csv(csv_path, keep_default_na=True,
                             engine="python", on_bad_lines="warn")
        total = len(df)
        output_path = output_path or csv_path

        for col in ("Panicle length (cm)", "Leaf width (cm)",
                     "Seed length (cm)", "ml_notes"):
            if col not in df.columns:
                df[col] = ""

        measured, skipped, failed = 0, 0, 0

        for idx in range(total):
            if cancel_flag and not cancel_flag():
                log(f"\nCancelled at row {idx + 1}/{total}")
                break

            row = df.iloc[idx]

            # Skip rows that already have measurements
            has_data = any(
                pd.notna(row.get(c)) and str(row.get(c)).strip()
                for c in ("Panicle length (cm)", "Leaf width (cm)", "Seed length (cm)")
            )
            if has_data:
                skipped += 1
                continue

            src = self._resolve_image_path(row, media_dir)
            if not src:
                df.at[idx, "ml_notes"] = "no image found"
                skipped += 1
                continue

            label = _row_label(row) or str(idx)
            log(f"  [{idx + 1}/{total}] {label} -- {str(src)[:70]}...")

            try:
                image = self._fetch_image(src)
                if image is None:
                    df.at[idx, "ml_notes"] = "could not load image"
                    failed += 1
                    continue

                result = self.measure_image(image)
                if result is None:
                    df.at[idx, "ml_notes"] = "model returned unparseable output"
                    failed += 1
                    continue

                pl = result.get("panicle_length_cm")
                lw = result.get("leaf_width_cm")
                sl = result.get("seed_length_cm")
                notes = result.get("notes", "")

                df.at[idx, "Panicle length (cm)"] = pl if pl is not None else ""
                df.at[idx, "Leaf width (cm)"] = lw if lw is not None else ""
                df.at[idx, "Seed length (cm)"] = sl if sl is not None else ""
                df.at[idx, "ml_notes"] = notes
                measured += 1

                # Save after every successful measurement
                df.to_csv(output_path, index=False, quoting=csv.QUOTE_ALL)

            except Exception as exc:
                df.at[idx, "ml_notes"] = f"error: {exc}"
                failed += 1

        # Final save
        df.to_csv(output_path, index=False, quoting=csv.QUOTE_ALL)
        log(f"\nMeasurement complete: {measured} measured, "
            f"{skipped} skipped, {failed} failed")
        log(f"Saved to: {output_path}")
        return output_path


# ==============================================================================
# GUI -- Issue Filter Dialog
# ==============================================================================

class IssueFilterDialog(tk.Toplevel):
    def __init__(self, parent, bad_vars, inspect_vars):
        super().__init__(parent)
        self.title("GBIF Issue Filters")
        self.geometry("720x620")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()
        self.bad_vars = bad_vars
        self.inspect_vars = inspect_vars
        self._cancelled = True
        self._canvases = []          # track canvases for cleanup
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        nb.add(self._build_tab(nb, self.bad_vars, BAD_GEOSPATIAL_ISSUES),
               text=f"  Remove Records ({self._count(self.bad_vars)} active)  ")
        nb.add(self._build_tab(nb, self.inspect_vars, INSPECTION_ISSUES),
               text=f"  Flag for Inspection ({self._count(self.inspect_vars)} active)  ")
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=10, pady=10)
        ttk.Button(bf, text="OK", command=self._on_ok).pack(side="right", padx=5)
        ttk.Button(bf, text="Cancel", command=self._on_cancel).pack(side="right", padx=5)

    @staticmethod
    def _count(vd):
        return sum(1 for v in vd.values() if v.get())

    def _build_tab(self, parent, var_dict, issue_list):
        outer = ttk.Frame(parent)
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(8, 4), padx=6)
        ttk.Button(bar, text="Select All",
                   command=lambda: self._set_all(var_dict, True)).pack(side="left", padx=4)
        ttk.Button(bar, text="Deselect All",
                   command=lambda: self._set_all(var_dict, False)).pack(side="left", padx=4)
        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)

        # Mousewheel — bind to the canvas and its children only (not globally).
        # macOS sends delta in single-unit increments; Linux/Windows in multiples of 120.
        def _on_mousewheel(event):
            if sys.platform == "darwin":
                canvas.yview_scroll(int(-1 * event.delta), "units")
            else:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_wheel(widget):
            widget.bind("<MouseWheel>", _on_mousewheel)
            # Linux uses Button-4 / Button-5 instead of MouseWheel
            widget.bind("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
            widget.bind("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))

        _bind_wheel(canvas)
        inner.bind("<Map>", lambda e: _bind_descendants(inner), add="+")

        def _bind_descendants(w):
            for child in w.winfo_children():
                _bind_wheel(child)
                _bind_descendants(child)

        _bind_descendants(inner)
        self._canvases.append(canvas)

        canvas.pack(side="left", fill="both", expand=True, padx=(6, 0))
        sb.pack(side="right", fill="y", padx=(0, 6))
        for issue in issue_list:
            desc = ISSUE_DESCRIPTIONS.get(issue, "")
            display = issue.replace("_", " ").title()
            fr = ttk.Frame(inner)
            fr.pack(fill="x", pady=2, padx=4)
            ttk.Checkbutton(fr, variable=var_dict[issue]).pack(side="left")
            lf = ttk.Frame(fr)
            lf.pack(side="left", fill="x", expand=True, padx=(4, 0))
            ttk.Label(lf, text=display, font=("TkDefaultFont", 9, "bold")).pack(anchor="w")
            if desc:
                ttk.Label(lf, text=desc, foreground="gray", wraplength=560).pack(anchor="w")
        # Bind all the widgets we just created
        _bind_descendants(inner)
        return outer

    def _set_all(self, vd, val):
        for v in vd.values():
            v.set(val)

    def _on_ok(self):
        self._cancelled = False
        self.destroy()

    def _on_cancel(self):
        self._cancelled = True
        self.destroy()

    @property
    def cancelled(self):
        return self._cancelled


# ==============================================================================
# GUI -- Model Manager Dialog
# ==============================================================================

class ModelManagerDialog(tk.Toplevel):
    """Dialog for listing, downloading, adding, and removing VLM models."""

    def __init__(self, parent, manager: ModelManager):
        super().__init__(parent)
        self.title("Manage Models")
        self.geometry("640x420")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()
        self.manager = manager
        self._build()

    def _build(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="both", expand=True)

        if not ML_AVAILABLE:
            ttk.Label(
                top, wraplength=580, foreground="red",
                text=("ML dependencies are not installed.  "
                      "Run:  pip install torch torchvision transformers accelerate pillow"),
            ).pack(pady=20)

        # Model list
        cols = ("model_id", "status", "description")
        self.tree = ttk.Treeview(top, columns=cols, show="headings", height=8)
        self.tree.heading("model_id", text="Model ID")
        self.tree.heading("status", text="Status")
        self.tree.heading("description", text="Description")
        self.tree.column("model_id", width=280)
        self.tree.column("status", width=90, anchor="center")
        self.tree.column("description", width=220)
        self.tree.pack(fill="both", expand=True)
        self._refresh_list()

        # Buttons
        bf = ttk.Frame(top)
        bf.pack(fill="x", pady=(10, 0))
        ttk.Button(bf, text="Download Selected", command=self._download).pack(side="left", padx=4)
        ttk.Button(bf, text="Set as Default", command=self._set_default).pack(side="left", padx=4)
        ttk.Button(bf, text="Remove Selected", command=self._remove).pack(side="left", padx=4)

        sep = ttk.Separator(top, orient="horizontal")
        sep.pack(fill="x", pady=(10, 6))

        # Add model
        af = ttk.Frame(top)
        af.pack(fill="x")
        ttk.Label(af, text="Add model:").pack(side="left")
        self.add_entry = ttk.Entry(af, width=40)
        self.add_entry.pack(side="left", padx=(6, 4), fill="x", expand=True)
        self.add_entry.insert(0, "owner/model-name")
        ttk.Button(af, text="Register", command=self._add_model).pack(side="left", padx=4)

        # Console
        self.console = scrolledtext.ScrolledText(top, height=5, state="disabled", wrap="word")
        self.console.pack(fill="both", expand=True, pady=(8, 0))

        ttk.Button(top, text="Close", command=self.destroy).pack(anchor="e", pady=(6, 0))

    def _refresh_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        default = self.manager.default_model()
        for mid, desc, downloaded in self.manager.list_models():
            status = "Ready" if downloaded else "Not downloaded"
            label = mid
            if mid == default:
                label = mid + "  [default]"
            self.tree.insert("", "end", iid=mid, values=(label, status, desc))

    def _selected_id(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _log(self, msg):
        self.console.config(state="normal")
        self.console.insert(tk.END, msg + "\n")
        self.console.see(tk.END)
        self.console.config(state="disabled")
        self.update()

    def _download(self):
        mid = self._selected_id()
        if not mid:
            return
        def _work():
            try:
                ModelManager.download_model(mid, log=self._log)
            except Exception as exc:
                self._log(f"Error: {exc}")
            self.after(0, self._refresh_list)
        threading.Thread(target=_work, daemon=True).start()

    def _set_default(self):
        mid = self._selected_id()
        if mid:
            self.manager.set_default(mid)
            self._refresh_list()
            self._log(f"Default model set to: {mid}")

    def _remove(self):
        mid = self._selected_id()
        if mid:
            self.manager.remove_model(mid)
            self._refresh_list()
            self._log(f"Removed from registry: {mid}")

    def _add_model(self):
        mid = self.add_entry.get().strip()
        if mid and mid != "owner/model-name":
            self.manager.register_model(mid)
            self._refresh_list()
            self._log(f"Registered: {mid}")


# ==============================================================================
# GUI -- Main Application
# ==============================================================================

class GBIFPipelineGUI:
    """Tkinter GUI for the GBIF pipeline."""

    def __init__(self, root):
        self.root = root
        self.root.title("GBIF Herbaria Data Pipeline")
        self.root.geometry("740x930")
        self.is_running = False

        self.model_manager = ModelManager()

        self.bad_issue_vars = {
            i: tk.BooleanVar(value=True) for i in BAD_GEOSPATIAL_ISSUES
        }
        self.inspect_issue_vars = {
            i: tk.BooleanVar(value=True) for i in INSPECTION_ISSUES
        }

        self._build_menu()
        self._build_ui()

    # ------------------------------------------------------------------
    # Menu bar
    # ------------------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        models_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Models", menu=models_menu)
        models_menu.add_command(label="Manage Models...",
                                command=self._open_model_manager)

    def _open_model_manager(self):
        ModelManagerDialog(self.root, self.model_manager)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        main = ttk.Frame(self.root, padding="10")
        main.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        ttk.Label(main, text="GBIF Herbaria Data Pipeline",
                  font=("Arial", 14, "bold")).grid(
            row=0, column=0, columnspan=3, pady=(0, 15))

        # --- Workflow ---
        wf = ttk.LabelFrame(main, text="Workflow", padding="10")
        wf.grid(row=1, column=0, columnspan=3, sticky="ew", pady=10)
        self.workflow_var = tk.StringVar(value="full")
        for label, val in [("Download Only", "download"),
                           ("Prepare Only (Phase 1 & 2)", "prepare"),
                           ("Full Workflow (Download + Prepare)", "full")]:
            ttk.Radiobutton(wf, text=label, variable=self.workflow_var,
                            value=val, command=self._update_ui).pack(anchor="w")

        # --- Species ---
        self.species_frame = ttk.LabelFrame(main, text="Species Selection", padding="10")
        self.species_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(self.species_frame, text="Species Name:").pack(anchor="w")
        self.species_entry = ttk.Entry(self.species_frame, width=50)
        self.species_entry.insert(0, "Megathyrsus maximus")
        self.species_entry.pack(fill="x", pady=5)

        # --- Data path (prepare mode) ---
        self.data_frame = ttk.LabelFrame(main, text="Data Selection", padding="10")
        ttk.Label(self.data_frame, text="Data Folder or ZIP:").pack(anchor="w")
        self.data_entry = ttk.Entry(self.data_frame, width=50)
        self.data_entry.pack(fill="x", pady=5)
        ttk.Button(self.data_frame, text="Browse...",
                   command=self._browse_data).pack(anchor="e", pady=5)

        # --- Phase selection (prepare mode) ---
        self.phase_frame = ttk.LabelFrame(main, text="Phases", padding="10")
        self.phase_var = tk.StringVar(value="both")
        for label, val in [("Phase 1 & 2", "both"),
                           ("Phase 1 Only", "phase1"),
                           ("Phase 2 Only", "phase2")]:
            ttk.Radiobutton(self.phase_frame, text=label,
                            variable=self.phase_var, value=val).pack(anchor="w")

        # --- Filters ---
        filt = ttk.LabelFrame(main, text="Filters", padding="10")
        filt.grid(row=3, column=0, columnspan=3, sticky="ew", pady=10)

        pr = ttk.Frame(filt)
        pr.pack(fill="x")
        ttk.Label(pr, text="Preset:").pack(side="left")
        self.preset_var = tk.StringVar(value="G064 (Default)")
        combo = ttk.Combobox(pr, textvariable=self.preset_var,
                             values=list(PRESETS.keys()), state="readonly", width=20)
        combo.pack(side="left", padx=(8, 0))
        combo.bind("<<ComboboxSelected>>", lambda _: self._load_preset())

        ttk.Label(filt, text="Coordinate Precision:").pack(anchor="w", pady=(10, 0))
        self.precision_var = tk.StringVar(value=PRECISION_RELAXED)
        for label, val in [("None (no coordinate filtering)", PRECISION_NONE),
                           ("Relaxed (>=1 decimal place)", PRECISION_RELAXED),
                           ("Strict (>=3 decimal places)", PRECISION_STRICT)]:
            ttk.Radiobutton(filt, text=label, variable=self.precision_var,
                            value=val).pack(anchor="w")

        ttk.Label(filt, text="Taxa Exclusions (one per line):").pack(anchor="w", pady=(10, 0))
        self.taxa_text = tk.Text(filt, height=4, width=50)
        self.taxa_text.pack(fill="both", expand=True, pady=5)

        ir = ttk.Frame(filt)
        ir.pack(fill="x", pady=(6, 0))
        ttk.Button(ir, text="Configure Issue Filters...",
                   command=self._open_issue_dialog).pack(side="left")
        self.issue_summary_label = ttk.Label(ir, text="", foreground="gray")
        self.issue_summary_label.pack(side="left", padx=(10, 0))
        self._refresh_issue_summary()

        self._load_preset()

        # --- Media & Measurement ---
        ml_frame = ttk.LabelFrame(main, text="Media Download & Auto-Measurement",
                                  padding="10")
        ml_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=10)

        # CSV row
        cr = ttk.Frame(ml_frame)
        cr.pack(fill="x")
        ttk.Label(cr, text="CSV:").pack(side="left")
        self.measure_csv_entry = ttk.Entry(cr, width=44)
        self.measure_csv_entry.pack(side="left", padx=(8, 4), fill="x", expand=True)
        ttk.Button(cr, text="Browse...", command=self._browse_measure_csv).pack(side="left")

        # Media folder row
        mfr = ttk.Frame(ml_frame)
        mfr.pack(fill="x", pady=(6, 0))
        ttk.Label(mfr, text="Media:").pack(side="left")
        self.media_dir_entry = ttk.Entry(mfr, width=44)
        self.media_dir_entry.insert(0, str(MEDIA_DIR))
        self.media_dir_entry.pack(side="left", padx=(8, 4), fill="x", expand=True)
        ttk.Button(mfr, text="Browse...", command=self._browse_media_dir).pack(side="left")

        # Download media button
        dbr = ttk.Frame(ml_frame)
        dbr.pack(fill="x", pady=(6, 0))
        self.dl_media_btn = ttk.Button(dbr, text="Download Media",
                                       command=self._run_download_media)
        self.dl_media_btn.pack(side="left")
        self.media_status = ttk.Label(dbr, text="", foreground="gray")
        self.media_status.pack(side="left", padx=(10, 0))

        # Model row
        sep = ttk.Separator(ml_frame, orient="horizontal")
        sep.pack(fill="x", pady=(8, 6))
        mr = ttk.Frame(ml_frame)
        mr.pack(fill="x")
        ttk.Label(mr, text="Model:").pack(side="left")
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(mr, textvariable=self.model_var,
                                        state="readonly", width=42)
        self.model_combo.pack(side="left", padx=(8, 4), fill="x", expand=True)
        ttk.Button(mr, text="Refresh", command=self._refresh_model_list).pack(side="left")
        self._refresh_model_list()

        # Measure button
        br = ttk.Frame(ml_frame)
        br.pack(fill="x", pady=(6, 0))
        self.measure_btn = ttk.Button(br, text="Run Auto-Measurement",
                                      command=self._run_measurement)
        self.measure_btn.pack(side="left")
        self.ml_status = ttk.Label(br, text="", foreground="gray")
        self.ml_status.pack(side="left", padx=(10, 0))

        if not ML_AVAILABLE:
            self.ml_status.config(
                text="ML unavailable -- pip install torch torchvision transformers accelerate pillow",
                foreground="red",
            )
            self.measure_btn.config(state="disabled")

        # --- Console ---
        con = ttk.LabelFrame(main, text="Console Output", padding="10")
        con.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=10)
        main.rowconfigure(5, weight=1)
        self.console = scrolledtext.ScrolledText(
            con, height=10, width=80, state="disabled", wrap="word")
        self.console.pack(fill="both", expand=True)

        # --- Buttons ---
        btns = ttk.Frame(main)
        btns.grid(row=6, column=0, columnspan=3, sticky="ew", pady=10)
        self.run_btn = ttk.Button(btns, text="Run", command=self._run)
        self.run_btn.pack(side="left", padx=5)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self._cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=5)
        ttk.Button(btns, text="Clear", command=self._clear_console).pack(side="left", padx=5)

        self._update_ui()

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _update_ui(self):
        wf = self.workflow_var.get()
        if wf == "download":
            self.species_frame.grid()
            self.data_frame.grid_remove()
            self.phase_frame.grid_remove()
        elif wf == "prepare":
            self.species_frame.grid_remove()
            self.data_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)
            self.phase_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=10)
        else:
            self.species_frame.grid()
            self.data_frame.grid_remove()
            self.phase_frame.grid_remove()

    def _load_preset(self):
        name = self.preset_var.get()
        preset = PRESETS.get(name, {})
        self.species_entry.delete(0, tk.END)
        self.species_entry.insert(0, preset.get("species", ""))
        self.precision_var.set(preset.get("precision", PRECISION_RELAXED))
        taxa = preset.get("exclude_taxa", [])
        self.taxa_text.config(state="normal")
        self.taxa_text.delete("1.0", tk.END)
        self.taxa_text.insert("1.0", "\n".join(taxa))
        if name != "Custom":
            self.taxa_text.config(state="disabled")
        for cfg_key, var_dict, full_list in [
            ("bad_issues", self.bad_issue_vars, BAD_GEOSPATIAL_ISSUES),
            ("inspect_issues", self.inspect_issue_vars, INSPECTION_ISSUES),
        ]:
            cfg = preset.get(cfg_key, True)
            for issue, var in var_dict.items():
                if cfg is True:
                    var.set(True)
                elif isinstance(cfg, list):
                    var.set(issue in cfg)
                else:
                    var.set(False)
        self._refresh_issue_summary()

    def _open_issue_dialog(self):
        dlg = IssueFilterDialog(self.root, self.bad_issue_vars, self.inspect_issue_vars)
        self.root.wait_window(dlg)
        self._refresh_issue_summary()

    def _refresh_issue_summary(self):
        bn = sum(1 for v in self.bad_issue_vars.values() if v.get())
        sn = sum(1 for v in self.inspect_issue_vars.values() if v.get())
        self.issue_summary_label.config(
            text=f"Removing {bn}/{len(BAD_GEOSPATIAL_ISSUES)} | "
                 f"Flagging {sn}/{len(INSPECTION_ISSUES)}")

    def _browse_data(self):
        path = filedialog.askdirectory(title="Select data folder")
        if not path:
            path = filedialog.askopenfilename(
                title="Select ZIP file",
                filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")])
        if path:
            self.data_entry.delete(0, tk.END)
            self.data_entry.insert(0, path)

    def _browse_measure_csv(self):
        path = filedialog.askopenfilename(
            title="Select CSV for measurement",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self.measure_csv_entry.delete(0, tk.END)
            self.measure_csv_entry.insert(0, path)

    def _browse_media_dir(self):
        path = filedialog.askdirectory(title="Select media folder")
        if path:
            self.media_dir_entry.delete(0, tk.END)
            self.media_dir_entry.insert(0, path)

    def _refresh_model_list(self):
        models = self.model_manager.list_models()
        default = self.model_manager.default_model()
        vals = [mid for mid, _, _ in models]
        self.model_combo["values"] = vals
        if default in vals:
            self.model_var.set(default)
        elif vals:
            self.model_var.set(vals[0])

    def _log(self, msg):
        self.console.config(state="normal")
        self.console.insert(tk.END, msg + "\n")
        self.console.see(tk.END)
        self.console.config(state="disabled")
        self.root.update()

    def _clear_console(self):
        self.console.config(state="normal")
        self.console.delete("1.0", tk.END)
        self.console.config(state="disabled")

    def _get_exclude_taxa(self):
        return [l.strip() for l in self.taxa_text.get("1.0", tk.END).split("\n") if l.strip()]

    def _get_active_bad_issues(self):
        return [k for k, v in self.bad_issue_vars.items() if v.get()]

    def _get_active_inspect_issues(self):
        return [k for k, v in self.inspect_issue_vars.items() if v.get()]

    def _set_running(self, running):
        self.is_running = running
        self.run_btn.config(state="disabled" if running else "normal")
        self.cancel_btn.config(state="normal" if running else "disabled")
        self.dl_media_btn.config(state="disabled" if running else "normal")
        self.measure_btn.config(
            state="disabled" if running or not ML_AVAILABLE else "normal")

    def _phase1_kwargs(self):
        return dict(
            exclude_taxa=self._get_exclude_taxa() or None,
            coordinate_precision=self.precision_var.get(),
            bad_geospatial_issues=self._get_active_bad_issues(),
            inspection_issues=self._get_active_inspect_issues(),
        )

    # ------------------------------------------------------------------
    # Workflow runners
    # ------------------------------------------------------------------

    def _run(self):
        self._set_running(True)
        target = {"download": self._run_download,
                  "prepare": self._run_prepare,
                  "full": self._run_full}[self.workflow_var.get()]
        threading.Thread(target=target, daemon=True).start()

    def _cancel(self):
        self.is_running = False
        self._log("\nWorkflow cancelled")
        self._set_running(False)

    def _run_download(self):
        try:
            species = self.species_entry.get().strip()
            if not species:
                self._log("ERROR: Please enter a species name"); return
            self._log(f"\n{'='*60}\nDOWNLOADING: {species}\n{'='*60}\n")
            tk_ = resolve_species(species)
            dk = trigger_download(_build_gbif_queries(tk_, preset_name=self.preset_var.get()), "DWCA",
                                  GBIF_USER, GBIF_PASSWORD, GBIF_EMAIL)
            self._log(f"Download key: {dk}\nWaiting for GBIF (5-30 min)...\n")
            zf = wait_and_download(dk)
            folder = extract_archive(zf, f"./data_{dk}")
            self._log(f"\nExtracted to: {folder}")
            messagebox.showinfo("Complete", f"Download complete!\n{folder}")
        except Exception as e:
            self._log(f"ERROR: {e}"); messagebox.showerror("Error", str(e))
        finally:
            self._set_running(False)

    def _run_prepare(self):
        try:
            data_path = self.data_entry.get().strip()
            if not data_path:
                self._log("ERROR: Please select a data folder or ZIP"); return
            if os.path.isfile(data_path) and data_path.endswith(".zip"):
                self._log("Extracting ZIP...")
                dest = Path(data_path).stem
                with zipfile.ZipFile(data_path, "r") as zf:
                    zf.extractall(dest)
                data_path = dest
                self._log(f"Extracted to: {data_path}\n")
            if not os.path.isdir(data_path):
                self._log("ERROR: Invalid data folder"); return
            phase = self.phase_var.get()
            occ = os.path.join(data_path, "occurrence.txt")
            mul = os.path.join(data_path, "multimedia.txt")
            if phase in ("both", "phase1"):
                if not os.path.exists(occ) or not os.path.exists(mul):
                    self._log("ERROR: occurrence.txt or multimedia.txt not found"); return
                self._log(f"\n{'='*60}\nPHASE 1: AUTOMATED CLEANING\n{'='*60}\n")
                phase_1_clean_and_merge(occ, mul,
                                        output_csv="GBIFdownload_inspectFlags.csv",
                                        **self._phase1_kwargs())
                self._log("\nPhase 1 complete! Edit GBIFdownload_inspectFlags.csv in Excel")
                if phase == "both" and messagebox.askyesno("Proceed", "Run Phase 2?"):
                    self._log(f"\n{'='*60}\nPHASE 2: FINALIZATION\n{'='*60}\n")
                    phase_2_finalize_dataset("GBIFdownload_inspectFlags.csv",
                                             final_master="master_cleaned.csv",
                                             final_duplicates="removed_duplicates.csv")
                    self._log("\nPhase 2 complete!")
                    messagebox.showinfo("Complete", "Phase 2 complete!")
            elif phase == "phase2":
                self._log(f"\n{'='*60}\nPHASE 2: FINALIZATION\n{'='*60}\n")
                phase_2_finalize_dataset("GBIFdownload_inspectFlags.csv",
                                         final_master="master_cleaned.csv",
                                         final_duplicates="removed_duplicates.csv")
                self._log("\nPhase 2 complete!")
                messagebox.showinfo("Complete", "Phase 2 complete!")
        except Exception as e:
            self._log(f"ERROR: {e}"); messagebox.showerror("Error", str(e))
        finally:
            self._set_running(False)

    def _run_full(self):
        try:
            species = self.species_entry.get().strip()
            if not species:
                self._log("ERROR: Please enter a species name"); return
            self._log(f"\n{'='*60}\nSTEP 1: DOWNLOADING {species}\n{'='*60}\n")
            tk_ = resolve_species(species)
            dk = trigger_download(_build_gbif_queries(tk_, preset_name=self.preset_var.get()), "DWCA",
                                  GBIF_USER, GBIF_PASSWORD, GBIF_EMAIL)
            self._log(f"Download key: {dk}\nWaiting for GBIF (5-30 min)...\n")
            zf = wait_and_download(dk)
            folder = extract_archive(zf, f"./data_{dk}")
            self._log(f"\n{'='*60}\nSTEP 2: PHASE 1 CLEANING\n{'='*60}\n")
            occ = os.path.join(folder, "occurrence.txt")
            mul = os.path.join(folder, "multimedia.txt")
            phase_1_clean_and_merge(occ, mul,
                                    output_csv="GBIFdownload_inspectFlags.csv",
                                    **self._phase1_kwargs())
            self._log("\nPhase 1 complete! Edit GBIFdownload_inspectFlags.csv in Excel")
            if not messagebox.askyesno("Manual Review",
                                       "Edit the CSV, then click Yes for Phase 2"):
                return
            self._log(f"\n{'='*60}\nSTEP 3: PHASE 2 FINALIZATION\n{'='*60}\n")
            phase_2_finalize_dataset("GBIFdownload_inspectFlags.csv",
                                     final_master="master_cleaned.csv",
                                     final_duplicates="removed_duplicates.csv")
            self._log("\nWorkflow complete!")
            messagebox.showinfo("Complete", "Workflow complete!\nMaster: master_cleaned.csv")
        except Exception as e:
            self._log(f"ERROR: {e}"); messagebox.showerror("Error", str(e))
        finally:
            self._set_running(False)

    # ------------------------------------------------------------------
    # Media download runner
    # ------------------------------------------------------------------

    def _run_download_media(self):
        csv_path = self.measure_csv_entry.get().strip()
        media_dir = self.media_dir_entry.get().strip()
        if not csv_path:
            messagebox.showwarning("Missing CSV", "Select a CSV file first.")
            return

        self._set_running(True)
        self.media_status.config(text="Downloading...", foreground="blue")

        def _work():
            try:
                dl, sk, fa = download_media(
                    csv_path,
                    media_dir=media_dir or None,
                    log=self._log,
                    cancel_flag=lambda: self.is_running,
                )
                self.media_status.config(
                    text=f"{dl} downloaded, {sk} skipped, {fa} failed",
                    foreground="green")
                messagebox.showinfo("Complete",
                                    f"Media download complete.\n"
                                    f"{dl} downloaded, {sk} skipped, {fa} failed")
            except Exception as exc:
                self._log(f"ERROR: {exc}\n{traceback.format_exc()}")
                self.media_status.config(text="Error", foreground="red")
                messagebox.showerror("Error", str(exc))
            finally:
                self._set_running(False)

        threading.Thread(target=_work, daemon=True).start()

    # ------------------------------------------------------------------
    # Auto-measurement runner
    # ------------------------------------------------------------------

    def _run_measurement(self):
        csv_path = self.measure_csv_entry.get().strip()
        media_dir = self.media_dir_entry.get().strip()
        model_id = self.model_var.get().strip()
        if not csv_path:
            messagebox.showwarning("Missing CSV", "Select a CSV file first.")
            return
        if not model_id:
            messagebox.showwarning("Missing Model", "Select a model first.")
            return

        self._set_running(True)
        self.ml_status.config(text="Loading model...", foreground="blue")

        def _work():
            try:
                self.model_manager.load(model_id, log=self._log)
                engine = MeasurementEngine(self.model_manager)
                self.ml_status.config(text="Measuring...", foreground="blue")
                engine.measure_csv(
                    csv_path,
                    media_dir=media_dir or None,
                    log=self._log,
                    cancel_flag=lambda: self.is_running,
                )
                self.ml_status.config(text="Done", foreground="green")
                messagebox.showinfo("Complete",
                                    f"Measurements written to:\n{csv_path}")
            except Exception as exc:
                self._log(f"ERROR: {exc}\n{traceback.format_exc()}")
                self.ml_status.config(text="Error", foreground="red")
                messagebox.showerror("Error", str(exc))
            finally:
                self._set_running(False)

        threading.Thread(target=_work, daemon=True).start()


# ==============================================================================
# CLI
# ==============================================================================

def _print_banner():
    print("\n" + "=" * 60)
    print("GBIF HERBARIA DATA PIPELINE -- CLI")
    print("=" * 60 + "\n")


def _cli_download():
    print("DOWNLOAD ONLY\n" + "-" * 60)
    species = input("\nSpecies name (e.g. 'Megathyrsus maximus'): ").strip()
    if not species:
        print("ERROR: Species name required"); return
    try:
        tk_ = resolve_species(species)
        dk = trigger_download(_build_gbif_queries(tk_), "DWCA",
                              GBIF_USER, GBIF_PASSWORD, GBIF_EMAIL)
        print(f"Download key: {dk}\nWaiting for GBIF (5-30 min)...")
        zf = wait_and_download(dk)
        folder = extract_archive(zf, f"./data_{dk}")
        print(f"\nDownload complete! Location: {folder}\n")
    except Exception as e:
        print(f"ERROR: {e}\n")


def _cli_prepare():
    print("PREPARE ONLY (CLEANING)\n" + "-" * 60)
    data_path = input("\nData folder or ZIP path: ").strip()
    if not data_path:
        print("ERROR: Path required"); return
    if os.path.isfile(data_path) and data_path.endswith(".zip"):
        print("\nExtracting ZIP...")
        dest = Path(data_path).stem
        try:
            with zipfile.ZipFile(data_path, "r") as zf:
                zf.extractall(dest)
            data_path = dest
            print(f"Extracted to: {data_path}")
        except Exception as e:
            print(f"ERROR extracting ZIP: {e}\n"); return
    if not os.path.isdir(data_path):
        print("ERROR: Invalid folder path\n"); return
    print("\nPhase options:")
    print("  1 -- Phase 1 & 2 (full cleaning)")
    print("  2 -- Phase 1 only")
    print("  3 -- Phase 2 only")
    choice = input("\nSelect (1-3): ").strip()
    occ = os.path.join(data_path, "occurrence.txt")
    mul = os.path.join(data_path, "multimedia.txt")
    try:
        if choice in ("1", "2"):
            if not os.path.exists(occ) or not os.path.exists(mul):
                print("ERROR: occurrence.txt or multimedia.txt not found\n"); return
            phase_1_clean_and_merge(occ, mul, exclude_taxa=DEFAULT_EXCLUDE_TAXA,
                                    coordinate_precision=PRECISION_RELAXED)
            print("Phase 1 complete! Edit GBIFdownload_inspectFlags.csv in Excel.\n")
            if choice == "1" and input("Proceed to Phase 2? (y/n): ").strip().lower() == "y":
                phase_2_finalize_dataset("GBIFdownload_inspectFlags.csv")
        elif choice == "3":
            phase_2_finalize_dataset("GBIFdownload_inspectFlags.csv")
        else:
            print("ERROR: Invalid selection\n")
    except Exception as e:
        print(f"ERROR: {e}\n")


def _cli_full():
    print("FULL WORKFLOW (DOWNLOAD + PREPARE)\n" + "-" * 60)
    species = input("\nSpecies name (e.g. 'Megathyrsus maximus'): ").strip()
    if not species:
        print("ERROR: Species name required"); return
    try:
        tk_ = resolve_species(species)
        dk = trigger_download(_build_gbif_queries(tk_), "DWCA",
                              GBIF_USER, GBIF_PASSWORD, GBIF_EMAIL)
        print(f"Download key: {dk}\nWaiting for GBIF (5-30 min)...")
        zf = wait_and_download(dk)
        folder = extract_archive(zf, f"./data_{dk}")
        occ = os.path.join(folder, "occurrence.txt")
        mul = os.path.join(folder, "multimedia.txt")
        phase_1_clean_and_merge(occ, mul, exclude_taxa=DEFAULT_EXCLUDE_TAXA,
                                coordinate_precision=PRECISION_RELAXED)
        print("Edit GBIFdownload_inspectFlags.csv in Excel before continuing.")
        input("\nPress Enter after editing the CSV...")
        phase_2_finalize_dataset("GBIFdownload_inspectFlags.csv")
        print("\n" + "=" * 60 + "\nWORKFLOW COMPLETE!\n" + "=" * 60)
        print("\nOutput files:")
        print("  master_cleaned.csv     <- Use for ImageJ measurements")
        print("  removed_duplicates.csv <- Backup records\n")
    except Exception as e:
        print(f"ERROR: {e}\n")


def _cli_download_media():
    print("DOWNLOAD VOUCHER MEDIA\n" + "-" * 60)
    csv_path = input("\nCSV file path: ").strip()
    if not csv_path or not os.path.isfile(csv_path):
        print("ERROR: Valid CSV path required"); return
    media_dir = input(f"\nMedia folder [{MEDIA_DIR}]: ").strip()
    media_dir = media_dir or None
    try:
        download_media(csv_path, media_dir=media_dir)
    except Exception as e:
        print(f"ERROR: {e}\n")


def _cli_measure():
    """CLI auto-measurement."""
    if not ML_AVAILABLE:
        print("ERROR: ML dependencies not installed.")
        print("Run:  pip install torch torchvision transformers accelerate pillow\n")
        return
    print("AUTO-MEASUREMENT\n" + "-" * 60)
    csv_path = input("\nCSV file path: ").strip()
    if not csv_path or not os.path.isfile(csv_path):
        print("ERROR: Valid CSV path required"); return
    media_dir = input(f"\nMedia folder [{MEDIA_DIR}]: ").strip()
    media_dir = media_dir or None
    mgr = ModelManager()
    model_id = input(f"\nModel ID [{mgr.default_model()}]: ").strip()
    model_id = model_id or mgr.default_model()
    try:
        if not mgr.is_downloaded(model_id):
            print(f"Model not cached. Downloading {model_id}...")
            mgr.download_model(model_id)
        mgr.load(model_id)
        engine = MeasurementEngine(mgr)
        engine.measure_csv(csv_path, media_dir=media_dir)
        mgr.unload()
    except Exception as e:
        print(f"ERROR: {e}\n")


def cli_main():
    _print_banner()
    print("Select workflow:")
    print("  1 -- Download only")
    print("  2 -- Prepare only (clean existing data)")
    print("  3 -- Full workflow (download + prepare)")
    print("  4 -- Download voucher media")
    print("  5 -- Auto-measure (ML)")
    choice = input("\nChoice (1-5): ").strip()
    {"1": _cli_download, "2": _cli_prepare, "3": _cli_full,
     "4": _cli_download_media, "5": _cli_measure}.get(
        choice, lambda: print("ERROR: Invalid choice\n"))()


def _cleaner_cli():
    if len(sys.argv) < 3:
        print("Usage:")
        print("  python gbif_pipeline.py cleaner 1 <occurrence> <multimedia>"
              " [output] [--strict] [--no-precision]")
        print("  python gbif_pipeline.py cleaner 2 <inspected_csv>"
              " [master] [duplicates]")
        sys.exit(1)
    phase = sys.argv[2]
    if phase == "1":
        if len(sys.argv) < 5:
            print("Phase 1 requires: <occurrence_file> <multimedia_file>"); sys.exit(1)
        prec = (PRECISION_NONE if "--no-precision" in sys.argv
                else PRECISION_STRICT if "--strict" in sys.argv
                else PRECISION_RELAXED)
        out = next((a for a in sys.argv[5:] if not a.startswith("-")),
                   "GBIFdownload_inspectFlags.csv")
        phase_1_clean_and_merge(sys.argv[3], sys.argv[4],
                                output_csv=out, coordinate_precision=prec)
    elif phase == "2":
        if len(sys.argv) < 4:
            print("Phase 2 requires: <inspected_csv>"); sys.exit(1)
        phase_2_finalize_dataset(
            sys.argv[3],
            final_master=sys.argv[4] if len(sys.argv) > 4 else "master_cleaned.csv",
            final_duplicates=sys.argv[5] if len(sys.argv) > 5 else "removed_duplicates.csv")
    else:
        print(f"Invalid phase '{phase}' -- must be 1 or 2"); sys.exit(1)


def _measure_cli():
    """python gbif_pipeline.py measure <csv> [model_id]"""
    if not ML_AVAILABLE:
        print("ERROR: ML dependencies not installed.")
        print("Run:  pip install torch torchvision transformers accelerate pillow")
        sys.exit(1)
    if len(sys.argv) < 3:
        print("Usage: python gbif_pipeline.py measure <csv> [model_id]"); sys.exit(1)
    csv_path = sys.argv[2]
    mgr = ModelManager()
    model_id = sys.argv[3] if len(sys.argv) > 3 else mgr.default_model()
    if not mgr.is_downloaded(model_id):
        print(f"Downloading {model_id}...")
        mgr.download_model(model_id)
    mgr.load(model_id)
    engine = MeasurementEngine(mgr)
    engine.measure_csv(csv_path)
    mgr.unload()


# ==============================================================================
# Entry point
# ==============================================================================

def main():
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "cleaner":
            _cleaner_cli(); return
        if cmd == "measure":
            _measure_cli(); return

    print("\n" + "=" * 60)
    print("GBIF HERBARIA DATA PIPELINE")
    print("=" * 60)
    print("\nSelect interface:")
    print("  1 -- GUI (graphical, recommended)")
    print("  2 -- CLI (command line)")
    choice = input("\nChoice (1 or 2): ").strip()
    if choice == "1":
        root = tk.Tk()
        GBIFPipelineGUI(root)
        root.mainloop()
    elif choice == "2":
        cli_main()
    else:
        print("ERROR: Invalid choice\n")


if __name__ == "__main__":
    main()