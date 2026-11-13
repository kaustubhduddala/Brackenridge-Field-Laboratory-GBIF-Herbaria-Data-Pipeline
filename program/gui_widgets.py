"""Pieces of the window shared by the dialogs and tabs: widgets, the shared CSV file picker, and themes."""
import subprocess
import sys
import tkinter as tk
import weakref
from pathlib import Path
from tkinter import filedialog, ttk

import pandas as pd

import config
from .csv_editing import load_table
from .csv_statistics import numbers
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


# --------------------------------------------------------------------------- hints

# The colours in use, filled in by apply_theme. Widgets drawn by hand read them here, and `themed`
# widgets are restyled whenever the theme changes.
ACTIVE = {}
_themed = weakref.WeakKeyDictionary()


def themed(widget, styler):
    """Style a hand-coloured widget (a Canvas, Listbox...) now and again on every theme change."""
    _themed[widget] = styler
    if ACTIVE:
        styler(widget, ACTIVE)
    return widget


def _restyle_themed(palette):
    for widget, styler in list(_themed.items()):
        try:
            styler(widget, palette)
        except tk.TclError:                              # the widget is gone
            _themed.pop(widget, None)


class Tooltip:
    """Help text that pops up beside a widget while the pointer rests on it.

    `text` is a string, or a function returning one for hints that change. `click` makes a press
    show the hint at once (the "?" badges) instead of dismissing it.
    """
    shown = None

    def __init__(self, widget, text, delay=500, wrap=340, click=False):
        self.widget, self.text, self.delay, self.wrap = widget, text, delay, wrap
        self.window, self.job = None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<Destroy>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.toggle if click else self.hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self.job = self.widget.after(self.delay, self.show)

    def _cancel(self):
        if self.job:
            try:
                self.widget.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None

    def toggle(self, _event=None):
        self.hide() if self.window else self.show()

    def show(self):
        self._cancel()
        try:
            text = self.text() if callable(self.text) else self.text
            if not text or self.window or not self.widget.winfo_viewable():
                return
            if Tooltip.shown:
                Tooltip.shown.hide()
            p = ACTIVE or PALETTES["light"]
            tip = self.window = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            try:                                         # macOS: a help window that never takes the focus
                tip.tk.call("::tk::unsupported::MacWindowStyle", "style", tip._w, "help", "noActivates")
            except tk.TclError:
                pass
            tip.configure(background=p["border"])
            tk.Label(tip, text=text, justify="left", wraplength=self.wrap, background=p["surface"],
                     foreground=p["text"], padx=10, pady=7).pack(padx=1, pady=1)
            tip.update_idletasks()
            widget = self.widget
            width, height = tip.winfo_reqwidth(), tip.winfo_reqheight()
            x, y = widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height() + 6
            screen_w, screen_h = widget.winfo_screenwidth(), widget.winfo_screenheight()
            if 0 <= x < screen_w and 0 <= y < screen_h + height:     # on the main screen: keep it on screen
                x = max(8, min(x, screen_w - width - 8))
                if y + height > screen_h - 8:
                    y = widget.winfo_rooty() - height - 6
            tip.wm_geometry(f"+{x}+{y}")
            try:
                if widget.winfo_toplevel().attributes("-topmost"):
                    tip.attributes("-topmost", True)     # stay above a window that is itself kept on top
            except tk.TclError:
                pass
            tip.lift()
            Tooltip.shown = self
        except tk.TclError:
            self.window = None

    def hide(self, _event=None):
        self._cancel()
        window, self.window = self.window, None
        if Tooltip.shown is self:
            Tooltip.shown = None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass


def tip(widget, text, **options):
    """Give `widget` a hover hint; returns the widget so it can be laid out in the same line."""
    widget.tooltip = Tooltip(widget, text, **options)
    return widget


