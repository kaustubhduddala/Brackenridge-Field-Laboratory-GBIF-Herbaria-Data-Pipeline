import re
import sys
from pathlib import Path

from .analysis import join_measurements, measure_images
from config import MASTER_CSV, MEASUREMENTS_CSV, MEDIA_DIR, PRECISION_NONE, PRECISION_RELAXED, PRECISION_STRICT
from .gbif import download_dataset
from .media import download_media
from .processor import phase_1_clean_and_merge, phase_2_finalize_dataset, run_cleaning
from .utils import prepare_folder


def _ask(prompt, default=""):
    value = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return value or str(default)


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
    steps = {"1": "both", "2": "phase1", "3": "phase2"}.get(
        _ask("Steps (1 = phase 1 and 2, 2 = phase 1 only, 3 = phase 2 only)", "1"))
    if not steps:
        print("Invalid selection.")
        return
    folder = prepare_folder(_ask("Data folder or ZIP path")) if steps != "phase2" else None
    master = run_cleaning(folder, steps, _confirm)
    if master:
        print(f"Final dataset: {master}")


def _full():
    folder = _download()
    if folder:
        master = run_cleaning(folder, "both", _confirm)
        if master:
            print(f"Final dataset: {master}")


def _dataset_csv():
    csv_path = _ask("Dataset CSV", MASTER_CSV)
    if not Path(csv_path).is_file():
        print(f"CSV not found: {csv_path}")
        return None
    return csv_path


def _ids(text):
    ids = {i for i in re.split(r"[\s,;]+", text or "") if i}
    return ids or None


def _download_images():
    csv_path = _dataset_csv()
    if csv_path:
        only = _ids(_ask("gbifIDs to download (blank for all missing)"))
        download_media(csv_path, _ask("Image folder", MEDIA_DIR), only_ids=only, force=bool(only))


def _measure():
    only = _ids(_ask("gbifIDs to measure (blank for all new images)"))
    measure_images(_ask("Image folder", MEDIA_DIR), _ask("Measurements CSV", MEASUREMENTS_CSV),
                   only_ids=only, force=bool(only))


def _join():
    csv_path = _dataset_csv()
    if csv_path:
        fill = _ask("Fill empty dataset fields from voucher labels? (y/n)", "n").lower() == "y"
        join_measurements(csv_path, _ask("Measurements CSV", MEASUREMENTS_CSV), fill_blanks=fill)


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
    positional = [a for a in args if not a.startswith("--")]
    if positional[:1] == ["1"] and len(positional) >= 3:
        precision = (PRECISION_NONE if "--no-precision" in args
                     else PRECISION_STRICT if "--strict" in args else PRECISION_RELAXED)
        phase_1_clean_and_merge(positional[1], positional[2],
                                output_csv=positional[3] if len(positional) > 3 else None,
                                coordinate_precision=precision)
    elif positional[:1] == ["2"] and len(positional) >= 2:
        phase_2_finalize_dataset(*positional[1:4])
    else:
        print("Usage:")
        print(f"  python {prog} cleaner 1 <occurrence> <multimedia> [output] [--strict | --no-precision]")
        print(f"  python {prog} cleaner 2 <inspected_csv> [master] [duplicates]")
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
    positional = _positional(args, ("--ids",))
    only = _ids(_option(args, "--ids"))
    measure_images(positional[0] if positional else MEDIA_DIR,
                   positional[1] if len(positional) > 1 else MEASUREMENTS_CSV,
                   only_ids=only, force=bool(only) or "--redo" in args)


def join_command(args):
    value_options = ("--key", "--measurement-key", "--columns", "--output")
    positional = _positional(args, value_options)
    columns = _option(args, "--columns")
    key = _option(args, "--key") or "gbifID"
    join_measurements(positional[0] if positional else MASTER_CSV,
                      positional[1] if len(positional) > 1 else MEASUREMENTS_CSV,
                      fill_blanks="--fill-blanks" in args,
                      master_key=key,
                      measurement_key=_option(args, "--measurement-key") or key,
                      measurement_columns=[c.strip() for c in columns.split(",") if c.strip()] if columns else None,
                      output_csv=_option(args, "--output"),
                      matched_only="--matched-only" in args)
