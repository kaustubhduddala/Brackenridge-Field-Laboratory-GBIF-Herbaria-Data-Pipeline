# GBIF Herbaria Data Pipeline

A Python application for downloading GBIF herbarium occurrence data,
cleaning it, downloading the specimen images, and measuring
morphological traits from those images with a local vision model.
It has a graphical interface (tkinter) and a command-line interface.

The pipeline was developed for the G064 Guinea Grass Biogeography
project at UT Austin, based on the measurement protocol by
Cristopher Ferreon, Kat Tisshaw, Aaron Rhodes, and Kaustubh Duddala.

## Contents

1. Requirements
2. Installation
3. Starting the application
4. Workflow
5. Measuring images with LM Studio
6. Presets and filters
7. Output files
8. Command line
9. Configuration
10. Project layout
11. Packaged releases
12. Troubleshooting

## 1. Requirements

- Python 3.9 or later (3.11 or later recommended)
- tkinter for the graphical interface (see Troubleshooting if it is missing)
- A GBIF account for new downloads (not needed when downloading by DOI)
- LM Studio, or another OpenAI-compatible server, with a vision model
  loaded, for image measurements

Python packages are listed in `requirements.txt`: numpy, pandas,
requests, urllib3, pygbif, openai, beautifulsoup4, pillow and keyring.

## 2. Installation

Keep the files in this layout, with `main.py`, `config.py` and
`build.py` at the top level and the rest in `program/`:

```
main.py
config.py
build.py
requirements.txt
program/
    __init__.py
    analysis.py  cli.py  gbif.py  geo.py  gui.py
    media.py  power.py  processor.py  theme.py  utils.py
.github/workflows/release.yml
```

Then:

```
cd /path/to/project
python -m venv venv
```

Activate the environment:

```
# macOS / Linux
source venv/bin/activate

# Windows (Command Prompt)
venv\Scripts\activate.bat

# Windows (PowerShell)
venv\Scripts\Activate.ps1
```

Install the packages:

```
pip install -r requirements.txt
```

### GBIF account