class HelpTip(ttk.Label):
    """A tiny grey "?" that explains the thing beside it when hovered or clicked.

    Used instead of lines of explanatory text, so the layout shows only the controls. `on` names the
    palette colour it sits on when that is not the window background.
    """

    def __init__(self, parent, text, wrap=380, on="bg"):
        super().__init__(parent, text="?", cursor="hand2", takefocus=0,
                         style="Help.TLabel" if on == "bg" else "HelpRaised.TLabel")
        self.tooltip = Tooltip(self, text, delay=120, wrap=wrap, click=True)
        self.bind("<Enter>", lambda _: self.state(["active"]), add="+")
        self.bind("<Leave>", lambda _: self.state(["!active"]), add="+")

    def set_text(self, text):
        self.tooltip.text = text


def help_label(parent, text, help_text, style="TLabel"):
    """A label that shows `help_text` when the pointer rests on it, marked by a tiny "?" after the text."""
    frame = ttk.Frame(parent)
    label = ttk.Label(frame, text=text, style=style)
    label.pack(side="left")
    if help_text:
        label.tooltip = Tooltip(label, help_text, delay=350, wrap=380)
        frame.help = HelpTip(frame, help_text)
        frame.help.pack(side="left", padx=(3, 0), anchor="n")
    return frame


def titled_frame(parent, title, help_text=None, **options):
    """A LabelFrame whose title can carry a "?" badge."""
    frame = ttk.LabelFrame(parent, text=title, **options)
    if help_text:
        frame.configure(labelwidget=help_label(frame, title, help_text, style="TLabelframe.Label"))
    return frame


def placeholder(entry, text, variable=None):
    """Grey prompt text inside an entry while it is empty. It is never part of the entry's value."""
    label = ttk.Label(entry, text=text, style="Placeholder.TLabel", cursor="xterm")

    def refresh(*_):
        try:
            if entry.get():
                label.place_forget()
            else:
                label.place(x=10, rely=0.5, anchor="w")    # leaves the text cursor visible beside it
        except tk.TclError:
            pass

    for event in ("<KeyRelease>", "<FocusIn>", "<FocusOut>", "<<Paste>>", "<<Cut>>"):
        entry.bind(event, lambda _: entry.after_idle(refresh), add="+")
    label.bind("<Button-1>", lambda _: entry.focus_set())
    if variable is not None:
        variable.trace_add("write", refresh)
    refresh()
    return entry


def search_entry(parent, variable, width=24, prompt="Search"):
    return placeholder(ttk.Entry(parent, textvariable=variable, width=width), prompt, variable)


def menu_button(parent, text, items, **options):
    """A button that drops down a menu of (label, command) pairs; None adds a separator."""
    button = ttk.Button(parent, text=text + "  ▾", **options)
    menu = tk.Menu(button, tearoff=False)
    for item in items:
        if item is None:
            menu.add_separator()
        else:
            menu.add_command(label=item[0], command=item[1])
    button.configure(command=lambda: menu.tk_popup(button.winfo_rootx(),
                                                   button.winfo_rooty() + button.winfo_height()))
    button.menu = menu
    return button


def style_listbox(widget, p):
    widget.configure(background=p["surface"], foreground=p["text"], selectbackground=p["select"],
                     selectforeground=p["text"], highlightthickness=1, highlightbackground=p["border"],
                     highlightcolor=p["accent"], relief="flat", borderwidth=0)


def listbox(parent, **options):
    """A tk.Listbox in the app's colours, restyled when the theme changes."""
    return themed(tk.Listbox(parent, exportselection=False, **options), style_listbox)


