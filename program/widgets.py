"""Widgets shared by the dialogs and the Modify and Analysis tabs."""
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

import pandas as pd

from config import DATA_DIR
from .stats import numbers
from .utils import save_csv

CSV_TYPES = [("CSV files", "*.csv"), ("All files", "*.*")]
CHECK_MARK = [(4, 8), (5, 9), (6, 10), (7, 11), (8, 10), (9, 9), (10, 8), (11, 7), (12, 6)]
SELF_SCROLLING = ("Treeview", "Listbox", "Text", "Canvas")


def _set_entry(entry, value):
    entry.delete(0, "end")
    entry.insert(0, str(value))


def _set_text(widget, value):
    widget.delete("1.0", "end")
    widget.insert("1.0", value)


def _check_image(master, p, checked):
    image = tk.PhotoImage(master=master, width=16, height=16)
    image.put(p["accent"] if checked else p["muted"], to=(1, 1, 15, 15))
    image.put(p["accent"] if checked else p["surface"], to=(2, 2, 14, 14))
    if checked:
        for x, y in CHECK_MARK:
            image.put(p["on_accent"], to=(x, y - 1, x + 1, y + 1))
    return image


def wheel_steps(event):
    return -event.delta if sys.platform == "darwin" else -event.delta // 120


class ScrollFrame(ttk.Frame):
    def __init__(self, master, palette):
        super().__init__(master)
        self.canvas = tk.Canvas(self, background=palette["bg"], highlightthickness=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.columnconfigure(0, weight=1)
        self.inner.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.winfo_toplevel().bind("<MouseWheel>", self._wheel, add="+")

    def _wheel(self, event):
        try:
            under = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        if under is None or under.winfo_class() in SELF_SCROLLING and under is not self.canvas:
            return
        widget = under
        while widget is not None and widget is not self:
            widget = widget.master
        if widget is None:
            return
        step = wheel_steps(event)
        if step and self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(step, "units")


class ColumnChecklist(ttk.LabelFrame):
    def __init__(self, parent, app, title, columns, checked, key_default, height=10):
        super().__init__(parent, text=title)
        self.columns = list(columns)
        self.checked = set(checked)
        self.box_on = _check_image(self, app.palette, True)
        self.box_off = _check_image(self, app.palette, False)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(2, weight=1)

        ttk.Label(self, text="Match on").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.key_var = tk.StringVar(value=key_default if key_default in self.columns else self.columns[0])
        self.key_box = ttk.Combobox(self, textvariable=self.key_var, values=self.columns, state="readonly")
        self.key_box.grid(row=0, column=1, sticky="ew")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refresh())
        search = ttk.Entry(self, textvariable=self.search_var)
        search.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 6))
        ttk.Label(self, text="Search", style="Muted.TLabel").grid(row=1, column=2, sticky="w", padx=(6, 0))

        frame = ttk.Frame(self)
        frame.grid(row=2, column=0, columnspan=3, sticky="nsew")
        self.tree = ttk.Treeview(frame, show="tree", selectmode="extended", height=height)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda _: self._toggle(self.tree.selection()) or "break")

        buttons = ttk.Frame(self)
        buttons.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Button(buttons, text="Check shown", command=lambda: self._set_shown(True)).pack(side="left")
        ttk.Button(buttons, text="Uncheck shown", command=lambda: self._set_shown(False)).pack(side="left", padx=6)
        self.count_label = ttk.Label(buttons, style="Muted.TLabel")
        self.count_label.pack(side="right")
        self._refresh()

    def selected(self):
        return [c for c in self.columns if c in self.checked]

    def _refresh(self):
        term = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        for i, column in enumerate(self.columns):
            if not term or term in column.lower():
                self.tree.insert("", "end", iid=str(i), text=column,
                                 image=self.box_on if column in self.checked else self.box_off)
        self._update_count()

    def _update_count(self):
        self.count_label.configure(text=f"{len(self.checked)} of {len(self.columns)} checked")

    def _toggle(self, iids):
        names = [self.columns[int(i)] for i in iids]
        target = any(n not in self.checked for n in names)
        for iid, name in zip(iids, names):
            (self.checked.add if target else self.checked.discard)(name)
            self.tree.item(iid, image=self.box_on if target else self.box_off)
        self._update_count()

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) == "tree":
            iid = self.tree.identify_row(event.y)
            if iid:
                self._toggle([iid])
                return "break"

    def _set_shown(self, checked):
        for iid in self.tree.get_children():
            name = self.columns[int(iid)]
            (self.checked.add if checked else self.checked.discard)(name)
            self.tree.item(iid, image=self.box_on if checked else self.box_off)
        self._update_count()


