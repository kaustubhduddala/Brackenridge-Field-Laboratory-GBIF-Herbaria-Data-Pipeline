import csv
from pathlib import Path

import pandas as pd

from config import (BAD_GEOSPATIAL_ISSUES, DUPLICATES_CSV, FULL_MASTER_CSV, INSPECT_CSV, REMOVED_COLUMNS_CSV, INSPECTION_ISSUES, LINK_COLUMNS,
                    MASTER_CSV, MEASUREMENT_COLUMNS, MEDIA_COLUMNS, MEDIA_EVIDENCE_COLUMNS, MULTIMEDIA_RENAMES,
                    PRECISION_NONE, PRECISION_RELAXED, PRECISION_STRICT)
from .geo import clean_coordinates
from .utils import (all_links, find_dwca_files, first_url_series, insert_after_gbif, issue_sets, read_csv,
                   read_dwca, save_csv)


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


def phase_2_finalize_dataset(inspected_csv=INSPECT_CSV, final_master=None, final_duplicates=None, log=print):
    final_master = Path(final_master or MASTER_CSV)
    final_duplicates = Path(final_duplicates or DUPLICATES_CSV)
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
    if Path(final_master) == MASTER_CSV:
        save_csv(master, FULL_MASTER_CSV)
        REMOVED_COLUMNS_CSV.unlink(missing_ok=True)
    save_csv(duplicates.dropna(axis=1, how="all"), final_duplicates)
    log(f"  Duplicates saved to {final_duplicates}")
    log(f"Phase 2 complete: {len(master)} unique records saved to {final_master}")
    return str(final_master), str(final_duplicates)


REQUIRED_COLUMNS = ("gbifID", "media")
# REQUIRED_COLUMNS = ("gbifID", "media", *MEASUREMENT_COLUMNS)

def _header(path):
    return list(pd.read_csv(path, nrows=0, dtype=str, encoding="utf-8-sig").columns)


def available_columns(folder=None):
    """Every column found in the pipeline's files, and a note on where they came from."""
    columns = []
    for path in (FULL_MASTER_CSV, MASTER_CSV, REMOVED_COLUMNS_CSV, INSPECT_CSV):
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


def phase_3_select_columns(columns=None, full_csv=None, final_master=None, removed_columns_csv=None, log=print):
    full_csv = Path(full_csv or FULL_MASTER_CSV)
    final_master = Path(final_master or MASTER_CSV)
    removed_columns_csv = Path(removed_columns_csv or REMOVED_COLUMNS_CSV)
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


def run_cleaning(folder=None, phases=(1, 2, 3), confirm=None, log=print, master_columns=None,
                 column_chooser=None, **options):
    phases = set(phases)
    if 1 in phases:
        occurrence, multimedia = find_dwca_files(folder)
        phase_1_clean_and_merge(occurrence, multimedia, log=log, **options)
        if 2 in phases:
            log(f"Review {INSPECT_CSV} now. Type Remove in the Action column to drop a record.")
            if confirm and not confirm():
                return None
    master = None
    if 2 in phases:
        master, _ = phase_2_finalize_dataset(INSPECT_CSV, log=log)
    if 3 in phases:
        if not FULL_MASTER_CSV.is_file():
            raise FileNotFoundError(f"{FULL_MASTER_CSV.name} not found; run phase 2 first")
        if column_chooser:
            master_columns = column_chooser(available_columns()[0])
            if master_columns is False:
                log("Phase 3 skipped: no columns chosen")
                return master
        master = phase_3_select_columns(master_columns, log=log)
    return master
