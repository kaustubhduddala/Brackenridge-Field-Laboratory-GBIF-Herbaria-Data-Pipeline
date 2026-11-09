# GBIF Herbaria Data Pipeline

This is an ongoing project I'm building for the Invasive Species Lab at 
UT Austin's Brackenridge Field Laboratory. It's a python application for 
downloading GBIF herbarium occurrence data, cleaning it, downloading the
specimen images, and measuring morphological traits from those images 
with a local vision model. It has a graphical interface (tkinter) and 
a command-line interface.

The pipeline was developed for the G064 Guinea Grass Biogeography
project at UT Austin, based on the measurement protocol by
Cristopher Ferreon, Kat Tisshaw, Aaron Rhodes, and Kaustubh Duddala
(in `other/protocol/`).

## Contents

1. Requirements
2. Installation
3. Starting the application
4. Workflow
5. Measuring images
6. Presets and filters
7. Output files
8. Command line
9. Configuration
10. Project layout
11. Packaged releases
12. Troubleshooting

## 1. Requirements

- Python 3.12 or later (numpy and scipy need 3.12; the release builds
  use 3.12)
- tkinter for the graphical interface (see Troubleshooting if it is missing)
- A GBIF account for new downloads (not needed when downloading by DOI)
- For image measurements, one of: LM Studio (or another
  OpenAI-compatible server) with a vision model loaded, an API key for
  OpenAI, Google Gemini or Anthropic Claude, or trained models plus the
  built-in label reader (section 5)

Python packages are listed in `requirements.txt`. The ones the app uses
directly are numpy, pandas, requests, urllib3, pygbif, beautifulsoup4,
pillow, keyring, playwright (browser sign-in), openai and anthropic
(vision models), opencv-python-headless, scikit-image and scipy (image
measurement) and tksheet (the Edit tab); the rest are their
dependencies. The optional PyTorch and
label-reader add-ons are in `requirements-models.txt` (see "Model setup"
in section 5).

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
    analysis_tab.py  app_window.py  browser_sign_in.py  command_line.py
    csv_editing.py  csv_editor_tab.py  csv_statistics.py  dataset_cleaning.py
    download_folders.py  gbif_download.py  gui_widgets.py  image_download.py
    keep_awake.py  modify_tab.py  utils.py
    measure/     (everything that measures images)
        benchmark.py  calibration.py  decision.py  detectors.py  downloads.py
        geometry.py  labels.py  locator.py  model_setup.py  occlusion.py  ocr.py
        overlay.py  pipeline.py  providers.py  records.py  review_tab.py
        taxa.py  trained.py  training.py
        reference/  (example figures from the lab protocol)
