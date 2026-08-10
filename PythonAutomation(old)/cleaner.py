"""
Data Cleaning Pipeline for GBIF Herbaria Data
Based on original R script by Kat Tisshaw and protocol by Rhodes et al.

This module handles two phases of data cleaning:
- Phase 1: Initial data cleaning, merging, and flagging for manual inspection
- Phase 2: Final deduplication and export after manual review
"""

import pandas as pd
import csv
import sys
import re
from pathlib import Path


# ==============
# CONFIGURATION
# ==============

# Default taxa to exclude (can be overridden)
DEFAULT_EXCLUDE_TAXA = [
    "Megathyrsus maximus var. coloratus (C.T.White) B.K.Simon & S.W.L.Jacobs",
    "Megathyrsus maximus var. pubiglumis (K.Schum.) B.K.Simon & S.W.L.Jacobs",
    "Panicum compressum Biv.",
    "Panicum trichoglume K.Schum.",
    "Panicum maximum var. effusum A.Camus",
    "Panicum mahafalense A.Camus",
    "Panicum maximum var. pubiglume K.Schum",
    "Panicum maximum var. trichoglume Robyns"
]

# Geospatial issues that indicate unreliable data
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
    "CONTINENT_COUNTRY_MISMATCH"
]

# Issues that warrant manual inspection (don't remove, just flag)
INSPECTION_ISSUES = [
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
    "TAXON_CONCEPT_ID_NOT_FOUND",
    "TAXON_ID_NOT_FOUND",
    "ELEVATION_MIN_MAX_SWAPPED",
    "ELEVATION_NON_NUMERIC",
    "MODIFIED_DATE_INVALID",
    "IDENTIFIED_DATE_INVALID",
    "TYPE_STATUS_INVALID",
    "MULTIMEDIA_DATE_INVALID",
    "MULTIMEDIA_URI_INVALID",
    "INDIVIDUAL_COUNT_CONFLICTS_WITH_OCCURRENCE_STATUS",
    "OCCURRENCE_STATUS_UNPARSABLE",
    "OCCURRENCE_STATUS_INFERRED_FROM_INDIVIDUAL_COUNT",
    "AMBIGUOUS_INSTITUTION",
    "AMBIGUOUS_COLLECTION",
    "INSTITUTION_MATCH_NONE",
    "COLLECTION_MATCH_NONE",
    "INSTITUTION_MATCH_FUZZY",
    "COLLECTION_MATCH_FUZZY",
    "INSTITUTION_COLLECTION_MISMATCH"
]

# Link columns to validate (at least one must have data)
LINK_COLUMNS = [
    'identifier',
    'references_multimedia',
    'bibliographicCitation',
    'references',
    'associatedReferences',
    'occurrenceID'
]


# ==============
# PHASE 1: INITIAL DATA CLEANING
# ==============