New GBIF downloads need a free GBIF account; downloading by DOI does
not. In the app, open File, then GBIF account (on macOS also the app
menu's Settings). Enter your username, email and password, use "Test
sign-in" to check them with GBIF, then Save. The Download tab shows
which account is in use.

The password is kept in the macOS Keychain or Windows Credential
Manager, not in a plain file. If no system keychain is available it is
kept in `gbif_account.json` in the app's folder, readable only by your
user. "Remove saved account" deletes both.

The app also accepts, in this order of priority:

- Environment variables `GBIF_USER`, `GBIF_PASSWORD` and `GBIF_EMAIL`
- The account saved in the app
- A file at `~/credentials.json`:

```
{"user": "your_username", "password": "your_password", "email": "you@example.org"}
```

## 3. Starting the application

```
python main.py
```

This opens the graphical interface. If tkinter is not available it
falls back to the command-line menu. To open the menu directly:

```
python main.py --cli
```

The menu bar has File (GBIF account, open the data folder or log,
quit), View (theme and keep awake) and Help (About, which also shows
where your data is kept). The theme picker in the top right switches
between System, Light and Dark. Your theme, file paths and model settings are saved in
`settings.json` and restored the next time you open the app.

## 4. Workflow

The tabs follow the order of the work. Each step fills in the file
paths for the next one.

### Tab 1: Download

Choose a preset and a species, or paste the DOI of an existing GBIF
download. The app resolves the species to a GBIF taxon key, requests a
Darwin Core Archive, waits for GBIF to prepare it (usually 5 to 30
minutes), then downloads and extracts it into `data/`.

"Run full workflow" does the download, phase 1 and phase 2 in one go,
pausing for your manual review in between.

### Tab 2: Clean

Phase 1 reads `occurrence.txt` and `multimedia.txt` from the extracted
folder (or a ZIP) and:

1. Merges the two files on `gbifID` and adds a `media` column with the
   first image link found.
2. Moves records with no media to `Missing_Media.csv`.
3. Applies the coordinate precision filter, if one is selected.
4. Removes excluded taxa (checked against `scientificName`,
   `infraspecificEpithet` and `verbatimScientificName`).
5. Removes records on country centroids, in the ocean, or near major
   herbaria and botanic gardens.
6. Removes records with any issue code in the "Remove records" list.
7. Removes records with no reference link in any link column.
8. Sets `inspect_flag` to True for records with any issue code in the
   "Flag for inspection" list.
9. Adds an empty `Action` column and saves
   `GBIFdownload_inspectFlags.csv`.

Open that CSV, review the flagged records, and type `Remove` in the
`Action` column for any record that should be dropped (cultivated
specimens, botanic garden records, suspicious localities and so on).

Phase 2 then:

1. Drops rows marked Remove (not case-sensitive).
2. Merges rows that share a `gbifID`, keeping the first value and
   filling its empty cells from the duplicates.
3. Adds empty columns for your own measurements: Panicle length (cm),
   Leaf width (cm), Seed length (cm).
4. Saves `master_cleaned.csv` and `removed_duplicates.csv`.

### Tab 3: Images

"Download missing images" downloads the image for every record in the
dataset CSV that does not have one yet. Each file is saved as
`<gbifID>.<extension>` in the image folder, and its path is written to
the `media_path` column. The app follows IIIF manifests and web pages
to find the actual image. Failures are recorded in a `media_error`
column and listed in `<dataset>_failed_media.csv`.

"Choose records..." opens a checklist of every record, described in
"Choosing specific records or images" below.

### Tab 4: Measure

"Measure new images" sends every image in the image folder that has
not been measured yet to the model. Images that failed before are
retried. The file name (without extension) is used as the gbifID, and
only images in the image folder are measured.

Results go to a separate `measurements.csv`, one row per gbifID.
"Choose images..." opens the checklist so you can measure specific
images or measure some again.

"Join into dataset..." opens a window for adding the measurements to
the dataset CSV:

- Two column lists, one for the dataset and one for the measurements
  file. Check the columns to include from each. All dataset columns
  and the ai_ and voucher_ measurement columns are checked at first,
  and the app remembers your measurement choice for next time.
- "Match on" for each file sets the column the rows are matched by,
  gbifID by default. The window shows how many dataset rows match.
- Where a column is in both files, the measurement value is used, so
  joining again after measuring more images replaces the earlier
  results instead of duplicating them.
- "Keep only records that have a measurement" drops the rest.
- Output is either the dataset CSV itself, with the previous version
  kept as `<name>_before_join.csv`, or a new file of your choice.

Your own measurement columns are never changed unless you uncheck
them.

The option "Fill empty dataset fields with details read from the
voucher label" copies voucher details (for example `locality`,
`recordedBy`, `eventDate`) into dataset cells that are empty. It needs
the matching voucher_ columns checked. It never overwrites a value
that is already there, and the filled column names are recorded in
`voucher_filled_columns` so you can review them.

### Choosing specific records or images

The checklist window lists each gbifID with its status (for example
Downloaded, Not downloaded, Failed, Measured, Not measured) and a short
detail. You can:

- Click the box next to a gbifID, or highlight rows and press Space,
  to check or uncheck them
- Search, filter by status, and sort by clicking a column heading
- Use "Check shown" or "Uncheck shown" to check everything the current
  search and filter show, for example all Failed rows
- Paste a list of gbifIDs and choose "Check these"
- Preview the image and, for measured images, the calibration, notes
  and voucher text

"Download again" or "Measure again" controls whether checked items
that are already done are redone.

### Skip and Cancel

While images are downloading or being measured, the status bar shows
the progress, an estimate of the time left and, for measurements,
whether the model is thinking or writing its answer.

- "Skip this item" stops the current image and moves on to the next.
  A skipped image is left as it was, so it is picked up again the next
  time you run the step.
- "Cancel" stops the current image and ends the run. Everything
  finished so far is already saved.

