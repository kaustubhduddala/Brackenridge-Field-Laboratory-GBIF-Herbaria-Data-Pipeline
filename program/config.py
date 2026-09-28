import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
INSPECT_CSV = DATA_DIR / "GBIFdownload_inspectFlags.csv"
MASTER_CSV = DATA_DIR / "master_cleaned.csv"
DUPLICATES_CSV = DATA_DIR / "removed_duplicates.csv"
MEASUREMENTS_CSV = DATA_DIR / "measurements.csv"
RESPONSES_DIR = DATA_DIR / "responses"
LOG_FILE = DATA_DIR / "pipeline.log"
SETTINGS_FILE = BASE_DIR / "settings.json"

LMSTUDIO_URL = os.environ.get("LMSTUDIO_URL", "http://localhost:1234/v1")
LMSTUDIO_MODEL = os.environ.get("LMSTUDIO_MODEL", "")
MIN_CONTEXT_LENGTH = 16384
DEFAULT_TOKEN_LIMIT = 8000
THINKING_CHOICES = {"Model default": None, "Low": "low", "Off": "none"}
MAX_IMAGE_SIDE = 2560
REQUEST_TIMEOUT_S = 900
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".gif"}
HEADERS = {"User-Agent": "GBIF-HerbariaPipeline/1.0 (research use; Python requests)"}


def _load_credentials():
    creds_file = Path.home() / "credentials.json"
    file_creds = {}
    if creds_file.exists():
        try:
            file_creds = json.loads(creds_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return tuple(os.environ.get(f"GBIF_{key.upper()}", file_creds.get(key, "")) for key in ("user", "password", "email"))


GBIF_USER, GBIF_PASSWORD, GBIF_EMAIL = _load_credentials()


def has_gbif_credentials():
    return bool(GBIF_USER and GBIF_PASSWORD and GBIF_EMAIL)


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings):
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except OSError:
        pass


BAD_GEOSPATIAL_ISSUES = []
INSPECTION_ISSUES = [
    "COUNTRY_MISMATCH",
    "RECORDED_DATE_MISMATCH",
    "RECORDED_DATE_INVALID",
    "RECORDED_DATE_UNLIKELY",
    "OCCURRENCE_STATUS_UNPARSABLE",
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
    "MULTIMEDIA_DATE_INVALID",
]

MEDIA_COLUMNS = ["identifier", "references_multimedia", "references_occurrence", "references", "occurrenceID"]
LINK_COLUMNS = ["identifier", "references_multimedia", "references", "occurrenceID",
                "bibliographicCitation", "associatedReferences"]
MEDIA_EVIDENCE_COLUMNS = ["mediaType", "type_multimedia", "identifier", "format", "references_multimedia"]
MEASUREMENT_COLUMNS = ["Panicle length (cm)", "Leaf width (cm)", "Seed length (cm)"]
MULTIMEDIA_RENAMES = {
    "references": "references_multimedia",
    "publisher": "publisher_multimedia",
    "license": "license_multimedia",
    "rightsHolder": "rightsHolder_multimedia",
}

PRECISION_NONE = "none"
PRECISION_RELAXED = "relaxed"
PRECISION_STRICT = "strict"

BASE_FILTERS = (("BASIS_OF_RECORD", "PRESERVED_SPECIMEN"),)
PRESETS = {
    "G064 (Default)": {
        "species": "Megathyrsus maximus",
        "precision": PRECISION_NONE,
        "exclude_taxa": [],
        "bad_issues": [],
        "inspect_issues": INSPECTION_ISSUES,
        "filters": BASE_FILTERS,
    },
    "G024": {
        "species": "Megathyrsus maximus",
        "precision": PRECISION_NONE,
        "exclude_taxa": [],
        "bad_issues": [],
        "inspect_issues": INSPECTION_ISSUES,
        "filters": BASE_FILTERS + (("OCCURRENCE_STATUS", "PRESENT"),),
    },
    "Custom": {
        "species": "",
        "precision": PRECISION_NONE,
        "exclude_taxa": [],
        "bad_issues": [],
        "inspect_issues": INSPECTION_ISSUES,
        "filters": BASE_FILTERS,
    },
}
