import csv
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup 

import numpy as np
import pandas as pd
import requests
import urllib3
from pygbif import occurrences

from program.analysis import DATA_DIR, DEFAULT_BASE_URL, DEFAULT_MODEL, MEDIA_DIR, analyze_media
from program.analysis import OUTPUT_CSV as MEASUREMENTS_CSV

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, scrolledtext, ttk
except ImportError:
    tk = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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

INSPECT_CSV = DATA_DIR / "GBIFdownload_inspectFlags.csv"
MASTER_CSV = DATA_DIR / "master_cleaned.csv"
DUPLICATES_CSV = DATA_DIR / "removed_duplicates.csv"

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
URL_RE = re.compile(r"https?://[^\s;,|]+")
LINK_RE = re.compile(r"(?:https?://|ftp://|doi:)[^\s;,|]+")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
HEADERS = {"User-Agent": "GBIF-HerbariaPipeline/1.0 (research use; Python requests)"}

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

COUNTRY_CENTROIDS = {
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

INSTITUTIONS = {
    "Rio de Janeiro Botanical Garden": (-22.9711, -43.2265),
    "Kew Gardens": (51.4775, -0.2953),
    "Field Museum": (41.8658, -87.6167),
    "New York Botanical Garden": (40.7829, -73.9654),
    "Smithsonian NMNH": (38.9072, -77.0369),
    "MNHN Paris": (48.8441, 2.3620),
    "Berlin Botanical Garden": (52.4559, 13.3089),
    "Royal Botanic Garden Sydney": (-33.8688, 151.2093),
    "Singapore Botanic Gardens": (1.3138, 103.8159),
    "Bogor Botanical Garden": (-6.6000, 106.7994),
    "UNAM Botanical Garden": (18.9211, -99.2357),
    "Brasilia Botanical Garden": (-15.8711, -47.8825),
    "Curitiba Botanical Garden": (-25.4428, -49.2375),
    "Herbarium Friburguense": (-22.4667, -42.9833),
    "London": (51.4667, -0.3000),
    "Academy of Natural Sciences Philadelphia": (39.9526, -75.1652),
    "Munich Botanical Garden": (48.1639, 11.5028),
    "Basel Botanical Garden": (47.5580, 7.5839),
    "Naturalis Leiden": (52.3728, 4.9083),
    "CJBG Geneva": (46.2276, 6.1464),
}

LAND_BOXES = {
    "South America": (-56, 13, -82, -34),
    "North America": (7, 84, -170, -52),
    "Africa": (-35, 38, -18, 52),
    "Europe": (-11, 72, -12, 45),
    "Asia": (-12, 75, 25, 180),
    "Australia": (-48, -10, 112, 155),
    "New Zealand": (-47, -34, 165, 179),
    "Southeast Asia": (0, 22, 95, 142),
    "Indonesia": (-12, 6, 94, 141),
    "Philippines": (4, 21, 117, 127),
    "East Africa": (-8, -1, 29, 41),
}
ISLAND_TOLERANCE_KM = 1500


def _parse_issue_list(raw):
    items = raw if isinstance(raw, (list, tuple, set)) else re.split(r"[,;\n]", str(raw or ""))
    cleaned = (str(item).strip().strip("\"'").strip() for item in items)
    return list(dict.fromkeys(item for item in cleaned if item))


def _insert_after_gbif(df, name, values):
    df = df.drop(columns=name, errors="ignore").copy()
    position = df.columns.get_loc("gbifID") + 1 if "gbifID" in df.columns else 0
    df.insert(position, name, values)
    return df


def _read_csv(path, log=print):
    try:
        return pd.read_csv(path, dtype=str, low_memory=False)
    except pd.errors.ParserError:
        log("Standard CSV parser failed; retrying with the Python engine.")
        return pd.read_csv(path, dtype=str, engine="python", on_bad_lines="warn")


def _read_dwca(path):
    return pd.read_csv(path, sep="\t", low_memory=False, quoting=csv.QUOTE_NONE, dtype=str)


def _save_csv(df, path):
    df.to_csv(path, index=False, quoting=csv.QUOTE_ALL)


def _first_url_series(df, columns):
    result = pd.Series(pd.NA, index=df.index, dtype="object")
    for column in columns:
        if column in df.columns:
            result = result.fillna(df[column].astype("object").str.extract(f"({URL_RE.pattern})", expand=False))
    return result.fillna("")


def _find_media_url(row):
    for column in ("media", *LINK_COLUMNS):
        value = row.get(column)
        if isinstance(value, str):
            match = URL_RE.search(value)
            if match:
                return match.group()
    return None


def _all_links(row):
    text = " ".join(str(v) for v in row.dropna())
    return ", ".join(dict.fromkeys(LINK_RE.findall(text)))


def _issue_sets(df):
    if "issue" not in df.columns:
        return pd.Series([frozenset()] * len(df), index=df.index, dtype="object")
    return df["issue"].fillna("").map(lambda s: frozenset(filter(None, re.split(r"[;,\s]+", s))))


def resolve_species(scientific_name, log=print):
    log(f"Looking up GBIF taxon key for '{scientific_name}'")
    resp = requests.get("https://api.gbif.org/v1/species/match", params={"name": scientific_name}, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if result.get("matchType") in ("EXACT", "FUZZY") and "usageKey" in result:
        log(f"Matched '{result.get('scientificName', scientific_name)}' ({result['matchType'].lower()}), "
            f"taxon key {result['usageKey']}")
        return result["usageKey"]
    raise ValueError(f"No reliable GBIF match for '{scientific_name}'. GBIF response: {result}")


def build_predicate(taxon_key, preset_name=None):
    filters = PRESETS.get(preset_name, {}).get("filters", BASE_FILTERS)
    clauses = [("TAXON_KEY", str(taxon_key)), *filters]
    return {"type": "and", "predicates": [{"type": "equals", "key": k, "value": v} for k, v in clauses]}


def wait_and_download(download_key, output_dir=DATA_DIR, log=print):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    meta = occurrences.download_meta(download_key)
    while meta["status"] in ("RUNNING", "PREPARING"):
        log(f"GBIF is preparing the file ({meta['status'].lower()}); checking again in 30 s")
        time.sleep(30)
        meta = occurrences.download_meta(download_key)
    if meta["status"] != "SUCCEEDED":
        raise RuntimeError(f"GBIF download failed with status {meta['status']}")
    log("File is ready; downloading ZIP")
    occurrences.download_get(download_key, path=str(output_dir))
    return output_dir / f"{download_key}.zip"


def extract_archive(zip_path, extract_dir, log=print):
    extract_dir = Path(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir)
    log(f"Extracted to {extract_dir}")
    return str(extract_dir)


def download_from_doi_link(doi_text, output_dir=DATA_DIR, log=print):
    doi_text = doi_text.strip()
    if doi_text.lower().startswith("doi:"):
        doi_text = doi_text[4:].strip()
    if not doi_text:
        raise ValueError("No DOI provided")
    url = doi_text if doi_text.startswith(("http://", "https://")) else "https://doi.org/" + doi_text
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    log(f"Resolving {url}")
    with requests.get(url, allow_redirects=True, stream=True, timeout=30, headers=HEADERS) as resp:
        final_url = resp.url
        parsed = urlparse(final_url)
        if "gbif.org" not in parsed.netloc.lower():
            raise ValueError(f"DOI did not resolve to a GBIF download: {final_url}")
        content_type = resp.headers.get("content-type", "").lower()
        if final_url.lower().endswith(".zip") or content_type.startswith(("application/zip", "application/octet-stream")):
            out_path = output_dir / (Path(parsed.path).name or "gbif_download.zip")
            with open(out_path, "wb") as out_file:
                for chunk in resp.iter_content(1 << 16):
                    out_file.write(chunk)
            return out_path

    match = re.search(r"/download/([^/?#&]+)", final_url)
    if match:
        return wait_and_download(match.group(1), output_dir, log)
    raise ValueError("DOI did not resolve to a downloadable ZIP or a GBIF download key")


def download_dataset(species="", doi_text="", preset_name=None, log=print):
    if doi_text:
        archive = download_from_doi_link(doi_text, log=log)
    else:
        if not (GBIF_USER and GBIF_PASSWORD and GBIF_EMAIL):
            raise RuntimeError("GBIF credentials are missing. Set GBIF_USER, GBIF_PASSWORD and GBIF_EMAIL, "
                               "or add user, password and email to ~/credentials.json.")
        taxon_key = resolve_species(species, log)
        log("Submitting download request to GBIF")
        result = occurrences.download(build_predicate(taxon_key, preset_name), format="DWCA",
                                      user=GBIF_USER, pwd=GBIF_PASSWORD, email=GBIF_EMAIL)
        download_key = result[0] if isinstance(result, (tuple, list)) else result
        log(f"Download key {download_key}. GBIF usually takes 5 to 30 minutes.")
        archive = wait_and_download(download_key, log=log)
    return extract_archive(archive, DATA_DIR / Path(archive).stem, log)


def prepare_folder(path, log=print):
    if not str(path).strip():
        raise ValueError("Select a data folder or ZIP file")
    path = Path(path)
    if path.is_file() and path.suffix.lower() == ".zip":
        return extract_archive(path, DATA_DIR / path.stem, log)
    if path.is_dir():
        return str(path)
    raise ValueError(f"Not a folder or ZIP file: {path}")


def find_dwca_files(folder):
    folder = Path(folder)
    occurrence = list(folder.rglob("occurrence.txt"))
    multimedia = list(folder.rglob("multimedia.txt"))
    if not occurrence or not multimedia:
        raise FileNotFoundError(f"occurrence.txt or multimedia.txt not found in {folder}")
    for occ in occurrence:
        for mul in multimedia:
            if occ.parent == mul.parent:
                return str(occ), str(mul)
    return str(occurrence[0]), str(multimedia[0])


def _haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(v, dtype=float)) for v in (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def clean_coordinates(df, centroid_radius_km=1.0, institution_radius_km=0.5, log=print):
    reasons = pd.Series("", index=df.index, dtype="object")
    if not {"decimalLatitude", "decimalLongitude"} <= set(df.columns):
        log("  Coordinate columns not found; skipping coordinate tests")
        return reasons

    lat = pd.to_numeric(df["decimalLatitude"], errors="coerce")
    lon = pd.to_numeric(df["decimalLongitude"], errors="coerce")
    valid = lat.notna() & lon.notna()
    codes = (df["countryCode"] if "countryCode" in df.columns else pd.Series("", index=df.index))
    codes = codes.fillna("").astype(str).str.strip().str.upper()
    centroid = codes.map(lambda c: COUNTRY_CENTROIDS.get(c, (np.nan, np.nan)))
    to_centroid = pd.Series(_haversine_km(lat, lon, centroid.str[0], centroid.str[1]), index=df.index)

    on_centroid = valid & (to_centroid < centroid_radius_km)
    reasons[on_centroid] = "Country centroid: " + codes[on_centroid]
    log(f"  Centroids: {int(on_centroid.sum())} records on country centroids")

    on_land = pd.Series(False, index=df.index)
    for lat_min, lat_max, lon_min, lon_max in LAND_BOXES.values():
        on_land |= lat.between(lat_min, lat_max) & lon.between(lon_min, lon_max)
    at_sea = valid & ~on_land & ~(to_centroid < ISLAND_TOLERANCE_KM) & (reasons == "")
    reasons[at_sea] = "Coordinates in ocean"
    log(f"  Seas: {int(at_sea.sum())} records in the ocean")

    near_institution = pd.Series(False, index=df.index)
    for ilat, ilon in INSTITUTIONS.values():
        near_institution |= pd.Series(_haversine_km(lat, lon, ilat, ilon) < institution_radius_km, index=df.index)
    near_institution &= valid & (reasons == "")
    reasons[near_institution] = "Near biodiversity institution"
    log(f"  Institutions: {int(near_institution.sum())} records near biodiversity institutions")
    return reasons


def phase_1_clean_and_merge(occurrence_file, multimedia_file, output_csv=None, removed_csv=None,
                            exclude_taxa=None, coordinate_precision=PRECISION_RELAXED,
                            bad_geospatial_issues=None, inspection_issues=None, log=print):
    output_csv = Path(output_csv or INSPECT_CSV)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    removed_csv = Path(removed_csv or output_csv.with_name("GBIFdownload_removed.csv"))
    missing_csv = output_csv.with_name("Missing_Media.csv")
    exclude_taxa = list(exclude_taxa or [])
    bad_issues = frozenset(BAD_GEOSPATIAL_ISSUES if bad_geospatial_issues is None else bad_geospatial_issues)
    inspect_issues = frozenset(INSPECTION_ISSUES if inspection_issues is None else inspection_issues)
    removed_parts = []

    def remove(df, mask, reason, label):
        mask = pd.Series(mask, index=df.index).fillna(False).astype(bool)
        if mask.any():
            removed_parts.append(_insert_after_gbif(df[mask], "removal_reason", reason))
        df = df[~mask]
        log(f"  {label}: removed {int(mask.sum())}, {len(df)} remaining")
        return df

    log("Phase 1: cleaning and merging")
    occ = _read_dwca(occurrence_file).dropna(axis=1, how="all")
    media = _read_dwca(multimedia_file).rename(columns=MULTIMEDIA_RENAMES).dropna(axis=1, how="all")
    log(f"Loaded {len(occ)} occurrence rows and {len(media)} multimedia rows")
    df = occ.merge(media, on="gbifID", how="left", suffixes=("_occurrence", "_multimedia"))
    df = _insert_after_gbif(df, "media", _first_url_series(df, MEDIA_COLUMNS))
    log(f"Merged dataset: {len(df)} rows")

    evidence = [c for c in MEDIA_EVIDENCE_COLUMNS if c in df.columns]
    missing_count = 0
    if evidence:
        has_media = df[evidence].fillna("").astype(str).apply(lambda s: s.str.strip().ne("")).any(axis=1)
        missing = df[~has_media]
        df = df[has_media]
        missing_count = len(missing)
        if missing_count:
            links = [_all_links(row) for _, row in missing.iterrows()]
            out = _insert_after_gbif(missing.drop(columns="media"), "media_links", links)
            out = _insert_after_gbif(out, "removal_reason", "No media evidence")
            _save_csv(out.dropna(axis=1, how="all"), missing_csv)
            log(f"  Records without media saved to {missing_csv}")
        log(f"  Media: {missing_count} rows without media, {len(df)} remaining")
    if not missing_count and missing_csv.exists():
        missing_csv.unlink()

    if coordinate_precision != PRECISION_NONE and {"decimalLatitude", "decimalLongitude"} <= set(df.columns):
        strict = coordinate_precision == PRECISION_STRICT
        pattern = r"\.\d{3,}" if strict else r"\.\d+"
        label = ">=3 decimal places" if strict else ">=1 decimal place"
        precise = (df["decimalLatitude"].fillna("").str.contains(pattern, regex=True)
                   & df["decimalLongitude"].fillna("").str.contains(pattern, regex=True))
        df = remove(df, ~precise, f"Coordinate precision below threshold ({label})", f"Precision {label}")

    for column in ("scientificName", "infraspecificEpithet", "verbatimScientificName"):
        if exclude_taxa and column in df.columns:
            df = remove(df, df[column].isin(exclude_taxa), "Excluded taxon: " + df[column].fillna(""),
                        f"Excluded taxa ({column})")

    log("Cleaning coordinates")
    reasons = clean_coordinates(df, log=log)
    df = remove(df, reasons != "", reasons, "Coordinate tests")

    if bad_issues:
        found = _issue_sets(df).map(lambda issues: sorted(issues & bad_issues))
        df = remove(df, found.map(bool), "Bad geospatial issue: " + found.map(", ".join), "Bad issues")

    link_columns = [c for c in LINK_COLUMNS if c in df.columns]
    if link_columns:
        blank = df[link_columns].fillna("").astype(str).apply(lambda s: s.str.strip().eq("")).all(axis=1)
        df = remove(df, blank, "No valid reference links in any link column", "Reference links")

    df = df.copy()
    df["inspect_flag"] = _issue_sets(df).map(lambda issues: bool(issues & inspect_issues))
    log(f"  Flagged for inspection: {int(df['inspect_flag'].sum())}")
    if "Action" not in df.columns:
        df = _insert_after_gbif(df, "Action", "")
    df = df.dropna(axis=1, how="all")
    _save_csv(df, output_csv)

    if removed_parts:
        removed = pd.concat(removed_parts, ignore_index=True).dropna(axis=1, how="all")
        _save_csv(removed, removed_csv)
        log(f"Removed records saved to {removed_csv} ({len(removed)} rows)")
        for reason, count in removed["removal_reason"].astype(str).str.split(":").str[0].value_counts().items():
            log(f"  {reason}: {count}")
    elif removed_csv.exists():
        removed_csv.unlink()

    log(f"Phase 1 complete: {len(df)} rows saved to {output_csv}")
    return str(output_csv)


def phase_2_finalize_dataset(inspected_csv=INSPECT_CSV, final_master=None, final_duplicates=None, log=print):
    final_master = Path(final_master or MASTER_CSV)
    final_duplicates = Path(final_duplicates or DUPLICATES_CSV)
    final_master.parent.mkdir(parents=True, exist_ok=True)
    log("Phase 2: finalizing and deduplicating")
    df = _read_csv(inspected_csv, log)
    log(f"Loaded {len(df)} rows from {inspected_csv}")

    if "Action" in df.columns:
        keep = df["Action"].fillna("").str.strip().str.lower() != "remove"
        df = df[keep]
        log(f"  Removed {int((~keep).sum())} rows marked Remove")

    if "gbifID" in df.columns:
        duplicates = df[df.duplicated("gbifID", keep="first")]
        master = df.groupby("gbifID", sort=False, dropna=False).first().reset_index()[list(df.columns)]
        log(f"  Merged {len(duplicates)} duplicate rows into their first occurrence")
    else:
        duplicates = df.iloc[0:0]
        master = df.copy()
        log("  gbifID column not found; skipping deduplication")

    media = master["media"].fillna("") if "media" in master.columns else ""
    master = _insert_after_gbif(master, "media", media)
    for column in MEASUREMENT_COLUMNS:
        if column not in master.columns:
            master[column] = ""

    _save_csv(master.dropna(axis=1, how="all"), final_master)
    _save_csv(duplicates.dropna(axis=1, how="all"), final_duplicates)
    log(f"  Duplicates saved to {final_duplicates}")
    log(f"Phase 2 complete: {len(master)} unique records saved to {final_master}")
    return str(final_master), str(final_duplicates)


def run_cleaning(folder=None, steps="both", confirm=None, log=print, **options):
    if steps in ("both", "phase1"):
        occurrence, multimedia = find_dwca_files(folder)
        phase_1_clean_and_merge(occurrence, multimedia, log=log, **options)
        log(f"Review {INSPECT_CSV} now. Type Remove in the Action column to drop a record.")
        if steps == "phase1" or (confirm and not confirm()):
            return None
    master, _ = phase_2_finalize_dataset(INSPECT_CSV, log=log)
    return master


def _request_with_backoff(url, max_retries=3, log=print):
    delay = 5
    error = ""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=30, verify=False, headers=HEADERS)
        except requests.exceptions.RequestException as exc:
            error = str(exc)
        else:
            if resp.status_code != 429 and resp.status_code < 500:
                resp.raise_for_status()
                return resp
            error = f"HTTP {resp.status_code}"
        if attempt < max_retries:
            log(f"    {error}; retrying in {delay} s")
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"Request failed after {max_retries} attempts: {error}")


