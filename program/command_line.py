import re
import sys
from pathlib import Path

from config import APP_TITLE, PRECISION_NONE, PRECISION_RELAXED, PRECISION_STRICT, load_settings
from .gbif_download import download_dataset
from .image_download import download_media
from .measure.pipeline import measure_images
from .measure.records import join_measurements
from .dataset_cleaning import phase_1_clean_and_merge, phase_2_finalize_dataset, phase_3_select_columns, run_cleaning
from .download_folders import list_workspaces, resolve_input, workspace_by_name, workspace_of


def _ask(prompt, default=""):
    value = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return value or str(default)


def _workspace(name=None):
    """The data folder named `name` (or its path); the newest download when blank."""
    spaces = list_workspaces()
    if not name:
        if not spaces:
            raise ValueError("No downloads yet. Download or clean some data first.")
        return spaces[0]
    ws = workspace_by_name(name) or workspace_of(name)
    if ws is None:
        raise ValueError(f"No download folder named {name}. Known folders: "
                         + (", ".join(w.name for w in spaces) or "none"))
    return ws


def _ask_workspace():
    spaces = list_workspaces()
    if spaces:
        print("Download folders: " + ", ".join(w.name for w in spaces))
    return _workspace(_ask("Download folder", spaces[0].name if spaces else ""))


def _download():
    query = _ask("Species name or DOI")
    if not query:
        print("A species name or DOI is required.")
        return None
    is_doi = query.lower().startswith(("http://", "https://", "doi:", "10."))
    folder = download_dataset(doi_text=query) if is_doi else download_dataset(species=query)
    print(f"Download complete: {folder}")
    return folder


def _clean():
    phases = {int(c) for c in _ask("Phases to run (any of 1, 2, 3)", "1,2,3") if c in "123"}
    if not phases:
        print("Invalid selection.")
        return
    if 1 in phases:
        folder, ws = resolve_input(_ask("Data folder or ZIP path"))
    else:
        folder, ws = None, _ask_workspace()
    master = run_cleaning(ws, folder, phases)
    if master:
        print(f"Final dataset: {master}")


def _full():
    folder = _download()
    if folder:
        master = run_cleaning(resolve_input(folder)[1], folder, (1, 2, 3))
        if master:
            print(f"Final dataset: {master}")


def _dataset_csv(ws):
    csv_path = _ask("Dataset CSV", ws.master_csv)
    if not Path(csv_path).is_file():
        print(f"CSV not found: {csv_path}")
        return None
    return csv_path


def _ids(text):
    ids = {i for i in re.split(r"[\s,;]+", text or "") if i}
    return ids or None


def _download_images():
    ws = _ask_workspace()
    csv_path = _dataset_csv(ws)
    if csv_path:
        only = _ids(_ask("gbifIDs to download (blank for all missing)"))
        download_media(csv_path, _ask("Image folder", ws.media_dir), only_ids=only, force=bool(only))


def _measure():
    ws = _ask_workspace()
    only = _ids(_ask("gbifIDs to measure (blank for all new images)"))
    measure_images(_ask("Image folder", ws.media_dir), _ask("Measurements CSV", ws.measurements_csv),
                   only_ids=only, force=bool(only), settings=_model_settings([]), options=_detector_options([]))


def _join():
    ws = _ask_workspace()
    csv_path = _dataset_csv(ws)
    if csv_path:
        fill = _ask("Fill empty dataset fields from voucher labels? (y/n)", "n").lower() == "y"
        join_measurements(csv_path, _ask("Measurements CSV", ws.measurements_csv), fill_blanks=fill)


def interactive():
    actions = {
        "1": ("Download only", _download),
        "2": ("Clean existing data", _clean),
        "3": ("Full workflow", _full),
        "4": ("Download images", _download_images),
        "5": ("Measure images", _measure),
        "6": ("Join measurements into the dataset", _join),
    }
    print(APP_TITLE)
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