class DataTable(ttk.Frame):
    """A read-only grid for a DataFrame: sortable headers, a text filter and Save as CSV."""

    def __init__(self, parent, height=10, max_rows=1000, searchable=True, saveable=True, name="table"):
        super().__init__(parent)
        self.max_rows, self.df, self.shown = max_rows, pd.DataFrame(), pd.DataFrame()
        self.sort_column, self.sort_reverse, self.name = None, False, name
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        top = ttk.Frame(self)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self.query = tk.StringVar()
        if searchable:
            ttk.Label(top, text="Filter").pack(side="left")
            self.query.trace_add("write", lambda *_: self._refresh())
            ttk.Entry(top, textvariable=self.query, width=28).pack(side="left", padx=6)
        self.note = ttk.Label(top, style="Muted.TLabel")
        self.note.pack(side="left", padx=6)
        if saveable:
            ttk.Button(top, text="Save as CSV…", command=self.save).pack(side="right")
        self.tree = ttk.Treeview(self, show="headings", height=height, selectmode="extended")
        y = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        x = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        y.grid(row=1, column=1, sticky="ns")
        x.grid(row=2, column=0, sticky="ew")

    def show(self, df, note=""):
        self.df = df.reset_index(drop=True) if df is not None else pd.DataFrame()
        self.extra_note = note
        self.sort_column, self.sort_reverse = None, False
        columns = list(self.df.columns)
        self.tree.configure(columns=columns)
        for column in columns:
            self.tree.heading(column, text=column, anchor="w", command=lambda c=column: self._sort(c))
            sample = self.df[column].head(200).fillna("").astype(str).str.len()
            width = int(min(max(len(str(column)) * 8, (sample.max() if len(sample) else 0) * 7) + 24, 300))
            self.tree.column(column, width=max(width, 70), anchor="w", stretch=False)
        self._refresh()

    def _refresh(self):
        df = self.df
        term = self.query.get().strip().lower()
        if term and len(df):
            text = df.fillna("").astype(str).apply(lambda s: s.str.lower().str.contains(term, regex=False))
            df = df[text.any(axis=1)]
        if self.sort_column in df.columns:
            key = numbers(df[self.sort_column])
            numeric = key.notna().sum() >= 0.9 * df[self.sort_column].fillna("").astype(str).str.strip().ne("").sum()
            order = (key if numeric and key.notna().any() else df[self.sort_column].fillna("").astype(str).str.lower())
            df = df.assign(__s__=order).sort_values("__s__", ascending=not self.sort_reverse, na_position="last",
                                                   kind="stable").drop(columns="__s__")
        self.shown = df
        self.tree.delete(*self.tree.get_children())
        for row in df.head(self.max_rows).fillna("").astype(str).itertuples(index=False, name=None):
            self.tree.insert("", "end", values=row)
        note = f"{len(df):,} rows" + (f" (first {self.max_rows:,} shown)" if len(df) > self.max_rows else "")
        self.note.configure(text=" · ".join(p for p in (note, getattr(self, "extra_note", "")) if p))

    def _sort(self, column):
        self.sort_reverse = not self.sort_reverse if self.sort_column == column else False
        self.sort_column = column
        self._refresh()

    def save(self):
        if self.df.empty:
            self.bell()
            return
        path = filedialog.asksaveasfilename(parent=self.winfo_toplevel(), title="Save as CSV",
                                            initialfile=f"{self.name}.csv", defaultextension=".csv",
                                            filetypes=CSV_TYPES)
        if path:
            save_csv(self.shown if len(self.shown) != len(self.df) else self.df, path)


