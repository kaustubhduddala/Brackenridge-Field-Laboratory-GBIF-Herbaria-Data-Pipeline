import os, time, zipfile, requests, csv, pandas as pd, warnings, pathlib, sys, subprocess
from pathlib import Path
from pygbif import occurrences   

def resolve_species(scientific_name):

    # Takes a string species name and returns its official GBIF taxonKey by directly pinging the GBIF public API (bypassing pygbif)

    print(f"Looking up GBIF taxonomic key for '{scientific_name}'...")
    
    url = "https://api.gbif.org/v1/species/match"
    params = {"name": scientific_name}
    
    response = requests.get(url, params=params)
    
    response.raise_for_status()
    
    result = response.json()
    
    if result.get('matchType') in ['EXACT', 'FUZZY'] and 'usageKey' in result:
        key = result['usageKey']
        official_name = result.get('scientificName', scientific_name)
        print(f"Success! Found '{official_name}' with Taxon Key: {key}")
        return key
    else:
        # debugging if needed
        print("\n--- DEBUGLOG ---")
        print(result)
        print("-------------------------------\n")
        raise ValueError(f"Could not find a reliable taxonomic match for '{scientific_name}'.")

def trigger_download(queries, download_format, user, password, email):
    # Submits a download request to GBIF and returns the download key.
    print("Submitting download request to GBIF...")
    
    result = occurrences.download(
        queries, 
        format=download_format,
        user=user, 
        pwd=password, 
        email=email
    )
    
    # pygbif returns a tuple containing the key and status (e.g., ('000123-456', 'PREPARING'))
    if isinstance(result, tuple):
        return result[0]
    return result

def wait_and_download(download_key, output_dir="."):

    # Polls the GBIF server and downloads the ZIP when ready.
    meta = occurrences.download_meta(download_key)
    
    while meta['status'] in ['RUNNING', 'PREPARING']:
        print("Waiting for GBIF to generate the file...")
        time.sleep(30)
        meta = occurrences.download_meta(download_key)

    if meta['status'] == 'SUCCEEDED':
        print("File is ready! Downloading ZIP file...")
        occurrences.download_get(download_key, path=output_dir)
        return f"{output_dir}/{download_key}.zip"
    else:
        raise Exception(f"Download failed with status: {meta['status']}")

def extract_archive(zip_path, extract_dir):
    """Extracts the downloaded Darwin Core Archive."""
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_dir)
    print(f"Data extracted to {extract_dir}")
    return extract_dir