tests/           (drawn sheets with known measurements; python -m unittest discover tests)
assets/          (entitlements.plist for macOS signing)
requirements-models.txt   (optional PyTorch and label-reader add-ons)
.github/workflows/release.yml
other/           (not part of the app, see below)
```

`other/` holds material that the app does not use:

- `other/protocol/`: the G064 measurement protocol the pipeline
  follows, as Markdown, LaTeX, HTML and the original Word document,
  with its screenshots in `other/protocol/assets/`
- `other/Data_original/`: the lab's original G024 spreadsheets and
  protocol data (not in git)

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

Every download lives in its own folder, `data/<download id>_<Species>/`
(for example `data/0012345-240101123456789_Megathyrsus_maximus/`). The
extracted GBIF files, every cleaned CSV, the images, the measurements
and the raw model answers for that download are all kept inside it, so
two downloads never overwrite each other. The species name is added
when it can be found; otherwise the folder is named by the download ID
alone. The **Download** box at the top of the window selects the active
download; choosing one fills in the file paths on every tab. A download
or a cleaning run makes its folder the active one automatically.

Every file and folder box in the app is a drop-down as well as having
a Browse button. The drop-down lists the files of the active download
first, then those of other downloads. Paths inside the active download
are shown as just the file name (`master_cleaned.csv`); paths in other
downloads are shown relative to `data/`, and anything else is shown in
full. You can also type a name or a full path.

### Tab 1: Download

Choose a preset and a species, or paste the DOI of an existing GBIF
download. The app resolves the species to a GBIF taxon key, requests a
Darwin Core Archive, waits for GBIF to prepare it (usually 5 to 30
minutes), then downloads it into its own folder under `data/` and
extracts it into that folder's `raw/` subfolder. When you paste a DOI,
the download ID is read from the link and the species is looked up
from GBIF.

"Run full workflow" does the download, phase 1 and phase 2 in one go.

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

Open that CSV ("Edit flagged records..." on the Clean tab opens it in
the Edit tab), review the flagged records, and type `Remove` in the
`Action` column for any record that should be dropped (cultivated
specimens, botanic garden records, suspicious localities and so on).

Running the steps together does not pause between phase 1 and phase 2.
To review first, run phase 1 on its own (untick the others), edit the
flagged records, then run phase 2.

Phase 2 then:

1. Drops rows marked Remove (not case-sensitive).
2. Merges rows that share a `gbifID`, keeping the first value and
   filling its empty cells from the duplicates.
3. Adds empty columns for your own measurements: Panicle length (cm),
   Leaf width (cm), Seed length (cm).
4. Saves `removed_duplicates.csv`, `master_cleaned.csv` and a copy with
   every column, `master_cleaned_all_columns.csv`.

Phase 3 then opens a checkbox list of every column found in the
pipeline's files (finalized dataset, current master, previously removed
columns and the phase 1 output). Tick the columns to keep. It rewrites
`master_cleaned.csv` with those columns and saves the others to
`removed_columns.csv`; both files keep `gbifID`, and `media` and the
measurement columns are always kept. Re-running phase 3 recomputes both
files from everything available, so you can bring a column back later.
The dialog has the same search and preset bar as the issue filters
(default: all columns). "Download + clean" keeps every column on its
first pass unless you chose columns beforehand with "Choose columns...".

The Steps box has a checkbox for each of the three phases. Tick any
combination; the review pause only happens when phases 1 and 2 both run.

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

"Measure new images" measures every image in the image folder that has
not been measured yet. Images that failed before are retried. The file
name (without extension) is used as the gbifID, and only images in the
image folder are measured.

Models only find things on the sheet (the ruler, labels, panicles,
leaves, seeds). The numbers come from the image geometry, and every
value gets a status: accept, needs review, lower bound or not
measurable. Section 5 explains how. "Find with" chooses what does the
finding: trained models when there are any, otherwise the vision model
chosen under Provider. "Read labels" chooses what transcribes the label:
the built-in OCR when it is installed, otherwise the vision model.
"Model setup..." (also under File) installs and downloads everything
optional; see "Model setup" in section 5.

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

### Review

Lists the measured sheets that need a person, least confident first: a
trait needs review, or is only a lower bound. Choose a sheet to see
what was measured drawn on it.

- Drag a point to correct a measurement. Scroll to zoom, drag the sheet
  to move it, double-click to fit it in the window.
- "Panicle", "Leaf" and "Seed" add a measurement from two clicks (base
  then tip, or the two margins of a leaf). The tool you pick is
  highlighted and stays on, so you can keep clicking to add more lines.
  It stops when you press Esc, press the same button again, or click
  something unrelated (the sheet list, a tab, the bottom buttons). Points
  that already exist can still be dragged while a tool is on, and the
  Fit to plant, Straighten and checkbox controls keep it on. "Scale..."
  sets one scale and then turns itself off. Delete removes the selected
  line.
- A panicle starts as a straight line between your two clicks. "Fit to
  plant" (in the Selected measurement box) then traces it along the plant,
  with the same lowest-cost path along the plant mask that the automatic
  measuring uses, so the route follows the panicle where it bends. If the
  route takes a shortcut, drag along the line to pull out a new point
  where it should go (the line stays fitted and is traced through the
  point), and press "Fit to plant" again whenever you want to re-trace.
  Drag a point to move it and double-click it to remove it. "Straighten"
  goes back to straight lines between the points, like a segmented line
  in ImageJ. A stretch the plant cannot be followed along is drawn
  straight and noted in the method. Leaf width and seed length are single
  straight lines between two points, so they take no extra points.
- The checkboxes mark the selected measurement as folded (not
  measurable), as having a hidden end (its value becomes a lower
  bound), or leave it out.
- "Scale..." sets the scale from two points a known distance apart,
  like Set Scale in ImageJ. Every value on the sheet is recomputed.
- "Accept sheet" stores the sheet as reviewed, rewrites its row in
  `measurements.csv` and saves a label in the download's `labels/`
  folder. Objects the pipeline rejected stay out unless you corrected
  them.

**Untouched sheets.** The drop-down above the list also offers
"Untouched sheets", the images that were never measured. Opening one
finds the scale from the ruler or the sheet width (image analysis, no
model), and you click the measurements yourself: Panicle (base, then
tip), Leaf (the two margins) and Seed (the two ends). A panicle is a
straight line until you press Fit to plant, which traces it along the
plant; a leaf or seed is the distance between its two points. "Accept sheet" stores them like any reviewed
sheet, so measuring sheets by hand is also how you build training data
from scratch, with or without LM Studio.

**Your hand values.** "Hand values..." loads a CSV of your own
measurements (columns `gbifID`, `Panicle length (cm)`, `Leaf width
(cm)`, `Seed length (cm)`; a file with "CFmeasurements" or
"hand_measurements" in its name in the download's folder is picked up
automatically). Each sheet then shows your value beside the measured
one, with the difference in percent, so you can check your clicks. A
table of lengths alone cannot train the landmark model, because it needs
to know where the points are, which is why the clicks are what is saved.

The buttons at the bottom right exchange labels with CVAT, Label
Studio or Roboflow as COCO files ("Export pre-labels..." writes what
the pipeline found so it can be corrected there, "Import labels..."
reads the corrections back) and train the models on the reviewed
sheets (section 5).

### Edit

Opens any CSV as a spreadsheet. Type in a cell to change it; copy,
paste, undo and find use the usual keys; right-click for sorting and
for inserting or deleting rows and columns; double-click a heading to
rename a column. "Filter rows" shows only the rows containing a text.

Every cell is kept as text exactly as it is in the file, so an "NA"
stays "NA" and an ID keeps its leading zeros. "Save" keeps the previous
version in `backups/` first ("Undo last change" on the Modify tab
brings it back); "Save as..." writes a new file. The Edit, Modify and
Analysis tabs share the same file.

### Modify

Works on any CSV, not only the pipeline's files. The drop-down at the
top lists the CSV files of the active download, files used recently
and the dataset; Browse picks any other file. The tools are:

- **Columns**: tick the columns to keep, move them up or down,
  rename them, and see how much of each column is filled in. "Uncheck
  empty columns" drops the empty ones. The removed columns can be saved
  to a separate file together with a match column, like phase 3 does.
- **Join two files**: pick a second CSV (file B) and the column each
  file is matched on, which can have different names. Choose the
  columns to carry over from each file; whether to keep every row of A,
  only matching rows, every row of B or everything; what to do when a
  column is in both files (keep both, prefer A, prefer B); and what to
  do when B has several rows for one key. Matching ignores capital
  letters and extra spaces by default. The number of matching rows is
  shown before you join.
- **Rows**: filter rows by a condition (equals, contains, is blank,
  greater than, matches a pattern and so on), remove duplicate rows by
  any set of columns, find and replace, sort, and tidy up (trim spaces,
  drop empty columns and rows). Filtering and de-duplicating can also
  save the rows that were taken out.
- **Combine files**: stack two or more CSVs, optionally adding a
  `source_file` column and removing identical rows.
- **Preview**: the first rows of file A, sortable and searchable.

"Save the result" at the bottom of the tab decides where a change goes:
a new file next to the original (named for the change, for example
`master_cleaned_joined.csv`), a file name you choose, or the original
itself. When the original is replaced the old version is kept in a
`backups/` folder next to it and **Undo last change** brings it back.
By default the result becomes the open file so changes can be chained.

### Analysis

Summaries of any CSV, with every table saveable as a CSV:

- **Overview**: rows, columns, blank cells, duplicate rows and
  duplicate or blank values of a key column (gbifID by default), and for
  every column its type, how much is filled in, the number of different
  values and the minimum, maximum, mean and median of numbers.
- **Value counts**: how often each value of a column appears, with a
  bar chart.
- **Numbers**: summary figures and a histogram for a numeric column.
- **Compare groups**: count, mean, median, minimum, maximum, standard
  deviation and sum of a numeric column for each group of another
  column, for example the measured leaf width per country.

### Sites that refuse image downloads, CAPTCHAs and bot checks

The app does not solve CAPTCHAs or disguise itself; a person passes the
check once, in a real browser window, and the app reuses the result.
When a site refuses a download (403 Forbidden, a "verify you are human"
or sign-in page) the app works through these steps, as far as the
**Method** preference allows:

1. A plain request, with any saved cookies, retried once with
   browser-like headers and a Referer.
2. The same download through a visible Chrome window (Playwright's
   Chromium if Chrome is not installed). If the site shows a check, the
   window stays open and the app waits (3 minutes by default) for you to
   pass it, then carries on by itself. Use **Skip this item** to give
   up on one image. Once this works, the site is downloaded through the
   browser from then on; the browser keeps its cookies in
   `browser_profile/`, so a passed check usually lasts.
3. If that is not possible (no browser, the check was not passed in
   time), the images are listed under the site's name. After three
   refusals in a row the rest of that site's images are skipped for the
   run so the site is not hammered.

When a run ends with refused sites, the **Site logins** window opens
(also under Images, **Site logins…**). It lists every site that has ever
refused access, with the number of images waiting:

- **Sign in…** opens the browser on the site so you can sign in or pass
  the check before downloading; click **Done, save my sign-in**. The
  cookies go to `site_logins.json` (readable only by you, ignored by
  git; the previous version is kept as `.bak`).
- **Retry** downloads that site's waiting images again.
- **Download through the browser** (per saved site) forces step 2 for
  that site, **Sign in again** refreshes expired cookies and **Forget**
  removes a site's sign-in.
- **When a site refuses a download** chooses the method: *Automatic*
  (the steps above), *Plain requests only* (never opens a browser),
  *Always use the browser* for refused sites, or *Ask me* (stop and
  list the site). It also sets how long to wait for you to pass a check
  and the pause between browser downloads (2 seconds by default, to
  stay polite). These are saved in `settings.json`.

Browser features need Playwright, which is in `requirements.txt` and is
bundled in the packaged app. Chromium itself is not bundled; if Chrome
is not installed the app offers to download it (about 150 MB) into
`browsers/` next to your data, so it survives app updates.

Sites that need their own download method have a branch in
`download_special()` in `program/image_download.py`; return the saved file's path
from a branch to take over the download for that site, or `None` to fall
back to the general downloader. To add a site, add an `if` on the host
and a handler next to the others (`_try_get()` makes one plain request,
`_save_image()` saves an image response, `_stitch()` joins tiles). Built in:

- **Meise Botanic Garden** (`oxalis.br.fgov.be`): the server answers 403
  to most User-Agent strings, including Python's and Chrome's, and accepts
  a bare `GBIF-HerbariaPipeline/1.0`, which every download now tries
  after the usual one. The image is joined from full-resolution IIIF
  regions when it is larger than the server's `maxArea`.
- **CRIA / speciesLink** (`images.cria.org.br/viewer/...`): the viewer
  page only wraps a Zoomify tile pyramid; the tiles of the largest level
  are fetched and joined into one JPEG, with the speciesLink download
  address as a fallback.
- **Smithsonian NMNH** (`collections.nmnh.si.edu/media/...`): the page
  redirects to the image on `ids.si.edu` but its firewall needs
  browser-like headers.
- **Google Drive** share links: the file's download address is used.

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

## 5. Measuring images

### How a sheet is measured

1. Scale first. The ruler is found and its tick spacing measured; with
   no usable ruler the standard 29 cm sheet width is used and the
   scale is marked uncertain. The DPI stored in the file is never
   trusted. With no scale, nothing on the sheet is measured. The
   paper's colour, cream or neutral white, is read from the sheet
   itself (the commonest bright, weakly coloured shade), and both the
   sheet edges and the plant mask are found against it.
2. Finding. A detector points at each panicle (base and tip), leaf
   (the two margins at the widest point) and seed (a box). It never
   produces a number.
3. Measuring, from the pixels:
   - Panicle length is the path along the plant between base and tip,
     so a curved panicle is not shortened to its straight-line distance.
   - Leaf width is the blade's width at its widest point, from its
     outline when there is one, otherwise from the blade's edges on the
     plant mask.
   - Seed length is the long axis of each spikelet; the sheet's value
     is the median, reported with the number of seeds measured.
4. Hidden ends. A panicle end that stops at a label, envelope or the
   edge of the sheet gives a lower bound, written to its own
   `..._lower_bound_cm` column and never to the value column.
5. Checks. Folded or overlapping leaves, tips placed part-way along a
   panicle, paths that leave the plant, objects of too few pixels and
   values outside the range for the taxon are flagged. With
   "Second-opinion checks" the vision model is also asked yes/no
   questions (is this tip hidden? is this leaf folded? is this one
   whole seed?); its answer counts as one vote and never changes a
   number.
6. Status, by rules written out in `program/measure/decision.py`:

| Status | When |
|---|---|
| accept | Found with confidence, clean geometry, enough resolution |
| needs review | Unusual value, two methods disagree, uncertain scale, few seeds, low confidence |
| lower bound | An end of the panicle is hidden |
| not measurable | No scale, too low resolution, folded, nothing found |

Each sheet also gets `responses/<gbifID>_overlay.jpg`, the sheet with
every outline, measured path and value drawn on it, and
`responses/<gbifID>.json` with every point and the raw model answers.

### The vision model (LM Studio and others)

1. In LM Studio, load a vision model and start the local server (the
   Developer tab, or `lms server start`).
2. In the Measure tab, check that the server address is correct
   (default `http://localhost:1234/v1`) and choose "Test connection".