def phase_1_clean_and_merge(
    occurrence_file,
    multimedia_file,
    output_csv="GBIFdownload_inspectFlags.csv",
    exclude_taxa=None,
    relax_coordinate_precision=True
):
    """
    Phase 1: Clean raw GBIF data and prepare for manual inspection.
    
    Steps:
    1. Load occurrence and multimedia data
    2. Rename multimedia columns to avoid conflicts
    3. Merge datasets
    4. Filter by coordinate precision (3 decimal places)
    5. Remove specified taxa
    6. Remove rows with bad geospatial issues
    7. Flag rows requiring manual inspection
    8. Validate presence of reference links
    9. Export for manual inspection
    
    Args:
        occurrence_file: Path to occurrence.txt
        multimedia_file: Path to multimedia.txt
        output_csv: Output filename for flagged data
        exclude_taxa: List of taxa to exclude (uses DEFAULT_EXCLUDE_TAXA if None)
        relax_coordinate_precision: If True, allows precision with 1+ decimal places.
                                    If False, requires exactly 3 decimal places.
    
    Returns:
        Path to output CSV file
    """
    
    if exclude_taxa is None:
        exclude_taxa = DEFAULT_EXCLUDE_TAXA
    

    print("PHASE 1: INITIAL DATA CLEANING AND MERGING")

    print()
    
    # ==============
    # Step 1-2: Load and rename multimedia columns
    # ==============
    print("1. Loading data files...")
    try:
        gbif_db = pd.read_csv(
            occurrence_file,
            sep='\t',
            low_memory=False,
            quoting=csv.QUOTE_NONE,
            dtype=str
        )
        print(f"   ✓ Loaded occurrence.txt: {len(gbif_db)} rows")
    except Exception as e:
        raise ValueError(f"Failed to load occurrence file: {e}")
    
    try:
        media_db = pd.read_csv(
            multimedia_file,
            sep='\t',
            low_memory=False,
            quoting=csv.QUOTE_NONE,
            dtype=str
        )
        print(f"   ✓ Loaded multimedia.txt: {len(media_db)} rows")
    except Exception as e:
        raise ValueError(f"Failed to load multimedia file: {e}")
    
    # Rename multimedia columns to avoid conflicts
    multimedia_rename_map = {
        'type': 'type_multimedia',
        'references': 'references_multimedia',
        'publisher': 'publisher_multimedia',
        'license': 'license_multimedia',
        'rightsHolder': 'rightsHolder_multimedia'
    }
    media_db = media_db.rename(columns=multimedia_rename_map)
    
    # ==============
    # Step 3: Remove empty columns and merge
    # ==============
    print()
    print("2. Merging multimedia and occurrence data...")
    
    # Remove columns that are entirely empty
    gbif_db = gbif_db.dropna(axis=1, how='all')
    media_db = media_db.dropna(axis=1, how='all')
    
    # Merge on gbifID
    master_df = pd.merge(media_db, gbif_db, on='gbifID', how='outer')
    print(f"   ✓ Merged dataset: {len(master_df)} rows")
    
    # ==============
    # Step 4: Filter by coordinate precision
    # ==============
    print()
    print("3. Filtering by coordinate precision...")
    initial_count = len(master_df)
    
    if relax_coordinate_precision:
        # Allow at least 1 decimal place (relaxed)
        print("   Using relaxed precision: ≥1 decimal place")
        master_df = master_df[
            master_df['decimalLatitude'].astype(str).str.contains(r'\.\d+', na=False, regex=True) &
            master_df['decimalLongitude'].astype(str).str.contains(r'\.\d+', na=False, regex=True)
        ]
    else:
        # Strict: exactly 3+ decimal places
        print("   Using strict precision: exactly ≥3 decimal places")
        master_df = master_df[
            master_df['decimalLatitude'].astype(str).str.contains(r'\.\d{3,}', na=False, regex=True) &
            master_df['decimalLongitude'].astype(str).str.contains(r'\.\d{3,}', na=False, regex=True)
        ]
    
    removed_count = initial_count - len(master_df)
    print(f"   ✓ Rows remaining: {len(master_df)} (removed {removed_count})")
    
    # ==============
    # Step 5: Remove specified taxa
    # ==============
    print()
    print("4. Removing specified taxa exclusions...")
    initial_count = len(master_df)
    
    for col in ['scientificName', 'infraspecificEpithet', 'verbatimScientificName']:
        if col in master_df.columns:
            master_df = master_df[~master_df[col].isin(exclude_taxa)]
    
    removed_count = initial_count - len(master_df)
    print(f"   ✓ Rows remaining: {len(master_df)} (removed {removed_count} taxa)")
    
    # ==============
    # Step 6: Remove bad geospatial issues
    # ==============
    print()
    print("5. Removing records with bad geospatial issues...")
    initial_count = len(master_df)
    
    bad_pattern = '|'.join(BAD_GEOSPATIAL_ISSUES)
    master_df = master_df[
        master_df['issue'].isna() |
        ~master_df['issue'].astype(str).str.contains(bad_pattern, na=False, regex=True)
    ]
    
    removed_count = initial_count - len(master_df)
    print(f"   ✓ Rows remaining: {len(master_df)} (removed {removed_count} with bad issues)")
    
    # ==============
    # Step 7: Validate link columns
    # ==============
    print()
    print("6. Validating reference link columns...")
    initial_count = len(master_df)
    
    # Check which link columns exist
    existing_link_cols = [col for col in LINK_COLUMNS if col in master_df.columns]
    
    if existing_link_cols:
        # Replace empty/whitespace strings with NaN
        for col in existing_link_cols:
            master_df[col] = master_df[col].replace(r'^\s*$', pd.NA, regex=True)
        
        # Remove rows where ALL link columns are empty
        master_df = master_df.dropna(subset=existing_link_cols, how='all')
    
    removed_count = initial_count - len(master_df)
    print(f"   ✓ Rows remaining: {len(master_df)} (removed {removed_count} without links)")
    
    # ==============
    # Step 8: Flag inspection issues
    # ==============
    print()
    print("7. Flagging records for manual inspection...")
    
    inspect_pattern = '|'.join(INSPECTION_ISSUES)
    master_df['inspect_flag'] = (
        master_df['issue'].notna() &
        master_df['issue'].astype(str).str.contains(inspect_pattern, na=False, regex=True)
    )
    
    flagged_count = master_df['inspect_flag'].sum()
    print(f"   ✓ Records flagged for inspection: {flagged_count}")
    
    # ==============
    # Step 9: Export and summary
    # ==============
    print()
    print("8. Exporting cleaned dataset...")
    
    # Drop fully empty columns before export
    master_df = master_df.dropna(axis=1, how='all')
    
    master_df.to_csv(output_csv, index=False)
    print(f"   ✓ Exported to: {output_csv}")
    
    print()

    print(f"PHASE 1 COMPLETE")
    print(f"Final dataset: {len(master_df)} rows ready for manual inspection")

    print()
    print("NEXT STEPS:")
    print("1. Open the exported CSV file in Excel or a spreadsheet application")
    print("2. Sort by 'inspect_flag' to see flagged records")
    print("3. For any suspicious entries, add 'Remove' to the 'Action' column")
    print("4. Remove rows with obviously bad data (cultivated, botanic gardens, etc.)")
    print("5. Run Phase 2 after manual review is complete")
    print()
    
    return output_csv