### NOTE: This function is an **AI** adaption of the R script and is meant to replace it's functionality. THIS IS NOT STABLE, DO NOT RELY OR USE IT YET!!!
def clean_and_prepare_data(extract_dir, output_csv="GBIFdownload_inspectFlags.csv", exclude_taxa=None):
    

    occ_path = os.path.join(extract_dir, "occurrence.txt")
    media_path = os.path.join(extract_dir, "multimedia.txt")

    # Read everything as a string to prevent pandas from rounding or changing decimals
    gbif_db = pd.read_csv(occ_path, sep='\t', low_memory=False, quoting=csv.QUOTE_NONE, dtype=str)
    media_db = pd.read_csv(media_path, sep='\t', low_memory=False, quoting=csv.QUOTE_NONE, dtype=str)

    print(f"1. Raw Occurrence rows loaded: {len(gbif_db)}")
    if gbif_db.empty:
        raise ValueError("The GBIF occurrence file is empty!")

    media_db = media_db.rename(columns={
        'type': 'type_multimedia', 'references': 'references_multimedia',
        'publisher': 'publisher_multimedia', 'license': 'license_multimedia',
        'rightsHolder': 'rightsHolder_multimedia'
    })

    master_df = pd.merge(media_db, gbif_db, on='gbifID', how='outer')
    print(f"2. Rows after merging with multimedia: {len(master_df)}")

    # ==========================================
    # REGEX: Coordinate Precision
    # ==========================================
    master_df = master_df[master_df['decimalLatitude'].astype(str).str.contains(r'\.\d{3}', na=False)]
    master_df = master_df[master_df['decimalLongitude'].astype(str).str.contains(r'\.\d{3}', na=False)]
    print(f"3. Rows remaining after 3-decimal Regex filter: {len(master_df)}")

    # ==========================================
    # EXCLUSION: Taxa Drop
    # ==========================================
    if exclude_taxa:
        master_df = master_df[~master_df['scientificName'].isin(exclude_taxa)]
        master_df = master_df[~master_df['infraspecificEpithet'].isin(exclude_taxa)]
        master_df = master_df[~master_df['verbatimScientificName'].isin(exclude_taxa)]
    print(f"4. Rows remaining after specific taxa drop: {len(master_df)}")

    # ==========================================
    # EXCLUSION: Bad Geospatial Issues
    # ==========================================
    bad_issues = [
        "COORDINATE_ROUNDED", "GEODETIC_DATUM_INVALID", "GEODETIC_DATUM_ASSUMED_WGS84",
        "COORDINATE_PRECISION_INVALID", "COORDINATE_UNCERTAINTY_METERS_INVALID",
        "FOOTPRINT_INVALID", "FOOTPRINT_WKT_MISMATCH", "FOOTPRINT_SRS_INVALID", 
        "CONTINENT_COORDINATE_MISMATCH", "COUNTRY_COORDINATE_MISMATCH", "CONTINENT_COUNTRY_MISMATCH"
    ]
    bad_pattern = '|'.join(bad_issues)
    master_df = master_df[
        master_df['issue'].isna() | 
        ~master_df['issue'].astype(str).str.contains(bad_pattern, na=False)
    ]
    print(f"5. Rows remaining after BAD geospatial issues drop: {len(master_df)}")

    # ==========================================
    # EXCLUSION: Missing Link Columns
    # ==========================================
    link_cols = [
        'identifier', 'references_multimedia', 'bibliographicCitation', 
        'references', 'associatedReferences', 'occurrenceID'
    ]
    existing_link_cols = [col for col in link_cols if col in master_df.columns]
    
    if existing_link_cols:
        master_df[existing_link_cols] = master_df[existing_link_cols].replace(r'^\s*$', pd.NA, regex=True)
        master_df = master_df.dropna(subset=existing_link_cols, how='all')
    print(f"6. Rows remaining after empty-link validation drop: {len(master_df)}")

    # ==========================================
    # FLAGS: Inspection Issues (Doesn't drop rows, just flags them)
    # ==========================================
    inspect_issues = [
        "COORDINATE_REPROJECTED", "COUNTRY_MISMATCH", "COUNTRY_INVALID", 
        "COUNTRY_DERIVED_FROM_COORDINATES", "CONTINENT_INVALID", 
        "CONTINENT_DERIVED_FROM_COORDINATES", "RECORDED_DATE_MISMATCH", 
        "RECORDED_DATE_INVALID", "RECORDED_DATE_UNLIKELY", "TAXON_MATCH_FUZZY", 
        "TAXON_MATCH_HIGHERRANK", "SCIENTIFIC_NAME_ID_NOT_FOUND", 
        "TAXON_CONCEPT_ID_NOT_FOUND", "TAXON_ID_NOT_FOUND", "ELEVATION_MIN_MAX_SWAPPED", 
        "ELEVATION_NON_NUMERIC", "MODIFIED_DATE_INVALID", "IDENTIFIED_DATE_INVALID", 
        "TYPE_STATUS_INVALID", "MULTIMEDIA_DATE_INVALID", "MULTIMEDIA_URI_INVALID", 
        "INDIVIDUAL_COUNT_CONFLICTS_WITH_OCCURRENCE_STATUS", "OCCURRENCE_STATUS_UNPARSABLE", 
        "OCCURRENCE_STATUS_INFERRED_FROM_INDIVIDUAL_COUNT", "AMBIGUOUS_INSTITUTION", 
        "AMBIGUOUS_COLLECTION", "INSTITUTION_MATCH_NONE", "COLLECTION_MATCH_NONE", 
        "INSTITUTION_MATCH_FUZZY", "COLLECTION_MATCH_FUZZY", "INSTITUTION_COLLECTION_MISMATCH"
    ]
    inspect_pattern = '|'.join(inspect_issues)
    master_df['inspect_flag'] = (
        master_df['issue'].notna() & 
        master_df['issue'].astype(str).str.contains(inspect_pattern, na=False)
    )
    
    # Count how many are flagged for your reference
    flagged_count = master_df['inspect_flag'].sum()
    print(f"7. Total rows FLAGGED for manual Excel inspection: {flagged_count}")
    print("----------------------------------------\n")

    master_df = master_df.dropna(axis=1, how='all')
    master_df.to_csv(output_csv, index=False)
    return output_csv

    
def clean_and_prepare_data_with_R(phase, occurrence_file, multimedia_file, output_csv="GBIFdownload_inspectFlags.csv"):

    # Calls the R script, passes parameters, and streams the R console output in real-time.
    script_dir = Path(__file__).parent
    r_script_path = script_dir / "cleaner.R"

    # Construct the command line arguments
    cmd = ["Rscript", "--vanilla", str(r_script_path), str(phase), str(occurrence_file), str(multimedia_file), str(output_csv)]
    
    try:
        # Use Popen to stream stdout and stderr line-by-line in real time
        with subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, 
            text=True, 
            bufsize=1
        ) as process:
            
            # Read line by line as R prints it
            for line in process.stdout:
                print(line, end="")  # end="" because the line already has a newline character
                
        # Check if the process exited with an error code
        if process.returncode != 0:
            print(f"\nR script exited with error code: {process.returncode}", file=sys.stderr)
            
    except FileNotFoundError:
        print("Error: 'Rscript' executable not found. Please ensure R is added to your system PATH.")


def finalize_dataset(inspected_csv, final_master="master_cleaned.csv", final_duplicates="removed_duplicates.csv"):
    """Processes the manually inspected file, removes duplicates, and exports the final datasets."""
    print(f"Loading manually inspected file: {inspected_csv}")
    master_df = pd.read_csv(inspected_csv, keep_default_na=True, low_memory=False)

    # Apply manual removals if the 'Action' column exists
    if 'Action' in master_df.columns:
        master_df = master_df[(master_df['Action'].isna()) | (master_df['Action'] != 'Remove')]

    # Remove duplicates
    master_df_ND = master_df.drop_duplicates(subset=['decimalLatitude', 'decimalLongitude'], keep='first')
    
    # Isolate the exact duplicates that were removed
    removed_coords = master_df[master_df.duplicated(subset=['decimalLatitude', 'decimalLongitude'], keep='first')]
    removed_coords = removed_coords.dropna(axis=1, how='all')

    # Create new blank columns for ImageJ measurements
    master_df_ND['Panicle length (cm)'] = ""
    master_df_ND['Leaf width (cm)'] = ""
    master_df_ND['Seed length (cm)'] = ""

    # Export
    master_df_ND.to_csv(final_master, index=False)
    removed_coords.to_csv(final_duplicates, index=False)
    
    print(f"Successfully exported final cleaned dataset to {final_master}")