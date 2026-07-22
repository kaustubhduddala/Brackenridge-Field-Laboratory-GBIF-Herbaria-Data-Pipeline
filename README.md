# GBIF Download and Data Preparation Project

This project automates the download of a GBIF Darwin Core Archive for a target species and prepares the data for inspection and final cleaning.

## Project structure

- `main.py`
  - Entry point for the current workflow.
  - Uses the PythonAutomation package to resolve the species, submit a GBIF download request, wait for the archive to build, download the ZIP file, and extract it.
  - Supports three modes: download and prepare data, download only, and prepare only.

- `PythonAutomation/prepare_data.py`
  - Contains helper functions for GBIF API access and archive extraction.
  - Includes:
    - `resolve_species` to fetch the GBIF taxon key for a scientific name.
    - `trigger_download` to submit download requests.
    - `wait_and_download` to poll GBIF until the archive is ready and fetch it.
    - `extract_archive` to unzip the downloaded Darwin Core Archive.
    - `clean_and_prepare_data_with_R` to run the existing R cleaning workflow.
    - `clean_and_prepare_data` as an AI-adapted Python version of data preparation.
    - `finalize_dataset` to process the manually inspected CSV and export final cleaned outputs.

- `PythonAutomation/cleaner.R`
  - R script used for the original data preparation workflow.
  - Called from Python when `DATA_PREPARATION_VERSION` is set to 0.

- `Data/`
  - Holds downloaded and extracted GBIF data as well as prepared files.
  - Contains `ProtocolData` with Darwin Core files and metadata used by the project.

- `G064 Herbaria Image Measurement Protocol for MaxEnt (Revised)/`
  - Project documentation and protocol reference files.

## Key workflow

1. Set the target species in `main.py` at `TARGET_SPECIES`.
2. Set the GBIF credentials in `main.py` under `GBIF_USER`, `GBIF_PASSWORD`, and `GBIF_EMAIL`.
3. Run `main.py`.
4. Choose one of the options:
   - Download and prepare data
   - Download data only
   - Prepare data only
5. If downloading, the code will:
   - Look up the taxon key.
   - Submit the GBIF download request.
   - Poll GBIF until the archive is ready.
   - Download the ZIP archive.
   - Extract the archive to a folder named `./data_<downloadKey>`. -- this may take a couple of minutes
6. If preparing data, the code may:
   - Run the R script (`DATA_PREPARATION_VERSION = 0`).
   - Or run the Python cleaning path (`DATA_PREPARATION_VERSION = 1`).

## Data preparation details

- The current code supports two data preparation versions.
- Version 0 runs the original R script and requires `Rscript` in the system path.
- Version 1 is an experimental Python adaptation and is labeled as not stable.
- The R workflow processes the extracted `occurrence.txt` and `multimedia.txt` files.
- After manual inspection, `finalize_dataset` exports cleaned outputs.

## Notes and caveats

- The ZIP archive can be large and extraction can take time.
- Avoid extracting directly in a synced cloud folder if performance is slow.
- The `clean_and_prepare_data` Python function is not fully stable and is intended for future replacement.
- Always verify the manual review step before finalizing the cleaned dataset.

## Requirements

- Python 3
- `pygbif`
- `pandas`
- `requests`
- `R` and `Rscript` if using the R preparation workflow

## Usage example

Run the project from the repository root with:

```bash
python3 main.py
```

Follow the prompts to choose download or preparation mode.