def _has_image_suffix(url):
    return urlparse(url).path.lower().endswith(IMAGE_SUFFIXES)


def _extract_image_from_json(json_text, log=print):
    try:
        data = json.loads(json_text)
    except (json.JSONDecodeError, TypeError) as exc:
        log(f"    IIIF JSON parse failed: {exc}")
        return None
    if not isinstance(data, dict):
        return None

    candidates = []
    for canvas in data.get("items", []):
        for page in canvas.get("items", []):
            for annotation in page.get("items", []):
                body = annotation.get("body", {})
                body = body[0] if isinstance(body, list) and body else body
                if isinstance(body, dict):
                    candidates.append(body.get("id") or body.get("@id"))
    for sequence in data.get("sequences", []):
        for canvas in sequence.get("canvases", []):
            for image in canvas.get("images", []):
                resource = image.get("resource", {})
                candidates.append(resource.get("@id") or resource.get("id"))
    candidates += [data.get(key) for key in ("imageUri", "imageUrl", "identifier")]
    return next((c for c in candidates if isinstance(c, str) and c.startswith("http")), None)


def _resolve_image_from_html(resp, page_url, log=print):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise ValueError("HTML page returned; install beautifulsoup4 to find images in web pages")
    soup = BeautifulSoup(resp.text, "html.parser")

    meta = (soup.find("meta", property="og:image")
            or soup.find("meta", attrs={"name": "twitter:image"})
            or soup.find("link", rel="image_src"))
    href = meta and (meta.get("content") or meta.get("href"))
    if href and href.strip():
        return urljoin(page_url, href.strip())

    manifest = soup.find("link", rel="alternate", type="application/ld+json")
    if manifest and manifest.get("href"):
        try:
            manifest_resp = _request_with_backoff(urljoin(page_url, manifest["href"].strip()), log=log)
            image = _extract_image_from_json(manifest_resp.text, log)
            if image:
                return image
        except Exception as exc:
            log(f"    IIIF manifest failed: {exc}")

    for tag in soup.find_all("img", src=True):
        src = urljoin(page_url, tag["src"].strip())
        if not _has_image_suffix(src):
            continue
        try:
            if int(tag.get("width", 999)) < 100 or int(tag.get("height", 999)) < 100:
                continue
        except (TypeError, ValueError):
            pass
        return src

    for tag in soup.find_all("a", href=True):
        link = urljoin(page_url, tag["href"].strip())
        if _has_image_suffix(link):
            return link
    return None


