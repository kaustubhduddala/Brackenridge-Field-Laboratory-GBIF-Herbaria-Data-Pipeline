import sys

from config import APP_VERSION, DATA_DIR, MEDIA_DIR

USAGE = """Usage:
  python main.py                 open the app
  python main.py --cli           interactive command-line menu
  python main.py --version       print the version
  python main.py cleaner ...     run phase 1 or 2 directly (run without arguments for details)
  python main.py measure [image_folder] [measurements_csv] [--ids 123,456] [--redo]
  python main.py join [dataset_csv] [measurements_csv] [--fill-blanks] [--matched-only]
                      [--key gbifID] [--measurement-key gbifID] [--columns a,b,c] [--output file.csv]"""


def run_gui():
    try:
        import tkinter
        root = tkinter.Tk()
        root.destroy()
    except Exception:
        print("GUI unavailable; starting the command-line menu.")
        return run_cli()
    import program.gui as gui
    gui.launch()


def self_test():
    try:
        import program.analysis as analysis, program.cli as cli, program.gbif as gbif, program.geo as geo, program.media as media, program.power as power, program.processor as processor, program.theme as theme, program.utils as utils  # noqa: F401
        import bs4, numpy, openai, pandas, PIL.Image, pygbif, requests  # noqa: F401
        import tkinter  # noqa: F401
        import program.gui as gui  # noqa: F401
    except Exception as exc:
        print(f"Self-test failed: {exc!r}", file=sys.stderr)
        sys.exit(1)
    print(f"Self-test passed ({APP_VERSION})")


def run_cli():
    import program.cli as cli
    cli.interactive()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

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
        import program.cli as cli
        cli.cleaner_command(args[1:])
    elif command == "measure":
        import program.cli as cli
        cli.measure_command(args[1:])
    elif command == "join":
        import program.cli as cli
        cli.join_command(args[1:])
    else:
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()