# ==============
# PHASE 2: DEDUPLICATION AND FINALIZATION
# ==============

def phase_2_finalize_dataset(
    inspected_csv,
    final_master="master_cleaned.csv",
    final_duplicates="removed_duplicates.csv"
):
    """
    Phase 2: Process manually inspected data and finalize datasets.
    
    Steps:
    1. Load manually inspected file
    2. Remove rows marked for deletion
    3. Remove duplicate coordinates
    4. Add columns for ImageJ measurements
    5. Export final datasets
    
    Args:
        inspected_csv: Path to the manually inspected CSV (from Phase 1 output)
        final_master: Output filename for final master dataset
        final_duplicates: Output filename for removed duplicate coordinates
    
    Returns:
        Tuple of (final_master_path, final_duplicates_path)
    """
    

    print("PHASE 2: FINALIZATION AND DEDUPLICATION")

    print()
    
    # ==============
    # Step 1: Load manually inspected data
    # ==============
    print(f"1. Loading manually inspected file: {inspected_csv}")
    try:
        master_df = pd.read_csv(inspected_csv, keep_default_na=True, low_memory=False)
        print(f"   ✓ Loaded: {len(master_df)} rows")
    except Exception as e:
        raise ValueError(f"Failed to load inspected CSV: {e}")
    
    # ==============
    # Step 2: Remove marked deletions
    # ==============
    print()
    print("2. Removing records marked for deletion...")
    initial_count = len(master_df)
    
    if 'Action' in master_df.columns:
        master_df = master_df[
            (master_df['Action'].isna()) | (master_df['Action'] != 'Remove')
        ]
        removed_count = initial_count - len(master_df)
        print(f"   ✓ Rows remaining: {len(master_df)} (removed {removed_count} marked for deletion)")
    else:
        print("   ℹ No 'Action' column found; skipping deletion marks")
    
    # ==============
    # Step 3: Remove duplicate coordinates
    # ==============
    print()
    print("3. Deduplicating by coordinate...")
    initial_count = len(master_df)
    
    # Create non-duplicate dataset (keep first occurrence)
    master_df_ND = master_df.drop_duplicates(
        subset=['decimalLatitude', 'decimalLongitude'],
        keep='first'
    )
    
    # Extract removed duplicates (keep all but first)
    removed_coords = master_df[
        master_df.duplicated(subset=['decimalLatitude', 'decimalLongitude'], keep='first')
    ].copy()
    
    removed_count = len(removed_coords)
    print(f"   ✓ Master dataset: {len(master_df_ND)} rows (removed {removed_count} duplicates)")
    print(f"   ✓ Duplicate records saved separately: {len(removed_coords)} rows")
    
    # ==============
    # Step 4: Add measurement columns
    # ==============
    print()
    print("4. Adding ImageJ measurement columns...")
    
    measurement_cols = {
        'Panicle length (cm)': '',
        'Leaf width (cm)': '',
        'Seed length (cm)': ''
    }
    
    for col_name, default_val in measurement_cols.items():
        if col_name not in master_df_ND.columns:
            master_df_ND[col_name] = default_val
            print(f"   ✓ Added column: {col_name}")
    
    # ==============
    # Step 5: Clean and export
    # ==============
    print()
    print("5. Cleaning and exporting final datasets...")
    
    # Remove fully empty columns
    master_df_ND = master_df_ND.dropna(axis=1, how='all')
    removed_coords = removed_coords.dropna(axis=1, how='all')
    
    master_df_ND.to_csv(final_master, index=False)
    print(f"   ✓ Exported master: {final_master}")
    
    removed_coords.to_csv(final_duplicates, index=False)
    print(f"   ✓ Exported duplicates: {final_duplicates}")
    
    print()

    print("PHASE 2 COMPLETE")
    print(f"Final master dataset: {len(master_df_ND)} unique records")
    print(f"Duplicate coordinates removed: {len(removed_coords)} records")

    print()
    print("READY FOR IMAGEJ MEASUREMENTS")
    print(f"Use {final_master} to measure panicle length, leaf width, and seed length")
    print()
    
    return final_master, final_duplicates