def _detect_extension(content_type, url=""):
    content_type = content_type.lower()
    for key, ext in (("jpeg", "jpg"), ("jpg", "jpg"), ("png", "png"), ("tif", "tif"), ("gif", "gif"), ("webp", "webp")):
        if key in content_type:
            return ext
    path = urlparse(url).path.lower()
    for ext in ("jpg", "jpeg", "png", "tif", "tiff", "gif", "webp"):
        if path.endswith(f".{ext}"):
            return {"jpeg": "jpg", "tiff": "tif"}.get(ext, ext)
    return "jpg"


def _fetch_image(url, stem, log=print):
    resp = _request_with_backoff(url, log=log)
    content_type = resp.headers.get("Content-Type", "").lower()
    if "json" in content_type:
        image_url = _extract_image_from_json(resp.text, log)
        if not image_url:
            raise ValueError("JSON response: no image URL found")
        log(f"    IIIF image: {image_url[:80]}")
        resp = _request_with_backoff(image_url, log=log)
    elif "text/html" in content_type:
        image_url = _resolve_image_from_html(resp, url, log)
        if not image_url:
            raise ValueError("HTML page has no recognizable image (it may need JavaScript)")
        log(f"    Image from page: {image_url[:80]}")
        resp = _request_with_backoff(image_url, log=log)

    content_type = resp.headers.get("Content-Type", "").lower()
    if "text/html" in content_type or "json" in content_type:
        raise ValueError(f"Final response is not an image (Content-Type: {content_type})")
    path = Path(f"{stem}.{_detect_extension(content_type, resp.url)}")
    path.write_bytes(resp.content)
    return path


