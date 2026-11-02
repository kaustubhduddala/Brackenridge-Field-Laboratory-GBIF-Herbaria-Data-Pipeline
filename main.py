import sys

from config import APP_VERSION, DATA_DIR

USAGE = """Usage:
  python main.py                 open the app
  python main.py --cli           interactive command-line menu
  python main.py --version       print the version
  python main.py cleaner ...     run phase 1 or 2 directly (run without arguments for details)
  python main.py measure [image_folder] [measurements_csv] [--ids 123,456] [--redo] [--download NAME]
                      [--provider lmstudio|openai|gemini|anthropic|custom] [--model ID]
  python main.py join [dataset_csv] [measurements_csv] [--fill-blanks] [--matched-only]
                      [--key gbifID] [--measurement-key gbifID] [--columns a,b,c] [--output file.csv]
                      [--download NAME]
  python main.py benchmark hand_measurements.csv [--measure | --measurements file.csv] [--ids 123,456]
                      [--label NAME] [--key gbifID] [--provider NAME] [--model ID] [--download NAME]
                                 score AI measurements against hand measurements

Each download is kept in data/<download id>_<Species>/. --download NAME picks the folder (newest by default)."""


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
        from program import (accuracy_benchmark, command_line, csv_editing, csv_statistics,  # noqa: F401
                             dataset_cleaning, download_folders, gbif_download, gui_widgets, image_download,
                             keep_awake, measurements_csv, plant_geometry, scale_calibration, sheet_measurement,
                             utils, vision_locator, vision_providers)
        import bs4, keyring, numpy, openai, pandas, PIL.Image, pygbif, requests  # noqa: F401
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
    else:
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()