class ScrollFrame(ttk.Frame):
    """A frame that scrolls vertically when its content (`inner`) is taller than the space it has.
    The scrollbar is only shown while there is something to scroll."""

    def __init__(self, master, palette):
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, background=palette["bg"], highlightthickness=0)
        themed(self.canvas, lambda canvas, p: canvas.configure(background=p["bg"]))
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.bar.grid(row=0, column=1, sticky="ns")
        self.inner = ttk.Frame(self.canvas)
        window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.columnconfigure(0, weight=1)
        self.inner.bind("<Configure>", lambda _: self._resized())
        self.canvas.bind("<Configure>", lambda e: (self.canvas.itemconfigure(window, width=e.width),
                                                   self._resized()))
        self.winfo_toplevel().bind("<MouseWheel>", self._wheel, add="+")

    def _resized(self):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        needed = self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1
        if needed and not self.bar.winfo_ismapped():
            self.bar.grid()
        elif not needed and self.bar.winfo_ismapped():
            self.canvas.yview_moveto(0)
            self.bar.grid_remove()

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

        help_label(self, "Match on", "The column whose values pair the rows of the two files. Checked columns "
                   "below are the ones carried into the result.").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.key_var = tk.StringVar(value=key_default if key_default in self.columns else self.columns[0])
        self.key_box = ttk.Combobox(self, textvariable=self.key_var, values=self.columns, state="readonly")
        self.key_box.grid(row=0, column=1, sticky="ew")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refresh())
        search_entry(self, self.search_var, prompt="Search columns").grid(row=1, column=0, columnspan=3,
                                                                          sticky="ew", pady=(8, 6))

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
        tip(ttk.Button(buttons, text="Check all", command=lambda: self._set_shown(True)),
            "Check every column in the list (only the ones the search shows)").pack(side="left")
        tip(ttk.Button(buttons, text="Uncheck all", command=lambda: self._set_shown(False)),
            "Uncheck every column in the list (only the ones the search shows)").pack(side="left", padx=6)
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
            self.query.trace_add("write", lambda *_: self._refresh())
            search_entry(top, self.query, width=28, prompt="Filter rows").pack(side="left", padx=(0, 6))
        self.note = ttk.Label(top, style="Muted.TLabel")
        self.note.pack(side="left", padx=6)
        if saveable:
            tip(ttk.Button(top, text="Save as CSV…", command=self.save),
                "Save this table (the filtered rows, when a filter is on) as a CSV file").pack(side="right")
        self.tree = ttk.Treeview(self, show="headings", height=height, selectmode="extended")
        tip(self.tree, "Click a column heading to sort by it; click again to reverse.", delay=1500)
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
    for base in ([workspace.root] if workspace else []) + [config.DATA_DIR]:
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


# --------------------------------------------------------------------------- shared CSV file

# The CSV file shared by the Modify and Analysis tabs, and the file picker used by both.
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
        self.combo = PathCombo(self, app, variable, choices, on_select)
        self.combo.grid(row=0, column=1, sticky="ew", pady=3)
        buttons = [("Browse…", self._browse, "Pick any CSV file"),
                   ("Open", lambda: app._open(variable.get().strip()), "Open the file in its usual app"), *extras]
        for column, (text, command, *hint) in enumerate(buttons, start=2):
            button = ttk.Button(self, text=text, command=command)
            button.grid(row=0, column=column, padx=(6, 0), pady=3)
            if hint:
                tip(button, hint[0])
        self.info = ttk.Label(self, style="Muted.TLabel", wraplength=760, justify="left")
        self.info.grid(row=1, column=1, columnspan=len(buttons) + 1, sticky="w")
        self.refresh_choices()

    def label(self, text):
        ttk.Label(self, text=text).grid(row=0, column=0, sticky="w", padx=(0, 10))
        return self

    def refresh_choices(self):
        self.combo._fill()

    def set_info(self, text):
        self.info.configure(text=text)

    def _browse(self):
        current = Path(self.variable.get().strip() or ".")
        ws = self.app.workspace
        start = current.parent if current.parent.is_dir() and self.variable.get().strip() else (
            ws.root if ws else config.DATA_DIR)
        path = filedialog.askopenfilename(parent=self.winfo_toplevel(), title="Select a CSV file",
                                          initialdir=str(start), filetypes=CSV_TYPES)
        if path:
            self.on_select(path)


# --------------------------------------------------------------------------- light and dark themes