def download_media(csv_path, media_dir=None, log=print, cancel_flag=None, progress=None):
    csv_path = Path(csv_path)
    media_dir = Path(media_dir or MEDIA_DIR)
    media_dir.mkdir(parents=True, exist_ok=True)
    failed_csv = csv_path.with_name(csv_path.stem + "_failed_media.csv")
    log(f"Downloading voucher images to {media_dir}")

    df = _read_csv(csv_path, log).reset_index(drop=True)
    df.columns = df.columns.str.strip()
    if "media_path" not in df.columns:
        df["media_path"] = ""
    df["media_path"] = df["media_path"].astype("object").fillna("")
    ids = df["gbifID"].fillna("").str.strip() if "gbifID" in df.columns else pd.Series("", index=df.index)
    repeat = ids.groupby(ids).cumcount()

    summary = {"total": len(df), "downloaded": 0, "skipped": 0, "failed": 0, "cancelled": False}
    failures = []

    def fail(idx, reason):
        df.at[idx, "media_path"] = ""
        failures.append((idx, reason))
        summary["failed"] += 1

    for idx in df.index:
        if cancel_flag and cancel_flag():
            summary["cancelled"] = True
            log(f"Cancelled at row {idx + 1} of {len(df)}")
            break
        if progress:
            progress(idx, len(df))
        if idx and idx % 25 == 0:
            _save_csv(df, csv_path)

        gbif_id = ids[idx]
        if not gbif_id:
            summary["skipped"] += 1
            continue
        label = gbif_id if repeat[idx] == 0 else f"{gbif_id}_{repeat[idx]}"
        existing = next(media_dir.glob(f"{label}.*"), None)
        if existing:
            df.at[idx, "media_path"] = str(existing)
            summary["skipped"] += 1
            continue

        url = _find_media_url(df.loc[idx])
        if not url:
            fail(idx, "No media link found in row")
            continue
        log(f"[{idx + 1}/{len(df)}] {label}: {url[:80]}")
        try:
            df.at[idx, "media_path"] = str(_fetch_image(url, media_dir / label, log))
            summary["downloaded"] += 1
        except Exception as exc:
            log(f"    Failed: {exc}")
            fail(idx, str(exc))

    _save_csv(df, csv_path)
    if failures:
        failed_df = _insert_after_gbif(df.loc[[i for i, _ in failures]], "download_failure_reason",
                                       [reason for _, reason in failures])
        _save_csv(failed_df, failed_csv)
        log(f"Failed downloads saved to {failed_csv} ({len(failed_df)} rows)")
    elif failed_csv.exists():
        failed_csv.unlink()

    if progress and not summary["cancelled"]:
        progress(len(df), len(df))
    log(f"Media download complete: {summary['downloaded']} downloaded, "
        f"{summary['skipped']} skipped, {summary['failed']} failed")
    return summary


