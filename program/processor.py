from pathlib import Path

import pandas as pd

from program.config import (BAD_GEOSPATIAL_ISSUES, DUPLICATES_CSV, INSPECT_CSV, INSPECTION_ISSUES, LINK_COLUMNS,
                    MASTER_CSV, MEASUREMENT_COLUMNS, MEDIA_COLUMNS, MEDIA_EVIDENCE_COLUMNS, MULTIMEDIA_RENAMES,
                    PRECISION_NONE, PRECISION_RELAXED, PRECISION_STRICT)
from program.geo import clean_coordinates
from program.utils import (all_links, find_dwca_files, first_url_series, insert_after_gbif, issue_sets, read_csv,
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

    save_csv(master.dropna(axis=1, how="all"), final_master)
    save_csv(duplicates.dropna(axis=1, how="all"), final_duplicates)
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