Partly downloaded images are never left in the image folder.

### Keep computer awake

The "Keep computer awake" box in the top right stops the computer from
going to sleep while the app is open, which matters for long GBIF
downloads and measurement runs. The screen may still turn off. The
setting is remembered, and the computer can sleep normally again as
soon as you uncheck it or close the app. It uses
`SetThreadExecutionState` on Windows, `caffeinate` on macOS and
`systemd-inhibit` on Linux; if none is available the box is disabled.

## 5. Measuring images with LM Studio

1. In LM Studio, load a vision model and start the local server (the
   Developer tab, or `lms server start`).
2. In the Measure tab, check that the server address is correct
   (default `http://localhost:1234/v1`) and choose "Test connection".
   The log shows which model will be used and its context length, and
   warns if the context is too small or the model cannot read images.
3. Choose "Measure new images".

You do not choose a model name in the app. LM Studio needs one in each
request, because with its default just-in-time loading the name decides
which model answers (or which one gets loaded). So before each run the
app asks LM Studio which models are loaded and uses that model,
preferring a vision model if several are loaded. The name is
saved in the `ai_model` column. If nothing is loaded, the run stops
with a message instead of loading a model on its own.

You do not need to set anything in LM Studio's Structured Output
panel. Each request includes its own JSON schema, so the model's
answer always has the expected fields. If a server does not support
this, the app falls back to asking for JSON in the prompt.

### Settings that matter

- Context length: set this when loading the model in LM Studio. The
  image and instructions use roughly 3,000 to 5,000 tokens, and a
  thinking model needs room to reason and answer. 16,384 or more is
  recommended; 8,192 is often too small.
- Token limit per image: the most the model may generate for one
  image. It stops a model that gets stuck repeating its reasoning. Set
  0 for no limit.
- Thinking: "Low" or "Off" asks the model to reason less. Not every
  model or LM Studio version supports this; if the server rejects it,
  the app continues without it and says so in the log.

### What the model returns

Each answer is a JSON object like this, shortened:

```
{
  "calibration": "Kew ruler on the right edge, 1 cm ticks",
  "panicle_length_1_cm": 24.5, "panicle_length_1_confidence": 0.8,
  "panicle_length_2_cm": null, "panicle_length_2_confidence": null,
  "leaf_width_1_cm": 1.2, "leaf_width_1_confidence": 0.7,
  "notes": "Only one panicle visible; no seeds.",
  "voucher_text": "Bivona, Sic. Manip. IV. 6 (1816) ...",
  "voucher": {"catalogNumber": "K000674307", "locality": "Monte Pellegrino", "...": ""}
}
```

### Raw responses

Every answer is also saved unchanged as `data/responses/<gbifID>.json`,
including answers that could not be read. Each file holds the model
name, why the model stopped, the raw answer text, the model's
reasoning if it produced any, and the parsed values. Use these to check
a result or to see why one failed. In "Choose images...", select an
image and choose "Open raw model response".

Before values are written to `measurements.csv` they are tidied:

- Text is put on one line. The label transcription keeps its line
  breaks as " | ", so each record is one line in the CSV.
- Dates are written as YYYY-MM-DD, YYYY-MM or YYYY. A model answer such
  as 1929-03-00 becomes 1929-03.
- A confidence without a measurement is dropped, and confidences are
  kept between 0 and 1.

The CSV files are saved as UTF-8 with a byte-order mark, so Excel shows
accented characters in names and localities correctly.

Tidying cannot fix a wrong reading. Small models in particular may put
a value in the wrong field or misread a date, so check the voucher
fields before relying on them.

### Measurement columns