PALETTES = {
    "light": {
        "bg": "#f3f5f2", "surface": "#ffffff", "raised": "#e6ebe5", "hover": "#dce4dc", "border": "#cad4cb",
        "text": "#1b2420", "muted": "#5c6962", "accent": "#2d6a4f", "accent_hover": "#3b8061",
        "on_accent": "#ffffff", "disabled": "#b3beb6", "select": "#cfe5d8",
        "busy": "#1f5f8b", "ok": "#2d6a4f", "error": "#b42318",
    },
    "dark": {
        "bg": "#141a17", "surface": "#1c2420", "raised": "#222b26", "hover": "#2b3630", "border": "#34423a",
        "text": "#e2eae5", "muted": "#94a59b", "accent": "#5fb98c", "accent_hover": "#78c9a0",
        "on_accent": "#0e1712", "disabled": "#3a4740", "select": "#2e4d3d",
        "busy": "#86b8e3", "ok": "#6fcf9b", "error": "#f2786d",
    },
}
THEME_CHOICES = ("System", "Light", "Dark")


def system_theme():
    try:
        if sys.platform.startswith("win"):
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return "light" if winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else "dark"
        if sys.platform == "darwin":
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                 capture_output=True, text=True, timeout=2)
            return "dark" if "dark" in out.stdout.lower() else "light"
    except Exception:
        pass
    return "light"


def resolve_theme(choice):
    choice = (choice or "System").lower()
    return system_theme() if choice == "system" else choice


def set_title_bar(window, dark):
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def style_text(widget, p):
    widget.configure(background=p["surface"], foreground=p["text"], insertbackground=p["text"],
                     selectbackground=p["select"], selectforeground=p["text"], relief="flat", borderwidth=0,
                     highlightthickness=1, highlightbackground=p["border"], highlightcolor=p["accent"])