CLEANER_USAGE = """Usage:
  python main.py cleaner 1 <occurrence> <multimedia> [output] [--strict | --no-precision] [--download NAME]
  python main.py cleaner 2 [inspected_csv] [master] [duplicates] [--download NAME]
  python main.py cleaner 3 [--columns a,b,c] [--download NAME]   (all columns when --columns is omitted)
Each download lives in data/<download id>_<Species>/. --download picks the folder by name (the newest download when omitted)."""


def cleaner_command(args):
    positional = _positional(args, ("--columns", "--download"))
    download = _option(args, "--download")
    if positional[:1] == ["1"] and len(positional) >= 3:
        precision = (PRECISION_NONE if "--no-precision" in args
                     else PRECISION_STRICT if "--strict" in args else PRECISION_RELAXED)
        ws = _workspace(download) if download else resolve_input(Path(positional[1]).parent)[1]
        phase_1_clean_and_merge(positional[1], positional[2], ws,
                                output_csv=positional[3] if len(positional) > 3 else None,
                                coordinate_precision=precision)
    elif positional[:1] == ["2"]:
        phase_2_finalize_dataset(_workspace(download), *positional[1:4])
    elif positional[:1] == ["3"]:
        columns = _option(args, "--columns")
        phase_3_select_columns(_workspace(download),
                               [c.strip() for c in columns.split(",") if c.strip()] if columns else None)
    else:
        print(CLEANER_USAGE)
        sys.exit(1)


def _option(args, name):
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def _positional(args, value_options=()):
    result, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in value_options:
            skip = True
        elif not arg.startswith("--"):
            result.append(arg)
    return result


def _model_settings(args):
    """The provider and model saved in the app, overridden by --provider and --model."""
    from .measure.providers import settings_from_app

    return settings_from_app(load_settings(), _option(args, "--provider"), _option(args, "--model"))


def _detector_options(args):
    """The Detector options saved in the app, overridden by --detector, --no-sam and --second-opinion."""
    from .measure.detectors import DetectorOptions

    options = DetectorOptions.from_settings(load_settings())
    mode = _option(args, "--detector")
    if mode:
        if mode not in ("auto", "vision", "trained"):
            raise ValueError("--detector must be auto, vision or trained")
        options.mode = mode
    if "--no-sam" in args:
        options.refine_masks = False
    if "--second-opinion" in args:
        options.second_opinion = True
    if "--no-labels" in args:
        options.read_labels = False
    reader = _option(args, "--labels")
    if reader:
        if reader not in ("auto", "ocr", "vision", "off"):
            raise ValueError("--labels must be auto, ocr, vision or off")
        options.read_labels = reader != "off"
        options.label_reader = "auto" if reader == "off" else reader
    return options


MODEL_OPTIONS = ("--provider", "--model", "--detector", "--labels")


def measure_command(args):
    positional = _positional(args, ("--ids", "--download", *MODEL_OPTIONS))
    ws = _workspace(_option(args, "--download"))
    only = _ids(_option(args, "--ids"))
    measure_images(positional[0] if positional else ws.media_dir,
                   positional[1] if len(positional) > 1 else ws.measurements_csv,
                   only_ids=only, force=bool(only) or "--redo" in args, settings=_model_settings(args),
                   options=_detector_options(args))


def benchmark_command(args):
    from .measure.benchmark import BENCHMARK_DIR_NAME, run_benchmark

    positional = _positional(args, ("--measurements", "--ids", "--download", "--label", "--key", "--split",
                                    *MODEL_OPTIONS))
    if not positional:
        print("Usage: python main.py benchmark <hand_measurements_csv> [--measure | --measurements file.csv] "
              "[--ids 123,456] [--split test] [--label NAME] [--key gbifID] [--provider NAME] [--model ID] "
              "[--detector auto|vision|trained] [--download NAME]")
        sys.exit(1)
    ws = _workspace(_option(args, "--download"))
    only = _ids(_option(args, "--ids"))
    split = _option(args, "--split")
    if split:
        from .measure.training import latest_splits

        held_out = {gbif_id for gbif_id, name in latest_splits().items() if name == split}
        if not held_out:
            raise ValueError(f"No training set has a '{split}' split yet. Train a model first.")
        only = held_out if only is None else only & held_out
    run_benchmark(positional[0], ws.media_dir, ws.root / BENCHMARK_DIR_NAME,
                  measurements_csv=_option(args, "--measurements") or ws.measurements_csv,
                  measure="--measure" in args, only_ids=only,
                  label=_option(args, "--label") or "", reference_key=_option(args, "--key") or "gbifID",
                  settings=_model_settings(args), options=_detector_options(args))


