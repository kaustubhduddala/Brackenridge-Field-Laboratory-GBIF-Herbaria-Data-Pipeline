import sys

from config import APP_VERSION, DATA_DIR

USAGE = """Usage:
  python main.py                 open the app
  python main.py --cli           interactive command-line menu
  python main.py --version       print the version
  python main.py cleaner ...     run cleaning phase 1, 2 or 3 directly (cleaner --help for details)
  python main.py measure [image_folder] [measurements_csv] [--ids 123,456] [--redo] [--download NAME]
                      [--provider lmstudio|openai|gemini|anthropic|custom] [--model ID]
                      [--detector auto|vision|trained] [--no-sam] [--second-opinion] [--labels auto|ocr|vision|off]
  python main.py join [dataset_csv] [measurements_csv] [--fill-blanks] [--matched-only]
                      [--key gbifID] [--measurement-key gbifID] [--columns a,b,c] [--output file.csv]
                      [--download NAME]
  python main.py benchmark hand_measurements.csv [--measure | --measurements file.csv] [--ids 123,456]
                      [--split test] [--label NAME] [--key gbifID] [--provider NAME] [--model ID]
                      [--detector auto|vision|trained] [--download NAME]
                                 score measurements and flags against hand measurements
  python main.py export-mof [measurements_csv] [--output file.csv]
                                 write the measurements as Darwin Core MeasurementOrFact records
  python main.py models [status | install | download-sam | download-weights | install-ocr]
                                 the optional models: PyTorch add-on, SAM 2, built-in label reader (also: File > Model setup)
  python main.py labels export [--prelabels] [--output file.json] | import corrected_coco.json
                                 exchange labels with CVAT or Label Studio (COCO format)
  python main.py train [pose | seg | all] [--epochs N] [--imgsz N] [--batch N] [--base weights.pt]
                                 fine-tune the models on the reviewed sheets

Each download is kept in data/<download id>_<Species>/. --download NAME picks the folder (newest by default).
Add --help to any command to see its usage without running it."""

SUBCOMMANDS = ("cleaner", "measure", "join", "benchmark", "export-mof", "models", "labels", "train")
HELP_FLAGS = ("-h", "--help")


def command_usage(command):
    """The usage text for one subcommand: its lines from USAGE (the cleaner keeps its own)."""
    if command == "cleaner":
        from program.command_line import CLEANER_USAGE
        return CLEANER_USAGE
    lines, inside = [], False
    for line in USAGE.splitlines()[1:]:
        if line.startswith("  python main.py "):
            inside = line.split()[2] == command
        elif not line.startswith("    "):
            inside = False
        if inside:
            lines.append(line)
    return "Usage:\n" + "\n".join(lines)


def run_gui():
    try:
        import tkinter
        root = tkinter.Tk()
        root.destroy()
    except Exception:
        print("GUI unavailable; starting the command-line menu.")
        return run_cli()
    from program import app_window
    app_window.launch()


def self_test():
    try:
        from program import (command_line, csv_editing, csv_statistics, dataset_cleaning,  # noqa: F401
                             download_folders, gbif_download, gui_widgets, image_download, keep_awake, utils)
        from program.measure import (benchmark, calibration, decision, detectors, downloads,  # noqa: F401
                                     geometry, labels, locator, model_setup, occlusion, ocr, overlay, pipeline,
                                     providers, records, taxa, trained, training)
        import bs4, cv2, keyring, numpy, openai, pandas, PIL.Image, pygbif, requests, skimage, tksheet  # noqa: F401
        import tkinter  # noqa: F401
        from program import browser_sign_in
        if not browser_sign_in.available():
            raise ImportError('playwright is not installed or was not bundled')
        if not browser_sign_in.driver_ready():
            raise RuntimeError("Playwright's browser driver cannot run")
        from program import app_window  # noqa: F401
    except Exception as exc:
        print(f"Self-test failed: {exc!r}", file=sys.stderr)
        sys.exit(1)
    print(f"Self-test passed ({APP_VERSION})")


def run_cli():
    from program import command_line
    command_line.interactive()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    command = args[0] if args else "--gui"
    if command in ("-h", "--help"):
        print(USAGE)
    elif command == "--version":
        print(APP_VERSION)
    elif command == "--self-test":
        self_test()
    elif command == "--gui":
        run_gui()
    elif command == "--cli":
        run_cli()
    elif command in SUBCOMMANDS and any(arg in HELP_FLAGS for arg in args[1:]):
        # Checked before dispatch: the commands take free-form arguments, so a help flag would otherwise be
        # ignored (or read as a path) and the command would run, rewriting files.
        print(command_usage(command))
    elif command == "cleaner":
        from program import command_line
        command_line.cleaner_command(args[1:])
    elif command == "measure":
        from program import command_line
        command_line.measure_command(args[1:])
    elif command == "join":
        from program import command_line
        command_line.join_command(args[1:])
    elif command == "benchmark":
        from program import command_line
        command_line.benchmark_command(args[1:])
    elif command in ("models", "labels", "train", "export-mof"):
        from program import command_line
        getattr(command_line, command.replace("-", "_") + "_command")(args[1:])
    else:
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()