def apply_theme(root, name, fonts):
    p = PALETTES[name]
    ACTIVE.clear()
    ACTIVE.update(p)
    root.configure(background=p["bg"])
    for option, value in (("background", p["surface"]), ("foreground", p["text"]),
                          ("selectBackground", p["select"]), ("selectForeground", p["text"])):
        root.option_add(f"*TCombobox*Listbox.{option}", value)

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=p["bg"], foreground=p["text"], bordercolor=p["border"],
                    lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["raised"], focuscolor=p["accent"],
                    selectbackground=p["select"], selectforeground=p["text"], insertcolor=p["text"],
                    fieldbackground=p["surface"])
    style.map(".", foreground=[("disabled", p["muted"])])

    style.configure("TButton", background=p["raised"], foreground=p["text"], bordercolor=p["border"],
                    lightcolor=p["raised"], darkcolor=p["raised"], padding=(10, 4))
    style.map("TButton", background=[("disabled", p["bg"]), ("pressed", p["hover"]), ("active", p["hover"])],
              lightcolor=[("active", p["hover"])], darkcolor=[("active", p["hover"])])
    style.configure("Accent.TButton", background=p["accent"], foreground=p["on_accent"], bordercolor=p["accent"],
                    lightcolor=p["accent"], darkcolor=p["accent"], font=fonts["bold"], padding=(14, 5))
    style.map("Accent.TButton",
              background=[("disabled", p["disabled"]), ("pressed", p["accent_hover"]), ("active", p["accent_hover"])],
              lightcolor=[("disabled", p["disabled"]), ("active", p["accent_hover"])],
              darkcolor=[("disabled", p["disabled"]), ("active", p["accent_hover"])],
              bordercolor=[("disabled", p["disabled"])],
              foreground=[("disabled", p["muted"])])

    style.configure("TEntry", fieldbackground=p["surface"], foreground=p["text"], bordercolor=p["border"],
                    lightcolor=p["surface"], darkcolor=p["surface"], insertcolor=p["text"], padding=4)
    style.map("TEntry", bordercolor=[("focus", p["accent"])], lightcolor=[("focus", p["accent"])])
    style.configure("TCombobox", fieldbackground=p["surface"], background=p["raised"], foreground=p["text"],
                    arrowcolor=p["text"], bordercolor=p["border"], lightcolor=p["surface"],
                    darkcolor=p["surface"], padding=3)
    style.map("TCombobox", fieldbackground=[("readonly", p["surface"])], foreground=[("readonly", p["text"])],
              selectbackground=[("readonly", p["surface"])], selectforeground=[("readonly", p["text"])],
              background=[("active", p["hover"])], bordercolor=[("focus", p["accent"])])

    for widget in ("TRadiobutton", "TCheckbutton"):
        style.configure(widget, background=p["bg"], foreground=p["text"], indicatorbackground=p["surface"],
                        indicatorforeground=p["accent"], upperbordercolor=p["border"],
                        lowerbordercolor=p["border"], padding=(0, 2))
        style.map(widget, background=[("active", p["bg"])],
                  indicatorbackground=[("pressed", p["hover"]), ("selected", p["surface"])])

    style.configure("TNotebook", background=p["bg"], bordercolor=p["border"], tabmargins=(0, 4, 0, 0))
    style.configure("TNotebook.Tab", background=p["raised"], foreground=p["muted"], bordercolor=p["border"],
                    lightcolor=p["raised"], padding=(18, 7))
    style.map("TNotebook.Tab", background=[("selected", p["bg"]), ("active", p["hover"])],
              foreground=[("selected", p["accent"]), ("active", p["text"])],
              lightcolor=[("selected", p["bg"])])

    style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"], lightcolor=p["bg"],
                    darkcolor=p["bg"], padding=12)
    style.configure("TLabelframe.Label", background=p["bg"], foreground=p["accent"], font=fonts["bold"])
    style.configure("Horizontal.TProgressbar", background=p["accent"], troughcolor=p["raised"],
                    bordercolor=p["border"], lightcolor=p["accent"], darkcolor=p["accent"])
    style.configure("TScrollbar", background=p["raised"], troughcolor=p["bg"], bordercolor=p["bg"],
                    arrowcolor=p["muted"], lightcolor=p["raised"], darkcolor=p["raised"])
    style.map("TScrollbar", background=[("active", p["hover"])])
    style.configure("TSpinbox", fieldbackground=p["surface"], foreground=p["text"], background=p["raised"],
                    arrowcolor=p["text"], bordercolor=p["border"], lightcolor=p["surface"],
                    darkcolor=p["surface"], insertcolor=p["text"], padding=3)
    style.map("TSpinbox", bordercolor=[("focus", p["accent"])])
    style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"], foreground=p["text"],
                    bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"], rowheight=24)
    style.map("Treeview", background=[("selected", p["select"])], foreground=[("selected", p["text"])])
    style.configure("Treeview.Heading", background=p["raised"], foreground=p["text"], bordercolor=p["border"],
                    lightcolor=p["raised"], darkcolor=p["raised"], font=fonts["bold"], padding=(6, 4))
    style.map("Treeview.Heading", background=[("active", p["hover"])])
    style.configure("TPanedwindow", background=p["bg"])
    style.configure("Sash", sashthickness=8, gripcount=0, background=p["bg"])

    style.configure("Title.TLabel", font=fonts["title"], foreground=p["text"])
    style.configure("Muted.TLabel", foreground=p["muted"], font=fonts["small"])
    style.configure("Link.TLabel", foreground=p["accent"], font=fonts["small"])
    style.configure("Heading.TLabel", foreground=p["text"], font=fonts["bold"])
    style.configure("Placeholder.TLabel", foreground=p["muted"], background=p["surface"])
    for name, back in (("Help.TLabel", p["bg"]), ("HelpRaised.TLabel", p["raised"])):
        style.configure(name, foreground=p["muted"], background=back, font=fonts["small"], padding=(1, 0))
        style.map(name, foreground=[("active", p["accent"])])
    _restyle_themed(p)
    set_title_bar(root, name == "dark")
    return p