def models_command(args):
    """python main.py models [status | install | download-sam | download-weights | install-ocr]"""
    from .measure import trained

    action = (args or ["status"])[0]
    if action == "install":
        trained.install()
    elif action == "download-sam":
        trained.download_sam()
    elif action == "download-weights":
        trained.download_base_weights()
    elif action == "install-ocr":
        from .measure import ocr
        ocr.install()
    elif action != "status":
        print("Usage: python main.py models [status | install | download-sam | download-weights | install-ocr]")
        sys.exit(1)
    from .measure import ocr as label_reader
    print("\n".join(trained.status() + ["Label reader (OCR): " + ("installed" if label_reader.available()
                                                                  else "not installed (python main.py models install-ocr)")]))
    if not trained.available():
        print("To install the add-on yourself, run these in the app's Python environment:")
        for command in trained.install_commands("python"):
            print("  " + " ".join(command))


def labels_command(args):
    """python main.py labels export [--prelabels] [--output file.json] | import file.json"""
    from .measure import labels

    positional = _positional(args, ("--output", "--download"))
    ws = _workspace(_option(args, "--download"))
    if positional[:1] == ["export"]:
        labels.export_coco(ws.measurements_csv, _option(args, "--output"), reviewed_only="--prelabels" not in args)
    elif positional[:1] == ["import"] and len(positional) > 1:
        labels.import_coco(positional[1], ws.measurements_csv, ws.media_dir)
    else:
        print("Usage:\n  python main.py labels export [--prelabels] [--output file.json] [--download NAME]\n"
              "  python main.py labels import corrected_coco.json [--download NAME]")
        sys.exit(1)


def train_command(args):
    """python main.py train [pose | seg | all] [--epochs N] [--imgsz N] [--batch N] [--base weights.pt]"""
    from .measure import training

    positional = _positional(args, ("--epochs", "--imgsz", "--batch", "--base"))
    tasks = {"pose": ["pose"], "seg": ["seg"], "all": ["pose", "seg"]}.get((positional or ["pose"])[0])
    if not tasks:
        print("Usage: python main.py train [pose | seg | all] [--epochs N] [--imgsz N] [--batch N] [--base weights.pt]")
        sys.exit(1)
    for task in tasks:
        training.train(task, base=_option(args, "--base"), epochs=_option(args, "--epochs"),
                       imgsz=_option(args, "--imgsz"), batch=_option(args, "--batch"))


def export_mof_command(args):
    """python main.py export-mof [measurements_csv] [--output file.csv] [--download NAME]"""
    from .measure.records import export_measurement_or_fact

    positional = _positional(args, ("--output", "--download"))
    csv_path = positional[0] if positional else _workspace(_option(args, "--download")).measurements_csv
    export_measurement_or_fact(csv_path, _option(args, "--output"))


def join_command(args):
    value_options = ("--key", "--measurement-key", "--columns", "--output", "--download")
    positional = _positional(args, value_options)
    ws = _workspace(_option(args, "--download"))
    columns = _option(args, "--columns")
    key = _option(args, "--key") or "gbifID"
    join_measurements(positional[0] if positional else ws.master_csv,
                      positional[1] if len(positional) > 1 else ws.measurements_csv,
                      fill_blanks="--fill-blanks" in args,
                      master_key=key,
                      measurement_key=_option(args, "--measurement-key") or key,
                      measurement_columns=[c.strip() for c in columns.split(",") if c.strip()] if columns else None,
                      output_csv=_option(args, "--output"),
                      matched_only="--matched-only" in args)
