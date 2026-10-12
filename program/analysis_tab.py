"""The Analysis tab: profile a CSV, count values, summarise numbers and compare groups."""
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import stats
from .csv_source import CsvPicker
from .widgets import BarChart, DataTable

CHART_STATS = ("mean", "median", "count", "min", "max", "sum")


class AnalysisTab(ttk.Frame):
    def __init__(self, notebook, app, source):
        super().__init__(notebook, padding=16)
        self.app, self.source = app, source
        self.df, self.profile, self.dirty = None, None, True
        self.columns, self.number_columns = [], []
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        file_box = ttk.LabelFrame(self, text="CSV file to analyze")
        file_box.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        file_box.columnconfigure(0, weight=1)
        self.picker = CsvPicker(file_box, app, source.path_var, source.choices, source.select,
                                extras=[("Reload", source.reload)])
        self.picker.grid(row=0, column=0, sticky="ew")
        self.status = ttk.Label(file_box, style="Muted.TLabel")
        self.status.grid(row=1, column=0, sticky="w")

        self.pages = ttk.Notebook(self)
        self.pages.grid(row=1, column=0, sticky="nsew")
        for title, builder in (("Overview", self._build_overview), ("Value counts", self._build_counts),
                               ("Numbers", self._build_numbers), ("Compare groups", self._build_groups)):
            self.pages.add(builder(), text=title)
        source.listeners.append(self._file_changed)

    def _page(self, hint):
        page = ttk.Frame(self.pages, padding=10)
        page.columnconfigure(0, weight=1)
        ttk.Label(page, text=hint, style="Muted.TLabel", wraplength=820, justify="left").grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        return page

    def _chart(self, parent):
        chart = BarChart(parent, self.app.palette)
        self.app.restyle_hooks.append(chart.restyle)
        return chart

    # ------------------------------------------------------------------ loading

    def on_show(self):
        if self.dirty:
            self.refresh()

    def _file_changed(self):
        self.dirty = True
        if self.app.notebook.select() == str(self):
            self.refresh()

    def refresh(self):
        path = self.source.path()
        self.picker.refresh_choices()
        if not path or not Path(path).is_file():
            self.dirty = False
            self._clear("Choose a CSV file. The files of the selected download are in the drop-down."
                        if not path else f"{path} does not exist")
            return
        if self.app.running:
            return
        key = self.key_box.get() or "gbifID"

        def work():
            df = self.source.table(path)
            profile = stats.column_profile(df)
            info = stats.overview(df, key if key in df.columns else None)
            self.app._ui(self._show, path, df, profile, info)
            return f"{len(df):,} rows analyzed"

        self.app._start(self.status, "Analyzing…", work)

    def _clear(self, message):
        self.df = self.profile = None
        self.columns = self.number_columns = []
        self.picker.set_info(message)
        self.summary.configure(text="")
        self.profile_table.show(None)
        for table in (self.counts_table, self.groups_table):
            table.show(None)
        for chart in (self.counts_chart, self.numbers_chart, self.groups_chart):
            chart.set([], [])
        self.numbers_table.show(None)
        self.number_summary.configure(text="")
        self._fill_boxes()

    def _show(self, path, df, profile, info):
        if path != self.source.path():
            return
        self.dirty = False
        self.df, self.profile = df, profile
        self.columns = list(df.columns)
        self.number_columns = list(profile.loc[profile["type"] == "number", "column"])
        self.picker.set_info(f"{len(df):,} rows, {len(df.columns)} columns")
        self.summary.configure(text="   ".join(f"{name}: {value:,}" if isinstance(value, int) else f"{name}: {value}"
                                               for name, value in info.items()))
        self.profile_table.show(profile, note="Click a heading to sort.")
        for table in (self.counts_table, self.groups_table, self.numbers_table):
            table.show(None)
        for chart in (self.counts_chart, self.numbers_chart, self.groups_chart):
            chart.set([], [])
        self.number_summary.configure(text="")
        self._fill_boxes()

    def _fill_boxes(self):
        def fill(box, values, prefer=None):
            current = box.get()
            box.configure(values=values)
            box.set(current if current in values else (prefer if prefer in values else (values[0] if values else "")))

        fill(self.key_box, self.columns, "gbifID")
        fill(self.counts_column, self.columns, "country" if "country" in self.columns else None)
        fill(self.number_column, self.number_columns)
        fill(self.group_column, self.columns, "country" if "country" in self.columns else None)
        fill(self.group_value, self.number_columns)

    def _need_table(self):
        if self.df is None:
            messagebox.showwarning("No file", "Choose a CSV file to analyze first.", parent=self.app.root)
            return False
        return True

    # ------------------------------------------------------------------ overview

    def _build_overview(self):
        page = self._page("What each column holds: how much of it is filled in, how many different values it has and, "
                          "for numbers, the range.")
        page.rowconfigure(2, weight=1)
        top = ttk.Frame(page)
        top.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.summary = ttk.Label(top, style="Heading.TLabel", wraplength=640, justify="left")
        self.summary.pack(side="left")
        self.key_box = ttk.Combobox(top, state="readonly", width=18)
        self.key_box.pack(side="right")
        ttk.Label(top, text="Count duplicate and blank values of", style="Muted.TLabel").pack(side="right", padx=6)
        self.key_box.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        self.profile_table = DataTable(page, height=9, max_rows=2000, name="column_summary")
        self.profile_table.grid(row=2, column=0, sticky="nsew")
        return page

    # ------------------------------------------------------------------ value counts

    def _build_counts(self):
        page = self._page("How often each value appears in a column.")
        page.rowconfigure(2, weight=1)
        controls = ttk.Frame(page)
        controls.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(controls, text="Column").pack(side="left")
        self.counts_column = ttk.Combobox(controls, state="readonly", width=28)
        self.counts_column.pack(side="left", padx=6)
        ttk.Label(controls, text="Top").pack(side="left", padx=(10, 0))
        self.counts_top = tk.StringVar(value="25")
        ttk.Spinbox(controls, textvariable=self.counts_top, from_=5, to=1000, increment=5, width=6).pack(
            side="left", padx=6)
        self.counts_blank = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Count blanks", variable=self.counts_blank).pack(side="left", padx=10)
        ttk.Button(controls, text="Show", style="Accent.TButton", command=self._show_counts).pack(side="left")
        panes = ttk.PanedWindow(page, orient="horizontal")
        panes.grid(row=2, column=0, sticky="nsew")
        self.counts_table = DataTable(panes, height=8, max_rows=1000, name="value_counts")
        self.counts_chart = self._chart(panes)
        panes.add(self.counts_table, weight=1)
        panes.add(self.counts_chart, weight=1)
        return page

    def _top(self, variable, default=25):
        try:
            return max(int(variable.get()), 1)
        except ValueError:
            return default

    def _show_counts(self):
        if not self._need_table():
            return
        column = self.counts_column.get()
        table = stats.value_counts(self.df, column, self._top(self.counts_top), self.counts_blank.get())
        distinct = self.df[column].fillna("").astype(str).str.strip().replace("", None).nunique()
        self.counts_table.show(table, note=f"{distinct:,} different values")
        head = table.head(40)
        self.counts_chart.set(head["value"], head["count"], title=f"{column}: most common values")

    # ------------------------------------------------------------------ numbers

    def _build_numbers(self):
        page = self._page("The spread of a numeric column: summary numbers and how many values fall in each range.")
        page.rowconfigure(3, weight=1)
        controls = ttk.Frame(page)
        controls.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(controls, text="Column").pack(side="left")
        self.number_column = ttk.Combobox(controls, state="readonly", width=28)
        self.number_column.pack(side="left", padx=6)
        ttk.Label(controls, text="Ranges").pack(side="left", padx=(10, 0))
        self.bins = tk.StringVar(value="15")
        ttk.Spinbox(controls, textvariable=self.bins, from_=2, to=100, width=5).pack(side="left", padx=6)
        ttk.Button(controls, text="Show", style="Accent.TButton", command=self._show_numbers).pack(side="left",
                                                                                                    padx=10)
        self.number_summary = ttk.Label(page, style="Heading.TLabel", wraplength=820, justify="left")
        self.number_summary.grid(row=2, column=0, sticky="w", pady=(0, 6))
        panes = ttk.PanedWindow(page, orient="horizontal")
        panes.grid(row=3, column=0, sticky="nsew")
        self.numbers_table = DataTable(panes, height=8, max_rows=200, name="histogram", searchable=False)
        self.numbers_chart = self._chart(panes)
        panes.add(self.numbers_table, weight=1)
        panes.add(self.numbers_chart, weight=1)
        return page

    def _show_numbers(self):
        if not self._need_table():
            return
        column = self.number_column.get()
        if not column:
            messagebox.showinfo("Numbers", "This file has no numeric columns.", parent=self.app.root)
            return
        summary = stats.number_summary(self.df[column])
        self.number_summary.configure(text="   ".join(
            f"{k}: {v:,}" if isinstance(v, int) else f"{k}: {v:.6g}" for k, v in summary.items()))
        histogram = stats.histogram(self.df[column], self._top(self.bins, 15))
        self.numbers_table.show(histogram)
        self.numbers_chart.set(histogram["range"], histogram["count"], title=f"{column}: values per range")

    # ------------------------------------------------------------------ groups

    def _build_groups(self):
        page = self._page("Compare a numeric column across the groups of another, such as the leaf width measured "
                          "for each country.")
        page.rowconfigure(2, weight=1)
        controls = ttk.Frame(page)
        controls.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.group_column = ttk.Combobox(controls, state="readonly", width=24)
        self.group_value = ttk.Combobox(controls, state="readonly", width=24)
        self.group_stat = ttk.Combobox(controls, state="readonly", width=8, values=CHART_STATS)
        self.group_stat.set("mean")
        self.group_top = tk.StringVar(value="25")
        for text, widget in (("Group by", self.group_column), ("Numbers in", self.group_value),
                             ("Chart", self.group_stat)):
            ttk.Label(controls, text=text).pack(side="left", padx=(0 if text == "Group by" else 10, 0))
            widget.pack(side="left", padx=6)
        ttk.Label(controls, text="Top").pack(side="left", padx=(10, 0))
        ttk.Spinbox(controls, textvariable=self.group_top, from_=5, to=500, increment=5, width=5).pack(
            side="left", padx=6)
        ttk.Button(controls, text="Show", style="Accent.TButton", command=self._show_groups).pack(side="left",
                                                                                                   padx=6)
        panes = ttk.PanedWindow(page, orient="horizontal")
        panes.grid(row=2, column=0, sticky="nsew")
        self.groups_table = DataTable(panes, height=8, max_rows=1000, name="group_summary")
        self.groups_chart = self._chart(panes)
        panes.add(self.groups_table, weight=3)
        panes.add(self.groups_chart, weight=2)
        return page

    def _show_groups(self):
        if not self._need_table():
            return
        group, value, chart_stat = self.group_column.get(), self.group_value.get(), self.group_stat.get()
        if not value:
            messagebox.showinfo("Compare groups", "This file has no numeric columns to compare.",
                                parent=self.app.root)
            return
        table = stats.group_summary(self.df, group, value)
        self.groups_table.show(table, note=f"{len(table):,} groups")
        head = table.head(self._top(self.group_top)).dropna(subset=[chart_stat])
        self.groups_chart.set(head[group], head[chart_stat], title=f"{chart_stat} of {value} by {group}")