| Column | Contents |
|---|---|
| `ai_image` | Image file that was measured |
| `ai_<trait>_1_cm`, `ai_<trait>_2_cm` | Two independent measurements, in cm |
| `ai_<trait>_1_confidence`, `ai_<trait>_2_confidence` | Model confidence from 0 to 1 |
| `ai_<trait>_mean_cm` | Mean of the available measurements |
| `ai_calibration` | How the model calibrated the scale |
| `ai_notes` | Model notes, including why a value is missing |
| `voucher_text` | Transcribed label text |
| `voucher_<field>` | Label details in Darwin Core terms |
| `ai_model`, `ai_measured_at` | Model that answered and when |
| `ai_error` | Why the last attempt failed, empty on success |

The traits are `panicle_length`, `leaf_width` and `seed_length`.
Measurement definitions follow the project protocol: panicle length
from the lowest panicle node to the tip, leaf width at the broadest
point, seed length from the branch point to the tip.

Automated measurements should be checked against manual ones before
they are used in analysis.

## 6. Presets and filters

Presets fill in the species, precision, taxa and issue filters at
once. You can change any field after choosing a preset.

| Preset | Download filters | Precision | Excluded taxa | Remove issues | Flag issues |
|---|---|---|---|---|---|
| G064 (Default) | Preserved specimen | None | None | None | All 17 |
| G024 | Preserved specimen, occurrence status present | None | None | None | All 17 |
| Custom | Preserved specimen | None | None | None | All 17 |

Coordinate precision options: keep all coordinates, at least 1
decimal place, or at least 3 decimal places. Records without
coordinates are removed when a precision filter is on.

"Edit issue filters..." opens two lists of GBIF issue codes, one code
per line: records with a code in "Remove records" are dropped, and
records with a code in "Flag for inspection" get `inspect_flag` set to
True. Codes are matched exactly against the record's `issue` field.

To add a preset, add an entry to `PRESETS` in `config.py`:

```python
"My Preset": {
    "species": "Genus species",
    "precision": PRECISION_RELAXED,
    "exclude_taxa": ["Taxon name A", "Taxon name B"],
    "bad_issues": ["COORDINATE_ROUNDED"],
    "inspect_issues": INSPECTION_ISSUES,
    "filters": BASE_FILTERS + (("OCCURRENCE_STATUS", "PRESENT"),),
},
```

## 7. Output files

All files are written to `data/` unless you choose other paths.

| File | Contents |
|---|---|
| `GBIFdownload_inspectFlags.csv` | Phase 1 result, for manual review |
| `GBIFdownload_removed.csv` | Records removed in phase 1, with a `removal_reason` |
| `Missing_Media.csv` | Records with no media, with every link found in the row |
| `master_cleaned.csv` | Phase 2 result, the working dataset |
| `removed_duplicates.csv` | Duplicate rows merged in phase 2 |
| `master_cleaned_failed_media.csv` | Records whose image could not be downloaded |
| `media/` | Downloaded images named by gbifID |
| `measurements.csv` | Image measurements, one row per gbifID |
| `responses/` | The raw model answer for each image, as `<gbifID>.json` |
| `master_cleaned_before_join.csv` | The dataset as it was before the last join into it |
| `pipeline.log` | Everything shown in the app's log, with timestamps |

## 8. Command line

```
python main.py                  open the app
python main.py --cli            interactive menu
python main.py --help           list the commands
python main.py --version        print the version
python main.py --self-test      check that everything the app needs can be loaded
```

Phase 1 and phase 2:

```
python main.py cleaner 1 <occurrence.txt> <multimedia.txt> [output_csv] [--strict | --no-precision]
python main.py cleaner 2 <inspected_csv> [master_csv] [duplicates_csv]
```

Phase 1 uses at least 1 decimal place unless `--strict` (3 places) or
`--no-precision` is given.

Measurements:

```
python main.py measure [image_folder] [measurements_csv] [--ids 123,456] [--redo]
python main.py join [dataset_csv] [measurements_csv] [--fill-blanks] [--matched-only]
                     [--key gbifID] [--measurement-key gbifID] [--columns a,b,c] [--output file.csv]
```

`--ids` measures only the listed gbifIDs, measuring them again if
needed. `--redo` measures every image again.