class BarChart(tk.Canvas):
    """Horizontal bars with a label and value for each row. Scrolls when there are many bars."""
    ROW, LABEL_W, VALUE_W = 22, 190, 70

    def __init__(self, parent, palette, height=260):
        super().__init__(parent, height=height, highlightthickness=1)
        self.items, self.title = [], ""
        self.bind("<Configure>", lambda _: self._draw())
        self.bind("<MouseWheel>", lambda e: self.yview_scroll(wheel_steps(e), "units"))
        self.restyle(palette)

    def restyle(self, palette):
        self.palette = palette
        self.configure(background=palette["surface"], highlightbackground=palette["border"])
        self._draw()

    def set(self, labels, values, title=""):
        self.items = list(zip(map(str, labels), map(float, values)))
        self.title = title
        self.yview_moveto(0)
        self._draw()

    @staticmethod
    def _format(value):
        return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.4g}"

    def _draw(self):
        self.delete("all")
        p = self.palette
        width = max(self.winfo_width(), 300)
        top = 8
        if self.title:
            self.create_text(10, 12, text=self.title, anchor="w", fill=p["muted"])
            top = 30
        if not self.items:
            self.create_text(width / 2, 60, text="Nothing to show", fill=p["muted"])
            self.configure(scrollregion=(0, 0, width, 100))
            return
        biggest = max((abs(v) for _, v in self.items), default=1) or 1
        room = max(width - self.LABEL_W - self.VALUE_W - 24, 40)
        for i, (label, value) in enumerate(self.items):
            y = top + i * self.ROW
            text = label if len(label) <= 28 else label[:27] + "…"
            self.create_text(10, y + 10, text=text, anchor="w", fill=p["text"])
            length = max(room * abs(value) / biggest, 1 if value else 0)
            self.create_rectangle(self.LABEL_W, y + 3, self.LABEL_W + length, y + 17, fill=p["accent"], width=0)
            self.create_text(self.LABEL_W + length + 6, y + 10, text=self._format(value), anchor="w",
                             fill=p["muted"])
        self.configure(scrollregion=(0, 0, width, top + len(self.items) * self.ROW + 8))


def path_label(path, workspace=None):
    """The shortest clear text for a path: relative to the active download, else to data/, else in full."""
    path = str(path or "").strip()
    if not path:
        return ""
    for base in ([workspace.root] if workspace else []) + [DATA_DIR]:
        try:
            return str(Path(path).resolve().relative_to(base.resolve())) or path
        except (ValueError, OSError):
            continue
    return path


class PathCombo(ttk.Combobox):
    """A drop-down of files or folders. `variable` always holds the full path; the box shows a short label
    for paths inside the active download (the file name) and the full path for anything else."""

    def __init__(self, parent, app, variable, choices, on_select=None, **options):
        self.display = tk.StringVar()
        super().__init__(parent, textvariable=self.display, postcommand=self._fill, **options)
        self.app, self.variable, self.choices, self.on_select = app, variable, choices, on_select
        self.labels = {}
        variable.trace_add("write", lambda *_: self._sync())
        self.bind("<<ComboboxSelected>>", lambda _: self._commit())
        self.bind("<Return>", lambda _: self._commit())
        self.bind("<FocusOut>", lambda _: self._commit(notify=False))
        self._fill()
        self._sync()

    def _label(self, path):
        return path_label(path, self.app.workspace)

    def _fill(self):
        paths = list(dict.fromkeys([*self.choices(), self.variable.get().strip()]))
        self.labels = {}
        for path in filter(None, paths):
            self.labels.setdefault(self._label(path), path)
        self.configure(values=list(self.labels))

    def _sync(self):
        self.display.set(self._label(self.variable.get()))

    def _commit(self, notify=True):
        text = self.display.get().strip()
        if not self.labels:
            self._fill()
        path = self.labels.get(text)
        if path is None and text and self.app.workspace and not Path(text).is_absolute():
            candidate = self.app.workspace.root / text
            path = str(candidate) if candidate.exists() else None
        path = path if path is not None else text
        changed = path != self.variable.get().strip()
        if changed:
            self.variable.set(path)
        self._sync()
        if self.on_select and (notify or changed):
            self.on_select(path)
