"""Clean a GBIF download into the master dataset: phases 1-3, plus coordinate checks."""
import csv
from pathlib import Path

import numpy as np
import pandas as pd

from config import (BAD_GEOSPATIAL_ISSUES, INSPECTION_ISSUES, LINK_COLUMNS, MEASUREMENT_COLUMNS, MEDIA_COLUMNS,
                    MEDIA_EVIDENCE_COLUMNS, MULTIMEDIA_RENAMES, PRECISION_NONE, PRECISION_RELAXED,
                    PRECISION_STRICT)
from .utils import (all_links, find_dwca_files, first_url_series, insert_after_gbif, issue_sets, read_csv,
                   read_dwca, save_csv)


def phase_1_clean_and_merge(occurrence_file, multimedia_file, ws, output_csv=None, removed_csv=None,
                            exclude_taxa=None, coordinate_precision=PRECISION_RELAXED,
                            bad_geospatial_issues=None, inspection_issues=None, log=print):
    output_csv = Path(output_csv or ws.inspect_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    removed_csv = Path(removed_csv or output_csv.with_name(ws.removed_csv.name))
    missing_csv = output_csv.with_name(ws.missing_csv.name)
    exclude_taxa = list(exclude_taxa or [])
    bad_issues = frozenset(BAD_GEOSPATIAL_ISSUES if bad_geospatial_issues is None else bad_geospatial_issues)
    inspect_issues = frozenset(INSPECTION_ISSUES if inspection_issues is None else inspection_issues)
    removed_parts = []

    def remove(df, mask, reason, label):
        mask = pd.Series(mask, index=df.index).fillna(False).astype(bool)
        if mask.any():
            removed_parts.append(insert_after_gbif(df[mask], "removal_reason", reason))
        df = df[~mask]
        log(f"  {label}: removed {int(mask.sum())}, {len(df)} remaining")
        return df

    log("Phase 1: cleaning and merging")
    occ = read_dwca(occurrence_file).dropna(axis=1, how="all")
    media = read_dwca(multimedia_file).rename(columns=MULTIMEDIA_RENAMES).dropna(axis=1, how="all")
    log(f"Loaded {len(occ)} occurrence rows and {len(media)} multimedia rows")
    df = occ.merge(media, on="gbifID", how="left", suffixes=("_occurrence", "_multimedia"))
    df = insert_after_gbif(df, "media", first_url_series(df, MEDIA_COLUMNS))
    log(f"Merged dataset: {len(df)} rows")

    evidence = [c for c in MEDIA_EVIDENCE_COLUMNS if c in df.columns]
    missing_count = 0
    if evidence:
        has_media = df[evidence].fillna("").astype(str).apply(lambda s: s.str.strip().ne("")).any(axis=1)
        missing = df[~has_media]
        df = df[has_media]
        missing_count = len(missing)
        if missing_count:
            links = [all_links(row) for _, row in missing.iterrows()]
            out = insert_after_gbif(missing.drop(columns="media"), "media_links", links)
            out = insert_after_gbif(out, "removal_reason", "No media evidence")
            save_csv(out.dropna(axis=1, how="all"), missing_csv)
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
        found = issue_sets(df).map(lambda issues: sorted(issues & bad_issues))
        df = remove(df, found.map(bool), "Bad geospatial issue: " + found.map(", ".join), "Bad issues")

    link_columns = [c for c in LINK_COLUMNS if c in df.columns]
    if link_columns:
        blank = df[link_columns].fillna("").astype(str).apply(lambda s: s.str.strip().eq("")).all(axis=1)
        df = remove(df, blank, "No valid reference links in any link column", "Reference links")

    df = df.copy()
    df["inspect_flag"] = issue_sets(df).map(lambda issues: bool(issues & inspect_issues))
    log(f"  Flagged for inspection: {int(df['inspect_flag'].sum())}")
    if "Action" not in df.columns:
        df = insert_after_gbif(df, "Action", "")
    df = df.dropna(axis=1, how="all")
    save_csv(df, output_csv)

    if removed_parts:
        removed = pd.concat(removed_parts, ignore_index=True).dropna(axis=1, how="all")
        save_csv(removed, removed_csv)
        log(f"Removed records saved to {removed_csv} ({len(removed)} rows)")
        for reason, count in removed["removal_reason"].astype(str).str.split(":").str[0].value_counts().items():
            log(f"  {reason}: {count}")
    elif removed_csv.exists():
        removed_csv.unlink()

    log(f"Phase 1 complete: {len(df)} rows saved to {output_csv}")
    return str(output_csv)


def phase_2_finalize_dataset(ws, inspected_csv=None, final_master=None, final_duplicates=None, log=print):
    inspected_csv = Path(inspected_csv or ws.inspect_csv)
    final_master = Path(final_master or ws.master_csv)
    final_duplicates = Path(final_duplicates or ws.duplicates_csv)
    final_master.parent.mkdir(parents=True, exist_ok=True)
    log("Phase 2: finalizing and deduplicating")
    df = read_csv(inspected_csv, log)
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
    master = insert_after_gbif(master, "media", media)
    for column in MEASUREMENT_COLUMNS:
        if column not in master.columns:
            master[column] = ""

    master = master.dropna(axis=1, how="all")
    save_csv(master, final_master)
    if final_master == ws.master_csv:
        save_csv(master, ws.full_master_csv)
        ws.removed_columns_csv.unlink(missing_ok=True)
    save_csv(duplicates.dropna(axis=1, how="all"), final_duplicates)
    log(f"  Duplicates saved to {final_duplicates}")
    log(f"Phase 2 complete: {len(master)} unique records saved to {final_master}")
    return str(final_master), str(final_duplicates)


REQUIRED_COLUMNS = ("gbifID", "media")
# REQUIRED_COLUMNS = ("gbifID", "media", *MEASUREMENT_COLUMNS)

def _header(path):
    return list(pd.read_csv(path, nrows=0, dtype=str, encoding="utf-8-sig").columns)


def available_columns(ws, folder=None):
    """Every column found in the pipeline's files, and a note on where they came from."""
    columns = []
    known = (ws.full_master_csv, ws.master_csv, ws.removed_columns_csv, ws.inspect_csv) if ws else ()
    for path in known:
        if Path(path).is_file():
            columns += _header(path)
    if columns:
        return list(dict.fromkeys(columns)), "all available data"
    if folder:
        occurrence, multimedia = find_dwca_files(folder)
        occ = list(pd.read_csv(occurrence, sep="\t", nrows=0, quoting=csv.QUOTE_NONE).columns)
        media = [MULTIMEDIA_RENAMES.get(c, c) for c in pd.read_csv(multimedia, sep="\t", nrows=0,
                                                                     quoting=csv.QUOTE_NONE).columns]
        shared = (set(occ) & set(media)) - {"gbifID"}
        columns = ["gbifID", "media"] + [c + "_occurrence" if c in shared else c for c in occ if c != "gbifID"] \
            + [c + "_multimedia" if c in shared else c for c in media if c != "gbifID"]
        return list(dict.fromkeys(columns)), "the downloaded data (columns empty in every record are dropped later)"
    return [], ""


def phase_3_select_columns(ws, columns=None, full_csv=None, final_master=None, removed_columns_csv=None, log=print):
    full_csv = Path(full_csv or ws.full_master_csv)
    final_master = Path(final_master or ws.master_csv)
    removed_columns_csv = Path(removed_columns_csv or ws.removed_columns_csv)
    if not full_csv.is_file():
        raise FileNotFoundError(f"{full_csv.name} not found; run phase 2 first")
    log("Phase 3: selecting master columns")
    df = read_csv(full_csv, log)
    if "gbifID" in df.columns:
        for extra in (final_master, removed_columns_csv):
            if extra.is_file():
                other = read_csv(extra, log)
                new = [c for c in other.columns if c not in df.columns]
                if new and "gbifID" in other.columns:
                    df = df.merge(other.drop_duplicates("gbifID")[["gbifID", *new]], on="gbifID", how="left")
                    log(f"  Recovered {len(new)} columns from {extra.name}")
    if columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            log(f"  Ignoring {len(missing)} selected columns not in the data: {', '.join(missing[:5])}"
                + (", ..." if len(missing) > 5 else ""))
        chosen = set(columns) | set(REQUIRED_COLUMNS)
        kept = [c for c in df.columns if c in chosen]
    else:
        kept = list(df.columns)
    dropped = [c for c in df.columns if c not in kept]
    save_csv(df[kept], final_master)
    if dropped:
        id_column = ["gbifID"] if "gbifID" in df.columns else []
        save_csv(df[id_column + dropped], removed_columns_csv)
        log(f"  Removed {len(dropped)} columns, saved to {removed_columns_csv}")
    else:
        removed_columns_csv.unlink(missing_ok=True)
        log("  No columns removed")
    log(f"Phase 3 complete: {len(kept)} columns and {len(df)} records saved to {final_master}")
    return str(final_master)


def run_cleaning(ws, folder=None, phases=(1, 2, 3), log=print, master_columns=None, column_chooser=None, **options):
    phases = set(phases)
    log(f"Working in {ws.root}")
    if 1 in phases:
        occurrence, multimedia = find_dwca_files(folder)
        phase_1_clean_and_merge(occurrence, multimedia, ws, log=log, **options)
        if 2 in phases:
            log(f"Records flagged for a look are in {ws.inspect_csv}. To drop one, put Remove in its Action column "
                "(Edit tab) and run phase 2 again.")
    master = None
    if 2 in phases:
        master, _ = phase_2_finalize_dataset(ws, log=log)
    if 3 in phases:
        if not ws.full_master_csv.is_file():
            raise FileNotFoundError(f"{ws.full_master_csv.name} not found in {ws.root}; run phase 2 first")
        if column_chooser:
            master_columns = column_chooser(available_columns(ws)[0])
            if master_columns is False:
                log("Phase 3 skipped: no columns chosen")
                return master
        master = phase_3_select_columns(ws, master_columns, log=log)
    return master


# --------------------------------------------------------------------------- coordinate cleaning

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
