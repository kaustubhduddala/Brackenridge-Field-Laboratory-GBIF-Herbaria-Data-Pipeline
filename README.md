# GBIF Herbaria Data Pipeline

A single-file Python tool for downloading, cleaning, and preparing
GBIF herbarium occurrence data for morphological trait measurement
in ImageJ. Supports both a graphical interface (tkinter) and a
command-line interface.

The pipeline was developed for the G064 Guinea Grass Biogeography
project at UT Austin, based on the measurement protocol by
Cristopher Ferreon, Kat Tisshaw, Aaron Rhodes, and Kaustubh Duddala. It replaces the
original multi-file R + Python workflow with one self-contained
script.

---

## Table of Contents

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Quick Start](#quick-start)
4. [Interfaces](#interfaces)
5. [Workflow Overview](#workflow-overview)
6. [Presets](#presets)
7. [Filters](#filters)
8. [Output Files](#output-files)
9. [Auto-Measurement (ML)](#auto-measurement-ml)
10. [CLI Reference](#cli-reference)
11. [Configuration](#configuration)
12. [Troubleshooting](#troubleshooting)

---

## Requirements

- Python 3.9 or later
- tkinter (included with most Python installations; see
  Troubleshooting if missing)
- A GBIF account (credentials are configured inside the script)

Third-party packages (required):

| Package  | Purpose                              |
|----------|--------------------------------------|
| requests | GBIF species lookup via REST API     |
| pandas   | Tabular data loading and cleaning    |
| pygbif   | GBIF occurrence download management  |

Additional packages for ML auto-measurement (optional):

| Package      | Purpose                                      |
|--------------|----------------------------------------------|
| torch        | PyTorch backend for model inference          |
| torchvision  | Image transforms required by VLM processors  |
| transformers | HuggingFace model loading and tokenization   |
| accelerate   | Device mapping and mixed-precision inference |
| pillow       | Image loading and preprocessing              |

The pipeline works without the ML packages. The auto-measurement
features are disabled at startup if they are not installed.

---

## Installation

### 1. Clone or download the repository

Place `main.py` in a project directory of your choice.

### 2. Create a virtual environment (recommended)

```
cd /path/to/project
python -m venv venv
```

Activate it:

```
# macOS / Linux
source venv/bin/activate

# Windows (Command Prompt)
venv\Scripts\activate.bat

# Windows (PowerShell)
venv\Scripts\Activate.ps1
```

### 3. Install dependencies

If a `requirements.txt` is provided:

```
pip install -r requirements.txt
```

Otherwise install the three packages directly:

```
pip install requests pandas pygbif
```

### 4. Create a requirements.txt (optional)

To generate one from your current environment:

```
pip freeze > requirements.txt
```

Or create a minimal one manually:

```
requests
pandas
pygbif
```

---

## Quick Start

Run the launcher and choose an interface:

```
python main.py
```

You can also skip the launcher and choose mode directly:

```
python main.py --gui
python main.py --cli
```

To skip the launcher and run a cleaning phase directly:

```
python main.py cleaner 1 data/occurrence.txt data/multimedia.txt
python main.py cleaner 2 GBIFdownload_inspectFlags.csv
```

---

## Interfaces

### GUI

The graphical interface presents all options in a single window:

- Workflow selection (Download Only, Prepare Only, Full Workflow)
- Species name entry
- Data folder / ZIP file browser
- Phase selection (Phase 1 and 2, Phase 1 only, Phase 2 only)
- Filter controls: preset selector, coordinate precision, taxa
  exclusion list, and an issue filter dialog
- Console output panel
- Run, Cancel, and Clear buttons

The issue filter dialog (opened via the "Configure Issue Filters"
button) contains two tabs with checkboxes for every recognized GBIF
issue. Each checkbox shows the issue name and a plain-English
description of what it means. Select All and Deselect All buttons
are provided for each tab.

### CLI

The command-line interface presents an interactive menu with the
same three workflow options. It uses the default filter preset
(G064) and prompts for input at each step.

---

## Workflow Overview

The pipeline runs in three stages. You can run them individually
or together.

### Stage 1: Download

1. Resolve the species name to a GBIF taxon key via the GBIF
   species match API.
2. Submit a Darwin Core Archive download request to GBIF with
   these predicates: preserved specimen, has coordinates, occurrence
   status present.
3. Poll GBIF until the download completes (typically 5-30 minutes).
4. Download and extract the ZIP archive.

### Stage 2: Phase 1 -- Automated Cleaning

Operates on the `occurrence.txt` and `multimedia.txt` files from
the extracted archive.

1. Load both files and rename multimedia columns to avoid conflicts.
2. Merge on `gbifID` (outer join).
3. Filter by coordinate precision (configurable: none, relaxed, or
   strict).
4. Remove rows matching excluded taxa across `scientificName`,
   `infraspecificEpithet`, and `verbatimScientificName`.
5. Remove rows containing any active bad geospatial issues.
6. Validate that at least one reference link column has data
   (`identifier`, `references_multimedia`, `bibliographicCitation`,
   `references`, `associatedReferences`, or `occurrenceID`).
7. Flag rows that contain any active inspection issues (adds an
   `inspect_flag` column).
8. Export to `GBIFdownload_inspectFlags.csv`.

At this point, open the CSV in Excel or another spreadsheet
application. Sort by `inspect_flag`, review flagged records, and
write "Remove" in the `Action` column for any rows that should be
dropped (cultivated specimens, botanic garden records, suspicious
localities, etc.).

### Stage 3: Phase 2 -- Finalization

1. Load the manually reviewed CSV.
2. Drop rows where `Action` equals "Remove".
3. Deduplicate by coordinate (keep the first occurrence).
4. Add empty columns for ImageJ measurements: Panicle length (cm),
   Leaf width (cm), Seed length (cm).
5. Export `master_cleaned.csv` (the working dataset) and
   `removed_duplicates.csv` (backup of dropped coordinate
   duplicates).

---

## Presets

Presets configure all filter settings at once. Select one from the
dropdown in the GUI.

### G064 (Default)

The standard protocol for the G064 Guinea Grass project.

- Species: Megathyrsus maximus
- Coordinate precision: Relaxed (at least 1 decimal place)
- Taxa exclusions: 8 varieties and synonyms removed (see
  DEFAULT_EXCLUDE_TAXA in the source)
- Bad geospatial issues: All 11 enabled (records removed)
- Inspection issues: All enabled (records flagged)

### G024

A permissive configuration that retains the broadest possible
dataset for biogeography analysis.

- Species: Megathyrsus maximus
- Coordinate precision: None (no filtering; keeps records with
  integer coordinates or no decimal places)
- Taxa exclusions: None (all varieties and synonyms retained)
- Bad geospatial issues: All disabled (no records removed for
  geospatial problems)
- Inspection issues: All enabled (records flagged for review)

### Custom

A blank starting point. All fields are editable, including the
taxa exclusion text box. Bad and inspection issues default to all
enabled.

---

## Filters

### Coordinate Precision

Controls how many decimal places are required in `decimalLatitude`
and `decimalLongitude` for a record to be kept.

| Mode    | Behavior                                      |
|---------|-----------------------------------------------|
| None    | No filtering. All rows kept regardless.       |
| Relaxed | Requires at least 1 decimal place.            |
| Strict  | Requires at least 3 decimal places.           |

### Taxa Exclusions

A list of scientific names (one per line). Any row whose
`scientificName`, `infraspecificEpithet`, or
`verbatimScientificName` matches an entry is removed. The text box
is editable when the Custom preset is selected; it is locked for
built-in presets.

### Issue Filters

Opened via the "Configure Issue Filters" button. Two categories:

**Remove Records** -- Bad geospatial issues. If a record's `issue`
field contains any checked issue in this list, the record is
dropped. There are 11 issues in this category (e.g., Coordinate
Rounded, Geodetic Datum Invalid, Country Coordinate Mismatch).

**Flag for Inspection** -- Inspection issues. If a record's
`issue` field contains any checked issue in this list, the
`inspect_flag` column is set to True. Records are not removed, only
marked for manual review. There are 37 issues in this category
(e.g., Taxon Match Fuzzy, Recorded Date Invalid, Multimedia URI
Invalid, Presumed Negated Latitude).

Each checkbox includes a short description explaining what the GBIF
issue means and why it matters.

---

## Output Files

| File                              | Contents                                           |
|-----------------------------------|----------------------------------------------------|
| `GBIFdownload_inspectFlags.csv`   | Phase 1 output. Cleaned and merged dataset with `inspect_flag` column. Open in Excel for manual review before Phase 2. |
| `master_cleaned.csv`              | Phase 2 output. Final deduplicated dataset with empty ImageJ measurement columns. This is the working file for measurements. |
| `removed_duplicates.csv`          | Phase 2 output. Records that were removed as coordinate duplicates. Kept as a backup for backfilling if primary vouchers are inaccessible. |

---

## Auto-Measurement (ML)

The pipeline can optionally use a vision-language model to
automatically measure panicle length, leaf width, and seed length
from herbarium voucher images. The default model is
Qwen/Qwen3-VL-30B-A3B-Instruct, a 30B-parameter mixture-of-experts
model with 3B active parameters per inference pass.

### How it works

For each row in a CSV, the engine looks for an image URL in the
link columns (identifier, references_multimedia, etc.), downloads
the image, sends it to the model with a structured measurement
prompt, and parses the JSON response back into the Panicle length,
Leaf width, and Seed length columns. A notes column (ml_notes)
records any issues per row.

Rows that already have measurements are skipped automatically so
you can resume an interrupted run.

### GPU requirements

Running the default model in bfloat16 requires roughly 16-18 GB
of GPU memory. On systems without a CUDA GPU the model falls back
to CPU and float32, which works but is significantly slower. For
large datasets, a GPU is strongly recommended.

### Managing models (GUI)

Open Models > Manage Models from the menu bar. The dialog shows
all registered models, their download status, and which is set as
the default.

From this dialog you can:

- Download a registered model to the local HuggingFace cache
- Set a different model as the default
- Register a new model by entering its HuggingFace ID (e.g.
  Qwen/Qwen2.5-VL-7B-Instruct) or a local directory path
- Remove a model from the registry

### Managing models (CLI)

The CLI auto-measure option (choice 4 in the interactive menu)
prompts for a model ID and downloads it automatically if not
already cached.

### Running auto-measurement (GUI)

In the Auto-Measurement (ML) section of the main window:

1. Select a model from the dropdown (only registered models appear;
   use Manage Models to add more).
2. Browse to the CSV file you want to measure (typically
   master_cleaned.csv from Phase 2).
3. Click Run Auto-Measurement.
4. Progress appears in the console. Measurements are written
   directly into the CSV.

### Running auto-measurement (CLI)

From the interactive CLI, select option 4. Or invoke directly:

```
python main.py measure master_cleaned.csv
python main.py measure master_cleaned.csv Qwen/Qwen2.5-VL-7B-Instruct
```

### Adding a custom model

Any HuggingFace vision-language model that follows the standard
transformers chat-template interface can be registered. The model
must accept image+text input and produce text output. Register it
via the GUI dialog or by editing the config file at
~/.gbif_pipeline/config.json.

---

## CLI Reference

### Interactive launcher

```
python main.py
```

Prompts for GUI (1) or CLI (2), or you can skip the prompt with:

```
python main.py --gui
python main.py --cli
```

### Direct phase invocation

Run Phase 1 (clean and merge):

```
python main.py cleaner 1 <occurrence_file> <multimedia_file> [output_csv] [--strict] [--no-precision]
```

Arguments:

- `occurrence_file` -- Path to occurrence.txt from the GBIF archive.
- `multimedia_file` -- Path to multimedia.txt from the GBIF archive.
- `output_csv` -- (Optional) Output filename. Default:
  `GBIFdownload_inspectFlags.csv`.
- `--strict` -- Use strict coordinate precision (3+ decimal places).
- `--no-precision` -- Skip coordinate precision filtering entirely.

If neither `--strict` nor `--no-precision` is given, relaxed mode
(1+ decimal place) is used.

Run Phase 2 (finalize):

```
python main.py cleaner 2 <inspected_csv> [final_master] [final_duplicates]
```

Run auto-measurement:

```
python main.py measure <csv_file> [model_id]
```

Arguments:

- `csv_file` -- Path to the CSV to measure (e.g. master_cleaned.csv).
- `model_id` -- (Optional) HuggingFace model ID or local path.
  Defaults to the model set in ~/.gbif_pipeline/config.json.

Arguments:

- `inspected_csv` -- Path to the manually reviewed CSV from Phase 1.
- `final_master` -- (Optional) Output filename. Default:
  `master_cleaned.csv`.
- `final_duplicates` -- (Optional) Output filename. Default:
  `removed_duplicates.csv`.

---

## Configuration

GBIF credentials and default filter lists are defined as constants
at the top of `gbif_pipeline.py`. Edit these directly in the source
file.

| Constant               | Purpose                                         |
|------------------------|-------------------------------------------------|
| `GBIF_USER`           | GBIF account username                            |
| `GBIF_PASSWORD`       | GBIF account password                            |
| `GBIF_EMAIL`          | Email for download notifications                 |
| `DEFAULT_EXCLUDE_TAXA`| Taxa excluded by the G064 preset                 |
| `BAD_GEOSPATIAL_ISSUES` | Issues that cause record removal               |
| `INSPECTION_ISSUES`   | Issues that cause record flagging                |
| `ISSUE_DESCRIPTIONS`  | Plain-English descriptions shown in the GUI      |
| `LINK_COLUMNS`        | Columns checked for reference link validation    |
| `PRESETS`             | Named filter configurations for the GUI          |
| `DEFAULT_MODEL_ID`   | HuggingFace ID of the default VLM                |
| `MEASUREMENT_PROMPT` | The structured prompt sent to the VLM             |
| `IMAGE_URL_COLUMNS`  | Column priority order for finding image URLs      |
| `CONFIG_DIR`         | Directory for pipeline config (~/.gbif_pipeline)  |

To add a new preset, add an entry to the `PRESETS` dictionary
following this structure:

```python
"My Preset": {
    "species": "Genus species",
    "precision": PRECISION_RELAXED,  # or PRECISION_NONE, PRECISION_STRICT
    "exclude_taxa": ["Taxon name A", "Taxon name B"],
    "bad_issues": True,   # True = all enabled, [] = none, or a list
    "inspect_issues": True,
},
```

---

## Troubleshooting

### tkinter is not installed

On Debian/Ubuntu:

```
sudo apt-get install python3-tk
```

On Fedora:

```
sudo dnf install python3-tkinter
```

On macOS with Homebrew Python, tkinter is included by default. If
it is missing, reinstall Python via Homebrew:

```
brew reinstall python-tk
```

On Windows, tkinter is included with the standard Python installer
from python.org.

If tkinter is not available, the CLI interface still works. Select
option 2 at the launcher prompt.

### GBIF download takes a long time

Large queries can take up to 3 hours. Most complete within 15
minutes. The script polls every 30 seconds and prints status
updates. You will also receive an email at the configured address
when the download is ready.

### pygbif authentication errors

Verify that `GBIF_USER`, `GBIF_PASSWORD`, and `GBIF_EMAIL` in the
script match your GBIF account. You can test your credentials at
https://www.gbif.org/user/profile.

### Phase 2 reports "No Action column found"

This is normal if you did not add an Action column during manual
review. Phase 2 will skip the manual-removal step and proceed
directly to deduplication.

### ML features are disabled / "ML unavailable" message

Install the optional ML dependencies:

```
pip install torch torchvision transformers accelerate pillow
```

If you have an NVIDIA GPU, install the CUDA version of PyTorch
instead. See https://pytorch.org/get-started/locally/ for the
correct install command for your system.

### Model download is very large

The default model (Qwen3-VL-30B-A3B) is approximately 16-18 GB.
Models are cached in the standard HuggingFace directory
(~/.cache/huggingface/) and only downloaded once. You can register
a smaller model (e.g. Qwen/Qwen2.5-VL-7B-Instruct at ~5 GB) via
the Manage Models dialog if storage or bandwidth is limited.

### Out of GPU memory when loading a model

Try a smaller model, or ensure no other GPU processes are running.
If no GPU is available the pipeline falls back to CPU automatically,
which is slower but has no memory ceiling beyond system RAM.

### Coordinates appear rounded in the output CSV

Pandas may display fewer decimal places than are stored. The
pipeline reads all values as strings during Phase 1 to preserve
original precision. If you open the CSV in Excel, check that the
column format is set to show enough decimal places.