For `join`, `--key` sets the dataset match column and
`--measurement-key` the measurements match column (both gbifID by
default). `--columns` limits which measurement columns are added,
`--output` writes a new file instead of updating the dataset, and
`--matched-only` keeps only records with a measurement. The defaults are
`data/media`, `data/measurements.csv` and `data/master_cleaned.csv`.

## 9. Configuration

Most settings are in `config.py`:

| Setting | Purpose |
|---|---|
| `DATA_DIR`, `MEDIA_DIR` and the CSV paths | Where files are read and written |
| `LMSTUDIO_URL` | Default model server address |
| `DEFAULT_TOKEN_LIMIT` | Default token limit per image |
| `MIN_CONTEXT_LENGTH` | Context length below which Test connection warns |
| `MAX_IMAGE_SIDE` | Images are scaled so the long side is at most this many pixels before they are sent to the model |
| `BAD_GEOSPATIAL_ISSUES`, `INSPECTION_ISSUES` | Default issue code lists |
| `PRESETS` | Named filter configurations |
| `LINK_COLUMNS`, `MEDIA_COLUMNS` | Columns searched for reference and image links |

Environment variables: `GBIF_USER`, `GBIF_PASSWORD`, `GBIF_EMAIL`
(these override the account saved in the app), `GBIF_PIPELINE_HOME`,
`LMSTUDIO_URL`, and `LMSTUDIO_MODEL`. Set `LMSTUDIO_MODEL` only to force
a specific model, for example with a server other than LM Studio.

The measurement prompt and the answer format are in `analysis.py`
(`SYSTEM_PROMPT` and `RESPONSE_SCHEMA`).

## 10. Project layout

`main.py`, `config.py` and `build.py` are at the top level; everything
else is the `program` package. Modules only import from the ones above
them in this list, so there are no circular imports.

| File | Responsibility |
|---|---|
| `config.py` | Paths, GBIF account storage, presets, constants, saved settings |
| `program/utils.py` | CSV, URL, HTTP and file helpers; skip and cancel control |
| `program/geo.py` | Coordinate cleaning tests |
| `program/gbif.py` | GBIF species lookup and dataset downloads |
| `program/processor.py` | Phase 1 and phase 2 |
| `program/media.py` | Image downloading |
| `program/analysis.py` | Model requests, measurements CSV and join |
| `program/power.py` | Keeping the computer awake |
| `program/theme.py` | Light and dark themes |
| `program/gui.py` | The graphical interface |
| `program/cli.py` | The command-line menu and commands |
| `main.py` | Entry point |
| `build.py` | Builds the packaged app with PyInstaller |
| `.github/workflows/release.yml` | Builds and publishes releases on GitHub |

## 11. Packaged releases

GitHub Actions builds a Windows app and two macOS apps (Apple Silicon
and Intel) and publishes them as a release on the repository's
Releases page every time something is merged into `main`. The workflow
is `.github/workflows/release.yml` and the build itself is `build.py`,
which uses PyInstaller.

### One-time setup

1. Create a repository on GitHub and push this folder to it, with
   `main.py` at the top level and the `.github` folder included.
2. In the repository, open Settings, then Actions, then General, and
   under "Workflow permissions" choose "Read and write permissions".
   The workflow needs this to create releases.
3. Optional but recommended: under Settings, then Branches, add a rule
   for `main` that requires a pull request and the "Build" checks to
   pass before merging. Then a change that breaks the app can never
   reach a release.

### What happens when

| Event | Builds and checks | Publishes a release |
|---|---|---|
| Pull request into `main` | Yes | No |
| Merge or push to `main` | Yes | Yes, numbered automatically |
| Pushing a tag such as `v2.0.0` | Yes | Yes, using the tag as the version |
| "Run workflow" in the Actions tab | Yes | No; the zips are attached to the run |

Pushes that only change Markdown files or `.gitignore` do not start a
build.

Each build installs the requirements, runs `python main.py --self-test`
to check that every module and package loads, builds the app, and runs
the same self-test on the packaged app. A release is only published if
all three platforms pass. It contains:

- `GBIF-Herbaria-Pipeline-v1.0.12-windows-x64.zip`
- `GBIF-Herbaria-Pipeline-v1.0.12-macos-arm64.zip`
- `GBIF-Herbaria-Pipeline-v1.0.12-macos-intel.zip`

The release notes list the pull requests merged since the previous
release, so descriptive pull request titles make useful notes. The
newest release is marked "Latest" and appears on the repository's main
page.

### Version numbers

Releases from `main` are numbered `v<BASE_VERSION>.<run number>`, for
example `v1.0.12`. The run number goes up by one with every run of the
workflow, so numbers may skip but never repeat. For a bigger update,
change `BASE_VERSION` near the top of the workflow file, for example
from `"1.0"` to `"1.1"`.

To choose a version yourself instead, push a tag:

```
git tag v2.0.0
git push origin v2.0.0
```

A tag with a hyphen, such as `v2.1.0-beta1`, is published as a
pre-release and is not marked "Latest".

The version appears in the window title and in
`python main.py --version`.

### Building locally

```
pip install pyinstaller
python build.py v1.2.0
```

The zip is written to `dist/`. Each platform can only build its own
app, which is why the workflow uses three runners. To give the app an
icon, add `assets/icon.ico` (Windows) and `assets/icon.icns` (macOS).

### Where the packaged app keeps its files

The packaged app stores `data/` and `settings.json` in
`Documents/GBIF Herbaria Pipeline` in your home folder, because the
app folder itself may be read-only. When run from source they stay
next to `main.py`. Set the environment variable `GBIF_PIPELINE_HOME`
to use a different folder in either case.

The packaged app has no console, so the command-line menu and
commands are only available when running from source.

### Opening an unsigned app

The builds are not code-signed, so the first launch shows a warning.

- Windows: SmartScreen says it protected your PC. Choose "More info",
  then "Run anyway".
- macOS: unzip the app and move it to Applications. Then right-click
  it and choose Open, and confirm. If macOS says the app is damaged,
  run this once in Terminal:

```
xattr -dr com.apple.quarantine "/Applications/GBIF Herbaria Pipeline.app"
```

Removing these warnings requires a paid Apple Developer ID for macOS
signing and notarization, and a code-signing certificate for Windows.
Both can be added to the workflow later as repository secrets.

GitHub's Intel macOS runner (`macos-15-intel`) is the last one GitHub
plans to offer and is available until August 2027. After that, remove
the Intel entry from the workflow's build matrix.

## 12. Troubleshooting

### tkinter is not installed

On Windows and macOS it comes with the python.org installer. On Linux:

```
sudo apt-get install python3-tk      # Debian / Ubuntu
sudo dnf install python3-tkinter     # Fedora
```

With Homebrew Python on macOS: `brew install python-tk`.

### "The model stopped ... without finishing its answer"

The model used up its tokens, usually while thinking. Raise the context
length for the model in LM Studio, raise the token limit, or set
Thinking to Low or Off. The image is retried on the next run.

### "Could not reach LM Studio" or "no model is loaded"

Start the LM Studio server, load a vision model, check the server
address, and use "Test connection".

### A measurement is clearly wrong

Open "Choose images...", select the image to see its preview and the
model's calibration notes, then check it with "Measure again"
selected. Keep the automated values separate from your manual ones
until they have been checked.

### GBIF download takes a long time

Large requests can take hours; most finish within 30 minutes. The app
checks every 30 seconds, and GBIF also emails you when the file is
ready. Wait in the app, or paste the DOI from that email later.

### pygbif authentication errors

Open File, then GBIF account, and use "Test sign-in". If it fails,
check the username and password by signing in at https://www.gbif.org.
If GBIF_ environment variables are set, they are used instead of the
account saved in the app.

### Coordinates look rounded in Excel

The pipeline keeps coordinates exactly as GBIF provides them. Excel may
display fewer decimal places; widen the column or change its number
format.