"""The CSV file shared by the Modify and Analysis tabs, and the file picker used by both."""
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from config import DATA_DIR
from .modify import load_table
from .widgets import CSV_TYPES

MAX_RECENT = 12
MAX_CACHED = 3


class CsvSource:
    """Holds the selected path and loads each file once. Cached tables must not be modified."""

    def __init__(self, app):
        self.app = app
        self.path_var = tk.StringVar(value="")
        self.listeners = []
        self.cache = {}
        self.recent = [p for p in app.settings.get("recent_csvs", []) if Path(p).is_file()]

    def path(self):
        return self.path_var.get().strip()

    def choices(self):
        ws = self.app.workspace
        paths = [str(p) for p in ws.csv_files()] if ws else []
        dataset = self.app.dataset_var.get().strip()
        paths += [dataset, *self.recent]
        return list(dict.fromkeys(p for p in paths if p and Path(p).is_file()))

    def table(self, path=None):
        path = Path(path or self.path())
        if not path.is_file():
            raise FileNotFoundError(f"{path} does not exist")
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        hit = self.cache.get(str(path))
        if hit and hit[0] == stamp:
            return hit[1]
        df = load_table(path, self.app._log)
        while len(self.cache) >= MAX_CACHED:
            self.cache.pop(next(iter(self.cache)))
        self.cache[str(path)] = (stamp, df)
        return df

    def forget(self, path):
        self.cache.pop(str(path), None)

    def select(self, path):
        path = str(path).strip()
        self.path_var.set(path)
        if path and Path(path).is_file():
            self.recent = [path] + [p for p in self.recent if p != path]
            del self.recent[MAX_RECENT:]
        for listener in self.listeners:
            listener()

    def reload(self):
        self.forget(self.path())
        self.select(self.path())


class CsvPicker(ttk.Frame):
    """A path box with the files of the active download in its drop-down, Browse and Open."""

    def __init__(self, parent, app, variable, choices, on_select, extras=()):
        super().__init__(parent)
        self.app, self.variable, self.choices, self.on_select = app, variable, choices, on_select
        self.columnconfigure(1, weight=1)
        self.combo = ttk.Combobox(self, textvariable=variable, postcommand=self.refresh_choices)
        self.combo.grid(row=0, column=1, sticky="ew", pady=3)
        self.combo.bind("<<ComboboxSelected>>", lambda _: self._chosen())
        self.combo.bind("<Return>", lambda _: self._chosen())
        buttons = [("Browse…", self._browse), ("Open", lambda: app._open(variable.get().strip())), *extras]
        for column, (text, command) in enumerate(buttons, start=2):
            ttk.Button(self, text=text, command=command).grid(row=0, column=column, padx=(6, 0), pady=3)
        self.info = ttk.Label(self, style="Muted.TLabel", wraplength=760, justify="left")
        self.info.grid(row=1, column=1, columnspan=len(buttons) + 1, sticky="w")
        self.refresh_choices()

    def label(self, text):
        ttk.Label(self, text=text).grid(row=0, column=0, sticky="w", padx=(0, 10))
        return self

    def refresh_choices(self):
        self.combo.configure(values=self.choices())

    def set_info(self, text):
        self.info.configure(text=text)

    def _chosen(self):
        self.on_select(self.variable.get().strip())

    def _browse(self):
        current = Path(self.variable.get().strip() or ".")
        ws = self.app.workspace
        start = current.parent if current.parent.is_dir() and self.variable.get().strip() else (
            ws.root if ws else DATA_DIR)
        path = filedialog.askopenfilename(parent=self.winfo_toplevel(), title="Select a CSV file",
                                          initialdir=str(start), filetypes=CSV_TYPES)
        if path:
            self.on_select(path)
