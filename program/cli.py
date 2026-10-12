import re
import sys
from pathlib import Path

from .analysis import join_measurements, measure_images
from config import PRECISION_NONE, PRECISION_RELAXED, PRECISION_STRICT
from .gbif import download_dataset
from .media import download_media
from .processor import phase_1_clean_and_merge, phase_2_finalize_dataset, phase_3_select_columns, run_cleaning
from .workspace import list_workspaces, resolve_input, workspace_by_name, workspace_of


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


def _confirm():
    return input("Review the CSV, then press Enter to run phase 2, or type n to stop: ").strip().lower() != "n"


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
    master = run_cleaning(ws, folder, phases, _confirm)
    if master:
        print(f"Final dataset: {master}")


def _full():
    folder = _download()
    if folder:
        master = run_cleaning(resolve_input(folder)[1], folder, (1, 2, 3), _confirm)
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
                   only_ids=only, force=bool(only))


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
    print("GBIF Herbaria Data Pipeline")
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


def cleaner_command(args):
    prog = Path(sys.argv[0]).name
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
        print("Usage:")
        print(f"  python {prog} cleaner 1 <occurrence> <multimedia> [output] [--strict | --no-precision] [--download NAME]")
        print(f"  python {prog} cleaner 2 [inspected_csv] [master] [duplicates] [--download NAME]")
        print(f"  python {prog} cleaner 3 [--columns a,b,c] [--download NAME]   (all columns when --columns is omitted)")
        print("Each download lives in data/<download id>_<Species>/. --download picks the folder by name "
              "(the newest download when omitted).")
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


def measure_command(args):
    positional = _positional(args, ("--ids", "--download"))
    ws = _workspace(_option(args, "--download"))
    only = _ids(_option(args, "--ids"))
    measure_images(positional[0] if positional else ws.media_dir,
                   positional[1] if len(positional) > 1 else ws.measurements_csv,
                   only_ids=only, force=bool(only) or "--redo" in args)


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