# ==============
# COMMAND-LINE INTERFACE
# ==============

def main():
    """Command-line interface for the cleaner script."""
    
    if len(sys.argv) < 2:
        print("Usage: python cleaner.py <phase> [options]")
        print()
        print("PHASE 1:")
        print("  python cleaner.py 1 <occurrence_file> <multimedia_file> [output_csv] [--strict]")
        print()
        print("PHASE 2:")
        print("  python cleaner.py 2 <inspected_csv> [final_master] [final_duplicates]")
        print()
        print("Examples:")
        print("  python cleaner.py 1 data_dl123/occurrence.txt data_dl123/multimedia.txt")
        print("  python cleaner.py 2 GBIFdownload_inspectFlags.csv")
        print()
        sys.exit(1)
    
    phase = sys.argv[1]
    
    if phase == "1":
        # Phase 1: Clean and merge
        if len(sys.argv) < 4:
            print("Phase 1 requires: occurrence_file and multimedia_file")
            sys.exit(1)
        
        occurrence_file = sys.argv[2]
        multimedia_file = sys.argv[3]
        output_csv = sys.argv[4] if len(sys.argv) > 4 else "GBIFdownload_inspectFlags.csv"
        relax_precision = "--strict" not in sys.argv
        
        phase_1_clean_and_merge(
            occurrence_file,
            multimedia_file,
            output_csv=output_csv,
            exclude_taxa=None,
            relax_coordinate_precision=relax_precision
        )
    
    elif phase == "2":
        # Phase 2: Finalize
        if len(sys.argv) < 3:
            print("Phase 2 requires: inspected_csv")
            sys.exit(1)
        
        inspected_csv = sys.argv[2]
        final_master = sys.argv[3] if len(sys.argv) > 3 else "master_cleaned.csv"
        final_duplicates = sys.argv[4] if len(sys.argv) > 4 else "removed_duplicates.csv"
        
        phase_2_finalize_dataset(
            inspected_csv,
            final_master=final_master,
            final_duplicates=final_duplicates
        )
    
    else:
        print(f"Invalid phase: {phase}")
        print("Phase must be '1' or '2'")
        sys.exit(1)


if __name__ == "__main__":
    main()