3. Choose "Measure new images".

Leave Model empty to use the vision model loaded in LM Studio; the app
asks LM Studio which models are loaded before each run. OpenAI, Google
Gemini, Anthropic Claude and any OpenAI-compatible server can be used
instead by choosing them under Provider and saving an API key.

- Context length: set this when loading the model in LM Studio; 16,384
  or more is recommended.
- Token limit per image: stops a model that gets stuck. Set 0 for no
  limit; a model that starts repeating the same text is stopped anyway.
- Image size: the longest side, in pixels, of the images sent (0 uses the
  provider's default, 2560 for LM Studio). Large models spend most of
  their time reading the image, so a smaller size (1280 to 1792) can be
  much faster. If LM Studio runs out of memory the app retries with
  smaller images by itself.
- A model that cannot read images (LM Studio lists it as text-only) stops
  the run with a message instead of failing every sheet. Cancel and Skip
  work while the model is still reading; the status line shows how long
  it has been waiting.
- Thinking: "Low" or "Off" asks the model to reason less, where the
  server supports it.

A vision-language model is good at reading labels and at yes/no
questions, and poor at placing points precisely. It is the starting
point; trained models take over as sheets are reviewed.

### Model setup

File > Model setup... (or the button on the Measure tab) is one window for
everything optional, like the site logins window. Each item shows whether
it is ready, how big it is, and an Install or Download button; "Get
everything that is missing" does them all. Nothing is installed or
downloaded until you press a button, downloads can be cancelled and
resume where they stopped, and the window shows the free disk space.

| Item | Size | What it is for |
|---|---|---|
| Training and outlines add-on (PyTorch + Ultralytics) | about 2-3 GB | Runs trained models and SAM 2, and trains new ones |
| Label reader (RapidOCR) | about 140 MB | Reads labels on this computer, with no vision model |
| SAM 2 outlines | about 160 MB | Precise outlines of leaves and seeds |
| Starting weights | about 90 MB | The pretrained models your own models are trained from |

The same steps are available from the command line:

```
python main.py models install            # PyTorch + Ultralytics
python main.py models install-ocr        # the label reader
python main.py models download-sam
python main.py models download-weights
```

The packaged app cannot install packages; run from source for these.
(Ultralytics and RapidOCR are installed without their dependency lists so
that a second copy of OpenCV is not installed on top of the one the app
uses.) Ultralytics is licensed AGPL-3.0; check that this suits how you
distribute the app. RapidOCR is Apache-2.0.

### The built-in label reader

RapidOCR is an open-source text reader (PaddleOCR models converted to
ONNX) that runs on the CPU, with its models inside the package, so it
needs no vision model and no internet once installed. It reads one label
in about a second.

It reads printed text well: typed labels, stamps, barcode numbers. It
reads handwriting poorly and sometimes slips on single characters. It
returns the label text (`voucher_text`), and simple rules pull these
fields out of the text: catalogNumber, eventDate, scientificName,
recordedBy, recordNumber, county, locality and habitat. The rules need
the field name printed on the label (for example "LOCALITY"); every other
field stays empty and nothing is guessed. A vision model understands a
label better, so it stays available: "Read labels" on the Measure tab
chooses between the two. "Automatic" uses the OCR when it is installed.
With trained models and the label reader installed, a run needs no
vision model at all.

### Trained models

Small models fine-tuned on your own reviewed sheets find structures
faster and far more consistently than a vision-language model. They need
the add-on from Model setup. With it installed:

- SAM 2 outlines each object the detector points at, which gives the
  leaf and seed measurements a precise outline.
- "Train models..." on the Review tab (or `python main.py train pose`)
  fine-tunes a landmark model on the reviewed sheets: panicle base and
  tip, leaf margins, seed ends. At least 10 reviewed sheets are needed;
  100 to 200 that include the hard cases is a sensible first target.
  Sheets are split 70/15/15 into training, validation and test by
  specimen.
- `python main.py train seg` trains an outline model (panicle, leaf,
  seed, label, ruler, envelope, colour chart). It needs labels with
  outlines, which come from SAM 2 or from labels drawn in CVAT.
- Seeds are trained and found on full-resolution tiles, because on a
  downsized sheet they are only a few pixels long.

Trained models live in `models/` with a `registry.json`; the version
used is saved with every measurement in `ai_detector`. After training,
score the held-out sheets against hand measurements:

```
python main.py benchmark hand_measurements.csv --measure --split test
```

### Checking accuracy

`python main.py benchmark <hand measurements csv>` compares
`measurements.csv` with hand measurements (columns `gbifID`,
`Panicle length (cm)`, `Leaf width (cm)`, `Seed length (cm)`). For each
trait it reports error and bias for accepted values and for values sent
to review, how many of the values more than 20% off were sent to
review, how often no value was given where the hand sheet says NA, and
whether each lower bound really is below the hand value. `--measure`
measures the hand-measured images into `benchmark/<run>/` first.

### Measurement columns

| Column | Contents |
|---|---|
| `ai_image` | Image file that was measured |
| `ai_<trait>_cm` | The value in cm (accepted or needing review) |
| `ai_panicle_length_lower_bound_cm` | The minimum length when an end is hidden |
| `ai_<trait>_status` | accept, needs review, lower bound or not measurable |
| `ai_<trait>_confidence` | 0 to 1: detector score x geometric quality x scale quality, reduced for each review flag |
| `ai_<trait>_n`, `ai_<trait>_values_cm` | How many objects the value is built from, and their values |
| `ai_<trait>_method`, `ai_<trait>_flags` | How it was measured, and the flag codes raised |
| `ai_px_per_cm`, `ai_scale_source`, `ai_calibration` | The scale and where it came from (ruler, sheet, manual) |
| `ai_notes` | Why a value is missing or needs review, in words |
| `ai_review` | auto, or reviewed once accepted on the Review tab |
| `voucher_text`, `voucher_<field>` | Transcribed label text and details in Darwin Core terms |
| `ai_model`, `ai_detector`, `ai_pipeline` | Vision model, trained model versions and label reader (`labels=rapidocr`; `manual=reviewer` for sheets measured by hand), pipeline version |
| `ai_measured_at`, `ai_error` | When, and why the last attempt failed (empty on success) |

The traits are `panicle_length`, `leaf_width` and `seed_length`.
Definitions follow the project protocol: panicle length from the lowest
panicle node to the tip, leaf width from margin to margin at the widest
point (of the widest leaf measured), seed length from the branch point
to the tip. The plausible ranges for the taxon are in
`program/measure/taxa.py`.

A `measurements.csv` written by an earlier version is renamed to
`measurements_legacy.csv` (and `responses/` to `responses_legacy/`) the
first time images are measured, and a new file is started.

"Export Darwin Core records" (or `python main.py export-mof`) writes
`measurement_or_fact.csv`, one record per value using the Darwin Core
MeasurementOrFact terms. A lower bound is its own measurementType
("minimum panicle length").

Label transcriptions are tidied (one line, dates as YYYY-MM-DD) but a
small model may still misread a field, so check the voucher columns
before relying on them. CSV files are saved as UTF-8 with a byte-order
mark so Excel shows accented characters correctly.

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

"Edit issue filters..." reads the `issue` column of the occurrence file
in the selected data folder and lists only the issue codes present, with
a record count for each. Every row has its own Remove / Inspect / Nothing
buttons (Remove drops the records in phase 1, Inspect sets
`inspect_flag`). Use the search box to narrow the list and "Set all
shown to" to change every visible row at once. Assignments for codes that
are not in the data are kept.

The Preset bar at the top of the dialog saves the current assignments
under a name ("Save as..."), loads or deletes saved presets, and
"Reset to default" restores the app's defaults. Presets are stored in
`settings.json`.

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

All files are written to the active download's folder,
`data/<download id>_<Species>/`, unless you choose other paths.

| File | Contents |
|---|---|
| `workspace.json` | The download ID, species and where the download came from |
| `raw/` | The extracted GBIF download |
| `<download id>.zip` | The GBIF archive as downloaded |
| `GBIFdownload_inspectFlags.csv` | Phase 1 result, for manual review |
| `GBIFdownload_removed.csv` | Records removed in phase 1, with a `removal_reason` |
| `Missing_Media.csv` | Records with no media, with every link found in the row |
| `master_cleaned.csv` | Phase 2/3 result, the working dataset |
| `master_cleaned_all_columns.csv` | Phase 2 result with every column, read by phase 3 |
| `removed_columns.csv` | Columns dropped in phase 3, with `gbifID` |
| `removed_duplicates.csv` | Duplicate rows merged in phase 2 |
| `master_cleaned_failed_media.csv` | Records whose image could not be downloaded |
| `media/` | Downloaded images named by gbifID |
| `measurements.csv` | Image measurements, one row per gbifID |
| `measurement_or_fact.csv` | The same values as Darwin Core MeasurementOrFact records |
| `responses/` | Per image: `<gbifID>.json` with every point and the raw model answers, and `<gbifID>_overlay.jpg` |
| `labels/` | Reviewed labels, the training data for the models |
| `master_cleaned_before_join.csv` | The dataset as it was before the last join into it |
| `backups/` | Earlier versions of CSV files replaced on the Edit and Modify tabs |
| `<name>_joined.csv` and similar | Results saved by the Modify tab |
| `data/pipeline.log` | Everything shown in the app's log, with timestamps (one file for all downloads) |

## 8. Command line

```
python main.py                  open the app
python main.py --cli            interactive menu
python main.py --help           list the commands
python main.py --version        print the version
python main.py --self-test      check that everything the app needs can be loaded
```

Add `--help` (or `-h`) to any command below, for example
`python main.py cleaner --help`, to print its usage without running it.

Cleaning, phases 1 to 3:

```
python main.py cleaner 1 <occurrence.txt> <multimedia.txt> [output_csv] [--strict | --no-precision]
python main.py cleaner 2 [inspected_csv] [master_csv] [duplicates_csv]
python main.py cleaner 3 [--columns a,b,c]
```

Phase 1 uses at least 1 decimal place unless `--strict` (3 places) or
`--no-precision` is given. Phase 2 reads the active download's
`GBIFdownload_inspectFlags.csv` when no file is given. Phase 3 keeps
every column when `--columns` is left out.

Measurements:

```
python main.py measure [image_folder] [measurements_csv] [--ids 123,456] [--redo]
                     [--provider lmstudio|openai|gemini|anthropic|custom] [--model ID]
                     [--detector auto|vision|trained] [--no-sam] [--second-opinion]
                     [--labels auto|ocr|vision|off]
python main.py benchmark hand_measurements.csv [--measure | --measurements file.csv]
                     [--ids 123,456] [--split test] [--label NAME] [--key gbifID]
                     [--provider NAME] [--model ID] [--detector auto|vision|trained]
python main.py export-mof [measurements_csv] [--output file.csv]
python main.py models [status | install | download-sam | download-weights | install-ocr]
python main.py labels export [--prelabels] [--output file.json]
python main.py labels import corrected_coco.json
python main.py train [pose | seg | all] [--epochs N] [--imgsz N] [--batch N] [--base weights.pt]
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
`media`, `measurements.csv` and `master_cleaned.csv` of the newest download
folder.

Every command that reads or writes a download's files accepts
`--download NAME`, the name of a folder in `data/`, and uses the newest
download when it is left out. This includes all three `cleaner` phases.

To run the tests (59 of them, on drawn sheets with known measurements;
they need no model and take about a minute):

```
python -m unittest discover tests
```

## 9. Configuration

Most settings are in `config.py`:

| Setting | Purpose |
|---|---|
| `DATA_DIR` | Where the download folders are kept. The file names inside a download folder are defined in `program/download_folders.py` |
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

The prompts and answer formats for the vision model are in
`program/measure/locator.py` (`SYSTEM` and the `*_SCHEMA` constants).

## 10. Project layout

`main.py`, `config.py` and `build.py` are at the top level; everything
else is the `program` package. Modules only import from the ones above
them in this list, so there are no circular imports.

| File | Responsibility |
|---|---|
| `config.py` | Paths, GBIF account storage, presets, constants, saved settings |
| `program/utils.py` | CSV, URL, HTTP and file helpers; skip and cancel control |
| `program/download_folders.py` | The per-download data folders |
| `program/gbif_download.py` | GBIF species lookup and dataset downloads |
| `program/dataset_cleaning.py` | Phases 1-3 and coordinate cleaning tests |
| `program/image_download.py` | Image downloading: saved site sign-ins, browser-like retries, site-specific downloaders |
| `program/browser_sign_in.py` | The browser window used to sign in to a site (needs Playwright) |
| `program/measure/taxa.py` | The traits, and the plausible ranges for each taxon |
| `program/measure/providers.py` | Vision-model providers: LM Studio, OpenAI, Gemini, Claude, custom servers |
| `program/measure/locator.py` | Asks the vision model where things are, and yes/no check questions |
| `program/measure/downloads.py` | Downloads and package installs with progress, retries, resume and cancel |
| `program/measure/ocr.py` | The built-in label reader (RapidOCR) and the rules that pull fields out of label text |
| `program/measure/model_setup.py` | The Model setup window |
| `program/measure/trained.py` | The optional PyTorch add-on: trained YOLO seg/pose models, SAM 2, the model registry |
| `program/measure/detectors.py` | One interface over the vision model and the trained models |
| `program/measure/calibration.py` | Pixels per centimetre from the ruler, the sheet width or a reviewer |
| `program/measure/geometry.py` | Plant mask, geodesic paths, skeletons, widths, fold checks, spikelet axes |
| `program/measure/occlusion.py` | Whether an end is real or hidden by a label or the sheet edge |
| `program/measure/decision.py` | Flags, confidence and the accept / review / lower bound / not measurable rules |
| `program/measure/pipeline.py` | Measures one sheet, or a folder of sheets into the measurements CSV |
| `program/measure/overlay.py` | The picture of what was measured |
| `program/measure/records.py` | The measurements CSV, joining it into the dataset, Darwin Core export |
| `program/measure/labels.py` | Reviewed labels; COCO export and import |
| `program/measure/training.py` | Builds training sets from the labels and trains the models |
| `program/measure/benchmark.py` | Scores values and flags against hand measurements |
| `program/measure/review_tab.py` | The Review tab |
| `program/csv_editor_tab.py` | The Edit tab (spreadsheet) |
| `program/csv_editing.py` | Editing any CSV: columns, joins, rows, combining, backups |
| `program/csv_statistics.py` | Summaries used by the Analysis tab |
| `program/gui_widgets.py` | Shared widgets, the shared CSV picker, light and dark themes |
| `program/modify_tab.py`, `program/analysis_tab.py` | The Modify and Analysis tabs |
| `program/keep_awake.py` | Keeping the computer awake |
| `program/app_window.py` | The graphical interface |
| `program/command_line.py` | The command-line menu and commands |
| `main.py` | Entry point |
| `build.py` | Builds, signs and packages the app |
| `tests/` | Unit tests; `tests/synthetic.py` draws the test sheets |
| `assets/entitlements.plist` | macOS signing entitlements |
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

The zip is written to `dist/`. `python build.py --help` lists the
options; on a Mac with your Developer ID certificate in the keychain,
set `MACOS_SIGN_IDENTITY` (and the three `APPLE_` variables for
notarization) to sign a local build the same way. Each platform can only build its own
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

### Code signing

Unsigned apps show a warning on first launch. The workflow signs the
apps automatically once the secrets below are added under Settings,
then Secrets and variables, then Actions, then "New repository secret".
Until then, builds are published unsigned, and each platform is signed
as soon as its own secrets are present. Only release builds are signed;
pull request builds are not.

#### macOS: Developer ID and notarization

Cost: Apple Developer Program, 99 USD per year. A signed and notarized
app opens normally; macOS only asks once to confirm opening an app
downloaded from the internet.

1. Join the Apple Developer Program at https://developer.apple.com. An
   individual membership shows your name as the developer. An
   organization membership shows the organization's name and needs a
   D-U-N-S number; your university may already have one, so ask your
   department or IT office before paying.
2. On a Mac, create a "Developer ID Application" certificate: in Xcode,
   open Settings, then Accounts, select your team, choose "Manage
   Certificates", then the plus button. Only the account holder can
   create this certificate type.
3. In Keychain Access, find the certificate under "My Certificates",
   right-click it, choose Export, and save it as a `.p12` file with a
   password.
4. Copy the file as text for the secret:

```
base64 -i DeveloperID.p12 | pbcopy
```

5. Create an app-specific password at https://account.apple.com, under
   Sign-In and Security.
6. Find your Team ID under Membership details at
   https://developer.apple.com/account.

| Secret | Value |
|---|---|
| `MACOS_CERTIFICATE` | The base64 text from step 4 |
| `MACOS_CERTIFICATE_PASSWORD` | The password you gave the `.p12` file |
| `APPLE_ID` | Your Apple Account email |
| `APPLE_TEAM_ID` | The 10-character Team ID |
| `APPLE_APP_PASSWORD` | The app-specific password |

`build.py` signs every binary in the app with the hardened runtime,
submits the app to Apple for notarization (usually a few minutes),
staples the result to the app and checks it with Gatekeeper. If Apple
rejects it, the build fails and the log shows Apple's reasons. The
entitlements the app is signed with are in `assets/entitlements.plist`.

#### Windows: Azure Artifact Signing

Cost: about 10 USD per month (Basic plan). Available to individuals in
the USA and Canada and to organizations in the USA, Canada, the EU and
the UK. A signed app shows your verified name as the publisher.

1. Create an Azure account with a paid (pay-as-you-go) subscription;
   free and trial subscriptions are not accepted.
2. In the Azure portal, under Subscriptions, then Resource providers,
   register `Microsoft.CodeSigning`.
3. Create an Artifact Signing account (Basic). Note its name and the
   region's endpoint, for example `https://eus.codesigning.azure.net/`
   for East US.
4. In the account, start an identity validation for Public Trust, as an
   individual or for your organization. Microsoft checks it, which can
   take from a day to a few weeks.
5. Once validated, create a Public Trust certificate profile and note
   its name.
6. In Microsoft Entra ID, under App registrations, register an app (any
   name, for example "GitHub signing"). Note the Directory (tenant) ID
   and Application (client) ID, then under Certificates and secrets
   create a client secret and copy its value.
7. Back on the Artifact Signing account, under Access control (IAM),
   assign the role "Artifact Signing Certificate Profile Signer" to that
   app.

| Secret | Value |
|---|---|
| `AZURE_TENANT_ID` | Directory (tenant) ID |
| `AZURE_CLIENT_ID` | Application (client) ID |
| `AZURE_CLIENT_SECRET` | The client secret value |
| `AZURE_SIGNING_ENDPOINT` | The endpoint from step 3 |
| `AZURE_SIGNING_ACCOUNT` | The Artifact Signing account name |
| `AZURE_CERTIFICATE_PROFILE` | The certificate profile name |

Client secrets expire (at most after two years). Note the date and
create a new one before then, or signing will fail.

Signing does not remove the Windows warning immediately. SmartScreen
also looks at how many people have downloaded and run an app, so the
first releases may still show "Windows protected your PC", now with
your name as the publisher. The warning goes away as downloads
accumulate. To open the app while it shows, choose "More info", then
"Run anyway".

#### Opening an unsigned build

- Windows: choose "More info", then "Run anyway".
- macOS: unzip the app and move it to Applications, right-click it,
  choose Open, and confirm. If macOS says the app is damaged, run:

```
xattr -dr com.apple.quarantine "/Applications/GBIF Herbaria Pipeline.app"
```

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