def _set_entry(entry, value):
    entry.delete(0, "end")
    entry.insert(0, str(value))


def _set_text(widget, value):
    widget.delete("1.0", "end")
    widget.insert("1.0", value)


def _open_path(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


if tk:
    class IssueFilterDialog(tk.Toplevel):
        def __init__(self, parent, remove_issues, inspect_issues, font):
            super().__init__(parent)
            self.title("Issue filters")
            self.geometry("640x480")
            self.minsize(480, 360)
            self.transient(parent)
            self.result = None
            self.texts = []

            body = ttk.Frame(self, padding=12)
            body.pack(fill="both", expand=True)
            notebook = ttk.Notebook(body)
            notebook.pack(fill="both", expand=True)
            tabs = (
                ("Remove records", remove_issues, BAD_GEOSPATIAL_ISSUES,
                 "Records with any of these GBIF issue codes are removed in phase 1."),
                ("Flag for inspection", inspect_issues, INSPECTION_ISSUES,
                 "Records with any of these GBIF issue codes get inspect_flag set to True."),
            )
            for title, current, defaults, hint in tabs:
                frame = ttk.Frame(notebook, padding=10)
                ttk.Label(frame, text=f"{hint} One code per line.", style="Muted.TLabel",
                          wraplength=560, justify="left").pack(anchor="w")
                text = tk.Text(frame, height=14, wrap="word", font=font, relief="solid", borderwidth=1,
                               background="white", padx=6, pady=4)
                text.pack(fill="both", expand=True, pady=6)
                _set_text(text, "\n".join(current))
                buttons = ttk.Frame(frame)
                buttons.pack(fill="x")
                ttk.Button(buttons, text="Restore defaults",
                           command=lambda t=text, d=defaults: _set_text(t, "\n".join(d))).pack(side="left")
                ttk.Button(buttons, text="Clear", command=lambda t=text: _set_text(t, "")).pack(side="left", padx=6)
                notebook.add(frame, text=f"{title} ({len(current)})")
                self.texts.append(text)

            footer = ttk.Frame(body)
            footer.pack(fill="x", pady=(10, 0))
            ttk.Button(footer, text="Save filters", style="Accent.TButton", command=self._save).pack(side="right")
            ttk.Button(footer, text="Cancel", command=self.destroy).pack(side="right", padx=6)
            self.bind("<Escape>", lambda _: self.destroy())
            try:
                self.wait_visibility()
                self.grab_set()
            except tk.TclError:
                pass

        def _save(self):
            self.result = tuple(_parse_issue_list(t.get("1.0", "end")) for t in self.texts)
            self.destroy()

    class PipelineApp:
        COLORS = {
            "accent": "#2d6a4f", "accent_active": "#40916c", "bg": "#f4f6f4", "tab": "#e1e7e2",
            "muted": "#5b6760", "busy": "#1f5f8b", "ok": "#2d6a4f", "error": "#b42318",
        }

        def __init__(self, root):
            self.root = root
            self.cancel_event = threading.Event()
            self.running = False
            self.action_buttons = []
            self.bad_issues = list(BAD_GEOSPATIAL_ISSUES)
            self.inspect_issues = list(INSPECTION_ISSUES)

            root.title("GBIF Herbaria Data Pipeline")
            root.geometry("900x820")
            root.minsize(760, 640)
            root.protocol("WM_DELETE_WINDOW", self._on_close)
            self._apply_theme()
            self._build_ui()
            self._load_preset()

        def _apply_theme(self):
            c = self.COLORS
            base = tkfont.nametofont("TkDefaultFont")
            base.configure(size=10)
            tkfont.nametofont("TkTextFont").configure(size=10)
            self.fonts = {name: base.copy() for name in ("title", "bold", "small")}
            self.fonts["title"].configure(size=16, weight="bold")
            self.fonts["bold"].configure(weight="bold")
            self.fonts["small"].configure(size=9)
            self.fonts["mono"] = tkfont.nametofont("TkFixedFont").copy()
            self.fonts["mono"].configure(size=9)

            self.root.configure(background=c["bg"])
            style = ttk.Style()
            style.theme_use("clam")
            style.configure(".", background=c["bg"])
            style.configure("TNotebook", background=c["bg"], borderwidth=0)
            style.configure("TNotebook.Tab", padding=(16, 6), background=c["tab"])
            style.map("TNotebook.Tab", background=[("selected", c["bg"])], foreground=[("selected", c["accent"])])
            style.configure("TLabelframe", background=c["bg"], padding=10)
            style.configure("TLabelframe.Label", background=c["bg"], foreground=c["accent"], font=self.fonts["bold"])
            style.configure("Accent.TButton", foreground="white", background=c["accent"],
                            font=self.fonts["bold"], padding=(12, 5))
            style.map("Accent.TButton", background=[("disabled", "#a9b5ae"), ("active", c["accent_active"])],
                      foreground=[("disabled", "#eef1ef")])
            style.configure("Title.TLabel", font=self.fonts["title"], foreground=c["accent"])
            style.configure("Muted.TLabel", foreground=c["muted"], font=self.fonts["small"])
            style.configure("Horizontal.TProgressbar", background=c["accent"], troughcolor=c["tab"])

        def _build_ui(self):
            header = ttk.Frame(self.root, padding=(16, 12, 16, 6))
            header.pack(fill="x")
            ttk.Label(header, text="GBIF Herbaria Data Pipeline", style="Title.TLabel").pack(anchor="w")
            ttk.Label(header, text="Download, clean and measure herbarium specimen records. By Kaustubh Duddala",
                      style="Muted.TLabel").pack(anchor="w")

            status = ttk.Frame(self.root, padding=(16, 6, 16, 10))
            status.pack(side="bottom", fill="x")
            self.status_var = tk.StringVar(value="Ready")
            ttk.Label(status, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")
            self.cancel_btn = ttk.Button(status, text="Cancel", command=self._cancel, state="disabled")
            self.cancel_btn.pack(side="right")
            self.progress = ttk.Progressbar(status, length=240, mode="determinate")
            self.progress.pack(side="right", padx=10)

            panes = ttk.PanedWindow(self.root, orient="vertical")
            panes.pack(fill="both", expand=True, padx=12)
            self.notebook = ttk.Notebook(panes)
            self.notebook.add(self._build_download_tab(), text="1. Download")
            self.notebook.add(self._build_clean_tab(), text="2. Clean")
            self.notebook.add(self._build_media_tab(), text="3. Images")
            panes.add(self.notebook, weight=3)
            panes.add(self._build_console(panes), weight=2)

        def _build_console(self, parent):
            frame = ttk.Frame(parent, padding=(0, 8, 0, 0))
            bar = ttk.Frame(frame)
            bar.pack(fill="x", pady=(0, 4))
            ttk.Label(bar, text="Log", font=self.fonts["bold"]).pack(side="left")
            ttk.Button(bar, text="Clear", command=self._clear_console).pack(side="right")
            ttk.Button(bar, text="Open data folder", command=lambda: _open_path(DATA_DIR)).pack(side="right", padx=6)
            self.console = scrolledtext.ScrolledText(frame, height=10, state="disabled", wrap="word",
                                                     font=self.fonts["mono"], relief="solid", borderwidth=1,
                                                     background="white", padx=6, pady=4)
            self.console.pack(fill="both", expand=True)
            self.console.tag_configure("error", foreground=self.COLORS["error"])
            self.console.tag_configure("ok", foreground=self.COLORS["ok"])
            return frame

        def _section(self, parent, text, row, column=0, columnspan=1, padx=0):
            frame = ttk.LabelFrame(parent, text=text)
            frame.grid(row=row, column=column, columnspan=columnspan, sticky="nsew", pady=(0, 12), padx=padx)
            frame.columnconfigure(1, weight=1)
            return frame

        def _field(self, parent, row, label, value="", browse=None):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            entry = ttk.Entry(parent)
            entry.insert(0, str(value))
            entry.grid(row=row, column=1, sticky="ew", pady=3)
            if browse:
                ttk.Button(parent, text="Browse…", command=lambda: self._browse(entry, browse)).grid(
                    row=row, column=2, padx=(6, 0), pady=3)
            return entry

        def _hint(self, parent, row, text, column=0, columnspan=3):
            ttk.Label(parent, text=text, style="Muted.TLabel", wraplength=640, justify="left").grid(
                row=row, column=column, columnspan=columnspan, sticky="w", pady=(2, 4))

        def _action(self, parent, row, text, command, columnspan=3):
            frame = ttk.Frame(parent)
            frame.grid(row=row, column=0, columnspan=columnspan, sticky="w", pady=(6, 0))
            button = ttk.Button(frame, text=text, style="Accent.TButton", command=command)
            button.pack(side="left")
            status = ttk.Label(frame, style="Muted.TLabel")
            status.pack(side="left", padx=12)
            self.action_buttons.append(button)
            return status

        def _build_download_tab(self):
            tab = ttk.Frame(self.notebook, padding=14)
            tab.columnconfigure(0, weight=1)

            source = self._section(tab, "Data source", 0)
            ttk.Label(source, text="Preset").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
            self.preset_var = tk.StringVar(value=next(iter(PRESETS)))
            combo = ttk.Combobox(source, textvariable=self.preset_var, values=list(PRESETS), state="readonly", width=24)
            combo.grid(row=0, column=1, sticky="w", pady=3)
            combo.bind("<<ComboboxSelected>>", lambda _: self._load_preset())
            self.species_entry = self._field(source, 1, "Species")
            self.doi_entry = self._field(source, 2, "DOI (optional)")
            self._hint(source, 3, "Leave the DOI blank to request a new download for the species. "
                                  "Paste a GBIF download DOI to fetch an existing dataset.", column=1, columnspan=2)
            if GBIF_USER and GBIF_PASSWORD and GBIF_EMAIL:
                account = f"Signed in to GBIF as {GBIF_USER}."
            else:
                account = ("No GBIF credentials found. New downloads need GBIF_USER, GBIF_PASSWORD and GBIF_EMAIL "
                           "or ~/credentials.json; DOI downloads work without them.")
            self._hint(source, 4, account, column=1, columnspan=2)
            self.download_status = self._action(source, 5, "Download dataset", self._run_download)

            full = self._section(tab, "Full workflow", 1)
            self._hint(full, 0, "Download the dataset, run phase 1, pause so you can review the flagged CSV, "
                                "then finish with phase 2.")
            self.full_status = self._action(full, 1, "Run full workflow", self._run_full)
            return tab

        def _build_clean_tab(self):
            tab = ttk.Frame(self.notebook, padding=14)
            tab.columnconfigure((0, 1), weight=1, uniform="half")

            data = self._section(tab, "Input data", 0, columnspan=2)
            self.data_entry = self._field(data, 0, "Folder or ZIP")
            ttk.Button(data, text="Folder…", command=lambda: self._browse(
                self.data_entry, lambda: filedialog.askdirectory(title="Select data folder"))).grid(
                row=0, column=2, padx=(6, 0))
            ttk.Button(data, text="ZIP…", command=lambda: self._browse(
                self.data_entry, lambda: filedialog.askopenfilename(
                    title="Select ZIP file", filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")]))).grid(
                row=0, column=3, padx=(6, 0))
            self._hint(data, 1, f"Not needed for phase 2 only, which reads {INSPECT_CSV.name}.",
                       column=1, columnspan=3)

            steps = self._section(tab, "Steps", 1, padx=(0, 6))
            self.phase_var = tk.StringVar(value="both")
            for i, (label, value) in enumerate((("Phase 1, review, then phase 2", "both"),
                                                ("Phase 1 only: clean and merge", "phase1"),
                                                ("Phase 2 only: deduplicate and finalize", "phase2"))):
                ttk.Radiobutton(steps, text=label, variable=self.phase_var, value=value).grid(
                    row=i, column=0, sticky="w", pady=1)

            precision = self._section(tab, "Coordinate precision", 1, column=1, padx=(6, 0))
            self.precision_var = tk.StringVar(value=PRECISION_RELAXED)
            for i, (label, value) in enumerate((("Keep all coordinates", PRECISION_NONE),
                                                ("At least 1 decimal place", PRECISION_RELAXED),
                                                ("At least 3 decimal places", PRECISION_STRICT))):
                ttk.Radiobutton(precision, text=label, variable=self.precision_var, value=value).grid(
                    row=i, column=0, sticky="w", pady=1)

            filters = self._section(tab, "Filters", 2, columnspan=2)
            ttk.Button(filters, text="Edit issue filters…", command=self._open_issue_dialog).grid(
                row=0, column=0, sticky="w")
            self.issue_summary = ttk.Label(filters, style="Muted.TLabel")
            self.issue_summary.grid(row=0, column=1, sticky="w", padx=12)
            ttk.Label(filters, text="Excluded taxa, one name per line").grid(
                row=1, column=0, columnspan=2, sticky="w", pady=(10, 2))
            self.taxa_text = tk.Text(filters, height=4, wrap="word", font=self.fonts["mono"], relief="solid",
                                     borderwidth=1, background="white", padx=6, pady=4)
            self.taxa_text.grid(row=2, column=0, columnspan=2, sticky="ew")

            self.clean_status = self._action(tab, 3, "Run cleaning", self._run_clean, columnspan=2)
            return tab

        def _build_media_tab(self):
            tab = ttk.Frame(self.notebook, padding=14)
            tab.columnconfigure(0, weight=1)
            ask_csv = lambda: filedialog.askopenfilename(
                title="Select CSV", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
            ask_dir = lambda: filedialog.askdirectory(title="Select media folder")
            ask_output = lambda: filedialog.asksaveasfilename(
                title="Save measurements as", defaultextension=".csv",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])

            files = self._section(tab, "Files", 0)
            self.media_csv_entry = self._field(files, 0, "Dataset CSV", MASTER_CSV if MASTER_CSV.exists() else "", ask_csv)
            self.media_dir_entry = self._field(files, 1, "Image folder", MEDIA_DIR, ask_dir)

            download = self._section(tab, "Download images", 1)
            self._hint(download, 0, "Fetches each specimen image linked in the CSV and records its path in the "
                                    "media_path column. Images already in the folder are skipped.")
            self.media_status = self._action(download, 1, "Download images", self._run_download_media)

            measure = self._section(tab, "Measure images", 2)
            self.server_entry = self._field(measure, 0, "Model server", DEFAULT_BASE_URL)
            self.model_entry = self._field(measure, 1, "Model", DEFAULT_MODEL)
            self.measure_output_entry = self._field(measure, 2, "Output CSV", MEASUREMENTS_CSV, ask_output)
            self._hint(measure, 3, "Uses a vision model served by LM Studio. Images already measured are skipped "
                                   "and failed ones are retried.")
            self.measure_status = self._action(measure, 4, "Measure images", self._run_measurements)
            return tab

        def _browse(self, entry, ask):
            path = ask()
            if path:
                _set_entry(entry, path)

        def _load_preset(self):
            preset = PRESETS[self.preset_var.get()]
            _set_entry(self.species_entry, preset["species"])
            self.precision_var.set(preset["precision"])
            _set_text(self.taxa_text, "\n".join(preset["exclude_taxa"]))
            self.bad_issues = list(preset["bad_issues"])
            self.inspect_issues = list(preset["inspect_issues"])
            self._refresh_issue_summary()

        def _open_issue_dialog(self):
            dialog = IssueFilterDialog(self.root, self.bad_issues, self.inspect_issues, self.fonts["mono"])
            self.root.wait_window(dialog)
            if dialog.result:
                self.bad_issues, self.inspect_issues = map(list, dialog.result)
                self._refresh_issue_summary()

        def _refresh_issue_summary(self):
            self.issue_summary.configure(
                text=f"Removing {len(self.bad_issues)} issue codes, flagging {len(self.inspect_issues)}")

        def _clean_options(self):
            return {
                "exclude_taxa": [line.strip() for line in self.taxa_text.get("1.0", "end").splitlines() if line.strip()],
                "coordinate_precision": self.precision_var.get(),
                "bad_geospatial_issues": list(self.bad_issues),
                "inspection_issues": list(self.inspect_issues),
            }

        def _ui(self, fn, *args):
            if threading.current_thread() is threading.main_thread():
                fn(*args)
            else:
                self.root.after(0, fn, *args)

        def _log(self, message):
            self._ui(self._append_log, str(message))

        def _append_log(self, message):
            text = message.strip().lower()
            if text.startswith(("error", "failed")) or " failed:" in text:
                tag = "error"
            elif "complete" in text:
                tag = "ok"
            else:
                tag = None
            self.console.configure(state="normal")
            self.console.insert("end", message + "\n", tag)
            self.console.see("end")
            self.console.configure(state="disabled")

        def _clear_console(self):
            self.console.configure(state="normal")
            self.console.delete("1.0", "end")
            self.console.configure(state="disabled")

        def _progress(self, done, total):
            self._ui(self._set_progress, done, total)

        def _set_progress(self, done, total):
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress.configure(maximum=max(total, 1), value=done)
            if not self.cancel_event.is_set():
                self.status_var.set(f"Working… {done} of {total}")

        def _set_status(self, label, text, kind):
            label.configure(text=text, foreground=self.COLORS[kind])

        def _set_running(self, running, cancellable=False):
            self.running = running
            for button in self.action_buttons:
                button.configure(state="disabled" if running else "normal")
            self.cancel_btn.configure(state="normal" if running and cancellable else "disabled")
            if running:
                self.cancel_event.clear()
                self.progress.configure(mode="indeterminate", value=0)
                self.progress.start(15)
                self.status_var.set("Working…")
            else:
                self.progress.stop()
                self.progress.configure(mode="determinate", value=0)
                self.status_var.set("Ready")

        def _start(self, status_label, busy_text, work, cancellable=False):
            if self.running:
                return
            self._set_running(True, cancellable)
            self._set_status(status_label, busy_text, "busy")

            def runner():
                try:
                    result = work()
                except Exception as exc:
                    traceback.print_exc()
                    message = str(exc)
                    self._log(f"Error: {message}")
                    self._ui(self._set_status, status_label, "Failed", "error")
                    self._ui(lambda: messagebox.showerror("Error", message, parent=self.root))
                else:
                    text = result or "Done"
                    self._ui(self._set_status, status_label, text, "muted" if text == "Cancelled" else "ok")
                finally:
                    self._ui(self._set_running, False)
                    self._ui(self.root.bell)

            threading.Thread(target=runner, daemon=True).start()

        def _ask_yesno(self, title, message):
            answer = queue.Queue()
            self.root.after(0, lambda: answer.put(messagebox.askyesno(title, message, parent=self.root)))
            return answer.get()

        def _confirm_phase_2(self):
            return self._ask_yesno("Run phase 2?", f"Phase 1 is done. Review {INSPECT_CSV.name} now if needed "
                                                   "(type Remove in the Action column to drop a record), "
                                                   "then choose Yes to run phase 2.")

        def _cancel(self):
            self.cancel_event.set()
            self._log("Cancelling after the current item…")
            self.cancel_btn.configure(state="disabled")
            self.status_var.set("Cancelling…")

        def _on_close(self):
            if self.running and not messagebox.askyesno("Quit", "A task is still running. Quit anyway?",
                                                        parent=self.root):
                return
            self.root.destroy()

        def _download_inputs(self):
            species, doi = self.species_entry.get().strip(), self.doi_entry.get().strip()
            if not species and not doi:
                messagebox.showwarning("Missing input", "Enter a species name or a DOI.", parent=self.root)
                return None
            return species, doi, self.preset_var.get()

        def _run_download(self):
            inputs = self._download_inputs()
            if not inputs:
                return

            def work():
                folder = download_dataset(*inputs, log=self._log)
                self._ui(_set_entry, self.data_entry, folder)
                return "Downloaded"

            self._start(self.download_status, "Downloading…", work)

        def _run_full(self):
            inputs = self._download_inputs()
            if not inputs:
                return
            options = self._clean_options()

            def work():
                folder = download_dataset(*inputs, log=self._log)
                self._ui(_set_entry, self.data_entry, folder)
                master = run_cleaning(folder, "both", self._confirm_phase_2, self._log, **options)
                if not master:
                    return "Stopped after phase 1"
                self._ui(_set_entry, self.media_csv_entry, master)
                return "Workflow complete"

            self._start(self.full_status, "Running…", work)

        def _run_clean(self):
            steps = self.phase_var.get()
            path = self.data_entry.get().strip()
            if steps != "phase2" and not path:
                messagebox.showwarning("Missing input", "Select a data folder or ZIP file.", parent=self.root)
                return
            options = self._clean_options()

            def work():
                folder = prepare_folder(path, self._log) if steps != "phase2" else None
                master = run_cleaning(folder, steps, self._confirm_phase_2, self._log, **options)
                if not master:
                    return "Phase 1 done"
                self._ui(_set_entry, self.media_csv_entry, master)
                return "Finalized"

            self._start(self.clean_status, "Running…", work)

        def _run_download_media(self):
            csv_path = self.media_csv_entry.get().strip()
            media_dir = self.media_dir_entry.get().strip()
            if not csv_path or not Path(csv_path).is_file():
                messagebox.showwarning("Missing CSV", "Select an existing dataset CSV first.", parent=self.root)
                return

            def work():
                r = download_media(csv_path, media_dir or None, self._log, self.cancel_event.is_set, self._progress)
                if r["cancelled"]:
                    return "Cancelled"
                return f"{r['downloaded']} downloaded, {r['skipped']} skipped, {r['failed']} failed"

            self._start(self.media_status, "Downloading…", work, cancellable=True)

        def _run_measurements(self):
            media_dir = self.media_dir_entry.get().strip()
            output_csv = self.measure_output_entry.get().strip()
            base_url = self.server_entry.get().strip() or DEFAULT_BASE_URL
            model = self.model_entry.get().strip() or DEFAULT_MODEL
            if not media_dir or not output_csv:
                messagebox.showwarning("Missing input", "Choose an image folder and an output CSV.", parent=self.root)
                return

            def work():
                r = analyze_media(media_dir, output_csv, self._log, self.cancel_event.is_set, self._progress,
                                  base_url=base_url, model=model)
                if r["cancelled"]:
                    return "Cancelled"
                if not r["total"]:
                    return "No images found"
                return f"{r['measured']} measured, {r['skipped']} skipped, {r['failed']} failed"

            self._start(self.measure_status, "Measuring…", work, cancellable=True)


def _ask(prompt, default=""):
    value = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return value or str(default)


def _cli_confirm():
    return input("Review the CSV, then press Enter to run phase 2, or type n to stop: ").strip().lower() != "n"


def _cli_download():
    query = _ask("Species name or DOI")
    if not query:
        print("A species name or DOI is required.")
        return None
    is_doi = query.lower().startswith(("http://", "https://", "doi:", "10."))
    folder = download_dataset(doi_text=query) if is_doi else download_dataset(species=query)
    print(f"Download complete: {folder}")
    return folder


def _cli_clean():
    steps = {"1": "both", "2": "phase1", "3": "phase2"}.get(
        _ask("Steps (1 = phase 1 and 2, 2 = phase 1 only, 3 = phase 2 only)", "1"))
    if not steps:
        print("Invalid selection.")
        return
    folder = prepare_folder(_ask("Data folder or ZIP path")) if steps != "phase2" else None
    master = run_cleaning(folder, steps, _cli_confirm)
    if master:
        print(f"Final dataset: {master}")


def _cli_full():
    folder = _cli_download()
    if folder:
        master = run_cleaning(folder, "both", _cli_confirm)
        if master:
            print(f"Final dataset: {master}")


def _cli_download_media():
    csv_path = _ask("Dataset CSV", MASTER_CSV)
    if not Path(csv_path).is_file():
        print(f"CSV not found: {csv_path}")
        return
    download_media(csv_path, _ask("Image folder", MEDIA_DIR))


def _cli_measure():
    analyze_media(_ask("Image folder", MEDIA_DIR), _ask("Output CSV", MEASUREMENTS_CSV))


def cli_main():
    actions = {
        "1": ("Download only", _cli_download),
        "2": ("Clean existing data", _cli_clean),
        "3": ("Full workflow", _cli_full),
        "4": ("Download images", _cli_download_media),
        "5": ("Measure images", _cli_measure),
    }
    print("GBIF Herbaria Data Pipeline")
    for key, (label, _) in actions.items():
        print(f"  {key}  {label}")
    choice = actions.get(input("Choice: ").strip())
    if not choice:
        print("Invalid choice.")
        return
    try:
        choice[1]()
    except Exception as exc:
        print(f"Error: {exc}")


def _cleaner_cli(args):
    prog = Path(sys.argv[0]).name
    positional = [a for a in args if not a.startswith("--")]
    if positional[:1] == ["1"] and len(positional) >= 3:
        precision = (PRECISION_NONE if "--no-precision" in args
                     else PRECISION_STRICT if "--strict" in args else PRECISION_RELAXED)
        phase_1_clean_and_merge(positional[1], positional[2],
                                output_csv=positional[3] if len(positional) > 3 else None,
                                coordinate_precision=precision)
    elif positional[:1] == ["2"] and len(positional) >= 2:
        phase_2_finalize_dataset(*positional[1:4])
    else:
        print("Usage:")
        print(f"  python {prog} cleaner 1 <occurrence> <multimedia> [output] [--strict | --no-precision]")
        print(f"  python {prog} cleaner 2 <inspected_csv> [master] [duplicates]")
        sys.exit(1)


def main():
    args = sys.argv[1:]
    command = args[0] if args else "--gui"
    if command == "cleaner":
        return _cleaner_cli(args[1:])
    if command == "measure":
        return analyze_media(args[1] if len(args) > 1 else MEDIA_DIR, args[2] if len(args) > 2 else MEASUREMENTS_CSV)
    if command == "--cli":
        return cli_main()
    root = None
    if tk:
        try:
            root = tk.Tk()
        except tk.TclError:
            pass
    if root is None:
        print("GUI unavailable; starting the command-line interface.")
        return cli_main()
    PipelineApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()