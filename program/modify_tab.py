"""The Modify tab: change any CSV by choosing columns, joining two files, editing rows or combining files."""
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import modify
from .csv_source import CsvPicker
from .utils import save_csv
from .widgets import CSV_TYPES, ColumnChecklist, DataTable, ScrollFrame, _check_image, _set_entry

ALL_COLUMNS = "(all columns)"


def _combo(parent, values, width=26, variable=None, **grid):
    box = ttk.Combobox(parent, values=list(values), state="readonly", width=width, textvariable=variable)
    if grid:
        box.grid(**grid)
    return box


def _fix_combo(box, values, keep=True):
    """Replace a drop-down's choices, keeping the current choice when it is still available."""
    values = list(values)
    current = box.get()
    box.configure(values=values)
    box.set(current if keep and current in values else (values[0] if values else ""))


class OutputBar(ttk.LabelFrame):
    """Where the result goes. Shared by every tool on the Modify tab."""

    def __init__(self, parent):
        super().__init__(parent, text="Save the result")
        self.columnconfigure(1, weight=1)
        self.mode = tk.StringVar(value="auto")
        self.continue_var = tk.BooleanVar(value=True)
        ttk.Radiobutton(self, text="As a new file next to the original (named automatically)", variable=self.mode,
                        value="auto").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(self, text="As", variable=self.mode, value="path").grid(row=1, column=0, sticky="w")
        self.entry = ttk.Entry(self)
        self.entry.grid(row=1, column=1, sticky="ew", padx=(4, 0))
        self.entry.bind("<Key>", lambda _: self.mode.set("path"))
        ttk.Button(self, text="Browse…", command=self._browse).grid(row=1, column=2, padx=(6, 0))
        self.replace_button = ttk.Radiobutton(
            self, text="Replace the original (the old version is kept, and Undo brings it back)",
            variable=self.mode, value="replace")
        self.replace_button.grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(self, text="Open the result afterwards so the next change builds on it",
                        variable=self.continue_var).grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _browse(self):
        path = filedialog.asksaveasfilename(parent=self.winfo_toplevel(), title="Save as", defaultextension=".csv",
                                            filetypes=CSV_TYPES)
        if path:
            _set_entry(self.entry, path)
            self.mode.set("path")

    def target(self, source, suffix, allow_replace=True):
        """The file to write: None to replace `source`, otherwise a path."""
        mode = self.mode.get()
        if mode == "replace":
            if not allow_replace:
                raise ValueError("Choose a new file name; this tool cannot replace the original.")
            return None
        if mode == "path":
            path = self.entry.get().strip()
            if not path:
                raise ValueError("Enter a file name for the result.")
            return Path(path)
        return modify.unique_output(source, suffix)


class ColumnEditor(ttk.Frame):
    """Check the columns to keep, drag them into order and rename them."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.original, self.order, self.checked, self.renames, self.filled, self.rows = [], [], set(), {}, {}, 0
        self.box_on = _check_image(self, app.palette, True)
        self.box_off = _check_image(self, app.palette, False)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(top, text="Search").pack(side="left")
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self._refresh())
        ttk.Entry(top, textvariable=self.query, width=26).pack(side="left", padx=6)
        self.count_label = ttk.Label(top, style="Muted.TLabel")
        self.count_label.pack(side="left", padx=6)

        frame = ttk.Frame(self)
        frame.grid(row=1, column=0, sticky="nsew")
        self.tree = ttk.Treeview(frame, columns=("new", "filled"), show="tree headings", selectmode="extended",
                                 height=7)
        for column, text, width, stretch in (("#0", "Column (checked columns are kept)", 280, True),
                                             ("new", "New name", 200, True), ("filled", "Filled", 110, False)):
            self.tree.heading(column, text=text, anchor="w")
            self.tree.column(column, width=width, anchor="w", stretch=stretch)
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda _: self._toggle(self.tree.selection()) or "break")
        self.tree.bind("<Double-Button-1>", self._on_double)

        buttons = ttk.Frame(self)
        buttons.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        for text, command in (("Check shown", lambda: self._set_shown(True)),
                              ("Uncheck shown", lambda: self._set_shown(False)),
                              ("Invert shown", self._invert_shown),
                              ("Uncheck empty columns", self._uncheck_empty),
                              ("Move up", lambda: self._move(-1)), ("Move down", lambda: self._move(1)),
                              ("Rename…", self._rename), ("Reset", self.reset)):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 6))

    def load(self, columns, rows, filled):
        self.original, self.rows, self.filled = list(columns), rows, filled
        self.reset()

    def reset(self):
        self.order, self.checked, self.renames = list(self.original), set(self.original), {}
        self.query.set("")
        self._refresh()

    def result(self):
        kept = [c for c in self.order if c in self.checked]
        removed = [c for c in self.order if c not in self.checked]
        return kept, dict(self.renames), removed

    def _shown(self):
        needle = self.query.get().strip().lower()
        return [c for c in self.order if not needle or needle in c.lower()
                or needle in self.renames.get(c, "").lower()]

    def _filled_text(self, column):
        count = self.filled.get(column, 0)
        return f"{100 * count / self.rows:.0f}%  ({count:,})" if self.rows else ""

    def _refresh(self, keep_selection=()):
        self.tree.delete(*self.tree.get_children())
        shown = self._shown()
        for column in shown:
            self.tree.insert("", "end", iid=column, text=column, values=(self.renames.get(column, ""),
                                                                           self._filled_text(column)),
                             image=self.box_on if column in self.checked else self.box_off)
        present = [c for c in keep_selection if c in shown]
        if present:
            self.tree.selection_set(present)
            self.tree.see(present[0])
        self.count_label.configure(text=f"{len(self.checked)} of {len(self.order)} kept")

    def _count(self):
        self.count_label.configure(text=f"{len(self.checked)} of {len(self.order)} kept")

    def _toggle(self, iids):
        iids = list(iids)
        target = any(i not in self.checked for i in iids)
        for iid in iids:
            (self.checked.add if target else self.checked.discard)(iid)
            self.tree.item(iid, image=self.box_on if target else self.box_off)
        self._count()

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) == "tree" and \
                self.tree.identify_element(event.x, event.y) == "image":
            iid = self.tree.identify_row(event.y)
            if iid:
                self._toggle([iid])
                return "break"

    def _on_double(self, event):
        if self.tree.identify_row(event.y) and self.tree.identify_column(event.x) == "#2":
            self._rename()

    def _set_shown(self, checked):
        for column in self._shown():
            (self.checked.add if checked else self.checked.discard)(column)
        self._refresh(self.tree.selection())

    def _invert_shown(self):
        for column in self._shown():
            (self.checked.discard if column in self.checked else self.checked.add)(column)
        self._refresh(self.tree.selection())

    def _uncheck_empty(self):
        empty = [c for c in self.order if self.filled.get(c, 0) == 0]
        self.checked -= set(empty)
        self._refresh(self.tree.selection())
        self.count_label.configure(text=f"{len(self.checked)} of {len(self.order)} kept; {len(empty)} empty")

    def _move(self, step):
        if self.query.get().strip():
            self.bell()
            self.count_label.configure(text="Clear the search to move columns")
            return
        selected = [c for c in self.order if c in set(self.tree.selection())]
        if not selected:
            return
        order = self.order
        for column in (selected if step < 0 else reversed(selected)):
            i = order.index(column)
            j = i + step
            if 0 <= j < len(order) and order[j] not in selected:
                order[i], order[j] = order[j], order[i]
        self._refresh(selected)

    def _rename(self):
        selection = self.tree.selection()
        if not selection:
            self.bell()
            return
        column = selection[0]
        name = simpledialog.askstring("Rename column", f"New name for {column} (empty to undo a rename):",
                                      initialvalue=self.renames.get(column, column), parent=self.winfo_toplevel())
        if name is None:
            return
        name = name.strip()
        if not name or name == column:
            self.renames.pop(column, None)
        else:
            self.renames[column] = name
        self._refresh([column])


class ModifyTab(ttk.Frame):
    def __init__(self, notebook, app, source):
        super().__init__(notebook, padding=16)
        self.app, self.source = app, source
        self.df, self.df_b, self.dirty = None, None, True
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        file_box = ttk.LabelFrame(self, text="CSV file to change (file A)")
        file_box.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        file_box.columnconfigure(0, weight=1)
        self.picker = CsvPicker(file_box, app, source.path_var, source.choices, source.select,
                                extras=[("Reload", source.reload), ("Undo last change", self._undo)])
        self.picker.grid(row=0, column=0, sticky="ew")

        self.pages = ttk.Notebook(self)
        self.pages.grid(row=1, column=0, sticky="nsew")
        for title, builder in (("Columns", self._build_columns), ("Join two files", self._build_join),
                               ("Rows", self._build_rows), ("Combine files", self._build_combine),
                               ("Preview", self._build_preview)):
            self.pages.add(builder(), text=title)

        self.output = OutputBar(self)
        self.output.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        source.listeners.append(self._file_changed)

    def _page(self):
        page = ttk.Frame(self.pages, padding=10)
        page.columnconfigure(0, weight=1)
        return page

    def _scrolled(self):
        page = ttk.Frame(self.pages, padding=(6, 6, 0, 6))
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)
        scroll = ScrollFrame(page, self.app.palette)
        scroll.grid(row=0, column=0, sticky="nsew")
        return page, scroll.inner

    def _hint(self, parent, text, row=0, columnspan=1):
        ttk.Label(parent, text=text, style="Muted.TLabel", wraplength=820, justify="left").grid(
            row=row, column=0, columnspan=columnspan, sticky="w", pady=(0, 6))

    # ------------------------------------------------------------------ loading

    def on_show(self):
        if self.dirty:
            self.refresh()

    def _visible(self):
        return self.app.notebook.select() == str(self)

    def _file_changed(self):
        self.dirty = True
        if self._visible():
            self.refresh()

    def refresh(self):
        self.dirty = False
        self.picker.refresh_choices()
        path = self.source.path()
        self.df = None
        if not path:
            self.picker.set_info("Choose a CSV file. The files of the selected download are in the drop-down.")
        elif not Path(path).is_file():
            self.picker.set_info(f"{path} does not exist")
        else:
            try:
                self.app.root.configure(cursor="watch")
                self.app.root.update_idletasks()
                self.df = self.source.table(path)
            except Exception as exc:
                self.picker.set_info(f"Could not read the file: {exc}")
            finally:
                self.app.root.configure(cursor="")
        if self.df is not None:
            backups = len(modify.list_backups(path))
            self.picker.set_info(f"{len(self.df):,} rows, {len(self.df.columns)} columns"
                                 + (f"; {backups} earlier version{'s' if backups != 1 else ''} can be restored"
                                    if backups else ""))
        self._fill_pages()

    def _columns(self):
        return list(self.df.columns) if self.df is not None else []

    def _fill_pages(self):
        columns = self._columns()
        filled = modify.filled_counts(self.df) if self.df is not None else {}
        self.editor.load(columns, len(self.df) if self.df is not None else 0, filled)
        self.removed_key.configure(values=columns)
        self.removed_key.set("gbifID" if "gbifID" in columns else (columns[0] if columns else ""))
        for box in (self.filter_column, self.sort_column):
            _fix_combo(box, columns)
        _fix_combo(self.replace_column, [ALL_COLUMNS, *columns])
        self.dup_list.delete(0, "end")
        for column in columns:
            self.dup_list.insert("end", column)
        self._rebuild_join_lists()
        self.preview.show(self.df.head(500) if self.df is not None else None,
                          note="Click a column heading to sort.")

    def _need_table(self):
        if self.df is None:
            messagebox.showwarning("No file", "Choose a CSV file to change first.", parent=self.app.root)
            return False
        return True

    # ------------------------------------------------------------------ running a change

    def _run(self, status, busy, suffix, build, needs_file=True, allow_replace=True, source_path=None):
        """`build()` runs in a worker thread and returns (table, summary, [(rows, name)])."""
        if self.app.running:
            return
        if needs_file and not self._need_table():
            return
        source_path = source_path or self.source.path()
        try:
            target = self.output.target(source_path, suffix, allow_replace)
        except ValueError as exc:
            messagebox.showwarning("Where to save", str(exc), parent=self.app.root)
            return
        follow = self.output.continue_var.get()

        def work():
            table, summary, side_files = build()
            out, note = modify.write_result(table, source_path, target)
            self.app._log(f"{summary}\n  Saved {len(table):,} rows and {len(table.columns)} columns to {out}"
                          + (f" ({note})" if note else ""))
            for rows, name in side_files:
                if len(rows):
                    side = modify.default_output(out, name)
                    save_csv(rows, side)
                    self.app._log(f"  Saved {len(rows):,} rows to {side}")
            self.source.forget(out)
            if target is None or follow:
                self.app._ui(self.source.select, str(out))
            return f"{summary.splitlines()[0]}; saved to {out.name}"

        self.app._start(status, busy, work)

    def _undo(self):
        path = self.source.path()
        if not path or not Path(path).is_file():
            messagebox.showinfo("Undo", "There is no file to restore.", parent=self.app.root)
            return
        if self.app.running:
            return
        backups = modify.list_backups(path)
        if not backups:
            messagebox.showinfo("Undo", f"{Path(path).name} has no earlier version. Versions are kept when a change "
                                        "replaces the original file.", parent=self.app.root)
            return
        if not messagebox.askyesno("Undo last change", f"Bring back the version of {Path(path).name} saved before "
                                   f"{backups[-1].stem.rsplit('_', 1)[-1]}? The current version is replaced.",
                                   parent=self.app.root):
            return
        try:
            name = modify.restore_latest(path)
        except Exception as exc:
            messagebox.showerror("Undo", str(exc), parent=self.app.root)
            return
        self.app._log(f"Restored the earlier version of {Path(path).name} from backups/{name}")
        self.source.reload()

    # ------------------------------------------------------------------ columns

    def _build_columns(self):
        page = self._page()
        page.rowconfigure(1, weight=1)
        self._hint(page, "Tick the columns to keep, move them into the order you want and rename them. Unticked "
                         "columns are removed. Click a box to tick it, or select rows and press Space. Double-click "
                         "a New name cell to rename it.")
        self.editor = ColumnEditor(page, self.app)
        self.editor.grid(row=1, column=0, sticky="nsew")
        options = ttk.Frame(page)
        options.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.save_removed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Also save the removed columns to a separate file, with this column to match "
                                      "them again:", variable=self.save_removed_var).pack(side="left")
        self.removed_key = ttk.Combobox(options, state="readonly", width=22)
        self.removed_key.pack(side="left", padx=6)
        self.columns_status = self.app._action(page, 3, [("Apply columns", self._apply_columns)], columnspan=1)
        return page

    def _apply_columns(self):
        if not self._need_table():
            return
        kept, renames, removed = self.editor.result()
        if not kept:
            messagebox.showwarning("No columns", "Keep at least one column.", parent=self.app.root)
            return
        df, key, save_removed = self.df, self.removed_key.get(), self.save_removed_var.get()

        def build():
            table = modify.select_columns(df, kept, renames)
            side = []
            if save_removed and removed:
                cols = ([key] if key and key not in removed and key in df.columns else []) + removed
                side.append((df[cols], "removed_columns"))
            summary = f"Kept {len(kept)} columns, removed {len(removed)}" + (
                f", renamed {len(renames)}" if renames else "")
            return table, summary, side

        self._run(self.columns_status, "Saving…", "columns", build)

    # ------------------------------------------------------------------ join

    def _build_join(self):
        page, body = self._scrolled()
        self._hint(body, "Join any two CSV files on a column they share. Check the columns to carry over from each "
                         "file and the column each file is matched on. File A is the CSV chosen at the top.")
        file_b = ttk.Frame(body)
        file_b.grid(row=1, column=0, sticky="ew")
        file_b.columnconfigure(0, weight=1)
        self.b_var = tk.StringVar()
        self.picker_b = CsvPicker(file_b, self.app, self.b_var, self.source.choices, self._select_b)
        self.picker_b.label("File B")
        self.picker_b.grid(row=0, column=0, sticky="ew")
        self.lists = ttk.Frame(body)
        self.lists.grid(row=2, column=0, sticky="nsew", pady=(6, 6))
        self.lists.columnconfigure((0, 1), weight=1, uniform="lists")
        self.list_a = self.list_b = None
        self._join_placeholder = ttk.Label(self.lists, style="Muted.TLabel",
                                           text="Choose file A at the top and file B above to see their columns.")
        self._join_placeholder.grid(row=0, column=0, columnspan=2, sticky="w")
        self.match_label = ttk.Label(body, style="Heading.TLabel")
        self.match_label.grid(row=3, column=0, sticky="w")

        options = ttk.Frame(body)
        options.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        options.columnconfigure(1, weight=1)
        self.join_type = _combo(options, modify.JOIN_TYPES, width=46)
        self.join_type.set(next(iter(modify.JOIN_TYPES)))
        self.conflict = _combo(options, modify.CONFLICT_CHOICES, width=46)
        self.conflict.set(next(iter(modify.CONFLICT_CHOICES)))
        self.duplicates = _combo(options, modify.DUPLICATE_CHOICES, width=46)
        self.duplicates.set(next(iter(modify.DUPLICATE_CHOICES)))
        for row, (text, box) in enumerate((("Rows to keep", self.join_type), ("A column is in both files", self.conflict),
                                           ("File B has several rows per match", self.duplicates))):
            ttk.Label(options, text=text).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
            box.grid(row=row, column=1, sticky="w", pady=2)
        self.loose_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="Ignore capital letters and extra spaces when matching",
                        variable=self.loose_var, command=self._update_matches).grid(row=3, column=0, columnspan=2,
                                                                                    sticky="w")
        self.join_status = self.app._action(body, 5, [("Join files", self._apply_join)], columnspan=1)
        return page

    def _select_b(self, path):
        self.b_var.set(path)
        self.df_b = None
        if path and Path(path).is_file():
            try:
                self.df_b = self.source.table(path)
                self.picker_b.set_info(f"{len(self.df_b):,} rows, {len(self.df_b.columns)} columns")
            except Exception as exc:
                self.picker_b.set_info(f"Could not read the file: {exc}")
        elif path:
            self.picker_b.set_info(f"{path} does not exist")
        self._rebuild_join_lists()

    def _guess_keys(self):
        a, b = self._columns(), list(self.df_b.columns) if self.df_b is not None else []
        shared = [c for c in a if c in b]
        preferred = "gbifID" if "gbifID" in shared else (shared[0] if shared else None)
        return preferred or (a[0] if a else ""), preferred or (b[0] if b else "")

    def _rebuild_join_lists(self):
        for widget in (self.list_a, self.list_b):
            if widget is not None:
                widget.destroy()
        self.list_a = self.list_b = None
        if self.df is None or self.df_b is None:
            self._join_placeholder.grid()
            self.match_label.configure(text="")
            return
        self._join_placeholder.grid_remove()
        key_a, key_b = self._guess_keys()
        a_cols, b_cols = list(self.df.columns), list(self.df_b.columns)
        self.list_a = ColumnChecklist(self.lists, self.app, f"File A: {Path(self.source.path()).name}", a_cols,
                                      a_cols, key_a, height=6)
        self.list_a.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.list_b = ColumnChecklist(self.lists, self.app, f"File B: {Path(self.b_var.get()).name}", b_cols,
                                      [c for c in b_cols if c != key_b], key_b, height=6)
        self.list_b.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        for checklist in (self.list_a, self.list_b):
            checklist.key_box.bind("<<ComboboxSelected>>", lambda _: self._update_matches())
        self._update_matches()

    def _update_matches(self):
        if self.list_a is None:
            return
        try:
            matched, total, keys = modify.count_matches(self.df, self.list_a.key_var.get(), self.df_b,
                                                        self.list_b.key_var.get(), self.loose_var.get())
            self.match_label.configure(text=f"{matched:,} of {total:,} rows in A match one of {keys:,} keys in B")
        except Exception as exc:
            self.match_label.configure(text=f"Could not compare the match columns: {exc}")

    def _apply_join(self):
        if not self._need_table():
            return
        if self.list_a is None:
            messagebox.showwarning("File B", "Choose file B first.", parent=self.app.root)
            return
        a, b = self.df, self.df_b
        lk, rk = self.list_a.key_var.get(), self.list_b.key_var.get()
        params = dict(how=modify.JOIN_TYPES[self.join_type.get()], left_columns=self.list_a.selected(),
                      right_columns=self.list_b.selected(), conflict=modify.CONFLICT_CHOICES[self.conflict.get()],
                      duplicates=modify.DUPLICATE_CHOICES[self.duplicates.get()], loose=self.loose_var.get())
        names = (Path(self.source.path()).name, Path(self.b_var.get()).name)

        def build():
            table, info = modify.join_tables(a, b, lk, rk, **params)
            summary = (f"Joined {names[0]} with {names[1]} on {lk} = {rk}: {info['matched']:,} of "
                       f"{info['left_rows']:,} rows in A matched")
            if info["shared"] and params["conflict"] != "both":
                summary += f"\n  {len(info['shared'])} columns were in both files and were merged"
            if info["unmatched_right"] and params["how"] in ("left", "inner"):
                summary += f"\n  {info['unmatched_right']:,} keys in B were not in A"
            return table, summary, []

        self._run(self.join_status, "Joining…", "joined", build)

    # ------------------------------------------------------------------ rows

    def _build_rows(self):
        page, body = self._scrolled()
        body.columnconfigure((0, 1), weight=1, uniform="tools")
        self._hint(body, "Each tool changes file A and saves the result as set under Save the result.", columnspan=2)

        # filter
        box = ttk.LabelFrame(body, text="Filter rows")
        box.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        box.columnconfigure(1, weight=1)
        self.filter_column = _combo(box, [], width=24)
        self.filter_op = _combo(box, modify.FILTER_OPS, width=24)
        self.filter_op.set(modify.FILTER_OPS[0])
        self.filter_value = ttk.Entry(box)
        for row, (text, widget) in enumerate((("Column", self.filter_column), ("Condition", self.filter_op),
                                              ("Value", self.filter_value))):
            ttk.Label(box, text=text).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            widget.grid(row=row, column=1, sticky="ew", pady=2)
        self.filter_keep = tk.StringVar(value="keep")
        ttk.Radiobutton(box, text="Keep the matching rows", variable=self.filter_keep, value="keep").grid(
            row=3, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(box, text="Remove the matching rows", variable=self.filter_keep, value="remove").grid(
            row=4, column=0, columnspan=2, sticky="w")
        self.filter_case = tk.BooleanVar(value=False)
        self.filter_save = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Match capital letters", variable=self.filter_case).grid(
            row=5, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(box, text="Also save the rows that were left out", variable=self.filter_save).grid(
            row=6, column=0, columnspan=2, sticky="w")
        self.filter_status = self.app._action(box, 7, [("Filter", self._apply_filter)], columnspan=2)

        # duplicates
        box = ttk.LabelFrame(body, text="Remove duplicate rows")
        box.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))
        box.columnconfigure(0, weight=1)
        ttk.Label(box, text="Rows are duplicates when these columns match (select none to compare whole rows)",
                  style="Muted.TLabel", wraplength=380, justify="left").grid(row=0, column=0, columnspan=2,
                                                                              sticky="w")
        holder = ttk.Frame(box)
        holder.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=4)
        holder.columnconfigure(0, weight=1)
        self.dup_list = tk.Listbox(holder, selectmode="extended", height=5, exportselection=False,
                                   background=self.app.palette["surface"], foreground=self.app.palette["text"],
                                   selectbackground=self.app.palette["select"],
                                   selectforeground=self.app.palette["text"], highlightthickness=1,
                                   highlightbackground=self.app.palette["border"], relief="flat")
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.dup_list.yview)
        self.dup_list.configure(yscrollcommand=bar.set)
        self.dup_list.grid(row=0, column=0, sticky="ew")
        bar.grid(row=0, column=1, sticky="ns")
        self.dup_keep = tk.StringVar(value="first")
        ttk.Radiobutton(box, text="Keep the first of each", variable=self.dup_keep, value="first").grid(
            row=2, column=0, sticky="w")
        ttk.Radiobutton(box, text="Keep the last of each", variable=self.dup_keep, value="last").grid(
            row=3, column=0, sticky="w")
        self.dup_save = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Also save the removed duplicates", variable=self.dup_save).grid(
            row=4, column=0, sticky="w")
        self.dup_status = self.app._action(box, 5, [("Remove duplicates", self._apply_duplicates)], columnspan=2)

        # find and replace
        box = ttk.LabelFrame(body, text="Find and replace")
        box.grid(row=2, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        box.columnconfigure(1, weight=1)
        self.replace_column = _combo(box, [ALL_COLUMNS], width=24)
        self.replace_find = ttk.Entry(box)
        self.replace_with = ttk.Entry(box)
        for row, (text, widget) in enumerate((("In", self.replace_column), ("Find", self.replace_find),
                                              ("Replace with", self.replace_with))):
            ttk.Label(box, text=text).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            widget.grid(row=row, column=1, sticky="ew", pady=2)
        self.replace_whole = tk.BooleanVar(value=False)
        self.replace_case = tk.BooleanVar(value=False)
        self.replace_regex = tk.BooleanVar(value=False)
        for row, (text, var) in enumerate((("The whole cell must match", self.replace_whole),
                                           ("Match capital letters", self.replace_case),
                                           ("The text to find is a regular expression", self.replace_regex)), start=3):
            ttk.Checkbutton(box, text=text, variable=var).grid(row=row, column=0, columnspan=2, sticky="w")
        self.replace_status = self.app._action(box, 6, [("Replace", self._apply_replace)], columnspan=2)

        # sort
        box = ttk.LabelFrame(body, text="Sort rows")
        box.grid(row=2, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))
        box.columnconfigure(1, weight=1)
        self.sort_column = _combo(box, [], width=24)
        ttk.Label(box, text="Sort by").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
        self.sort_column.grid(row=0, column=1, sticky="ew", pady=2)
        self.sort_desc = tk.BooleanVar(value=False)
        self.sort_numbers = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Largest or last first", variable=self.sort_desc).grid(
            row=1, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(box, text="Sort as numbers (10 comes after 9)", variable=self.sort_numbers).grid(
            row=2, column=0, columnspan=2, sticky="w")
        self.sort_status = self.app._action(box, 3, [("Sort", self._apply_sort)], columnspan=2)

        # tidy
        box = ttk.LabelFrame(body, text="Tidy up")
        box.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(0, 8))
        self.tidy_vars = {}
        for column, (key, text) in enumerate((("trim", "Trim spaces around every value"),
                                              ("empty_columns", "Remove columns that are empty in every row"),
                                              ("empty_rows", "Remove rows that are empty in every column"))):
            self.tidy_vars[key] = tk.BooleanVar(value=True)
            ttk.Checkbutton(box, text=text, variable=self.tidy_vars[key]).grid(row=0, column=column, sticky="w",
                                                                              padx=(0, 16))
        self.tidy_status = self.app._action(box, 1, [("Tidy up", self._apply_tidy)], columnspan=3)
        return page

    def _apply_filter(self):
        if not self._need_table():
            return
        column, op, value = self.filter_column.get(), self.filter_op.get(), self.filter_value.get()
        keep, case, save_rest, df = self.filter_keep.get() == "keep", self.filter_case.get(), \
            self.filter_save.get(), self.df
        if op not in modify.NO_VALUE_OPS and not value.strip():
            messagebox.showwarning("Filter rows", "Enter a value to compare with.", parent=self.app.root)
            return

        def build():
            kept, rest = modify.filter_rows(df, column, op, value, keep, case)
            summary = (f"Filter {'kept' if keep else 'removed'} rows where {column} {op}"
                       f"{'' if op in modify.NO_VALUE_OPS else ' ' + value}: {len(kept):,} rows left, "
                       f"{len(rest):,} left out")
            return kept, summary, [(rest, "left_out")] if save_rest else []

        self._run(self.filter_status, "Filtering…", "filtered", build)

    def _apply_duplicates(self):
        if not self._need_table():
            return
        subset = [self.dup_list.get(i) for i in self.dup_list.curselection()]
        keep, save, df = self.dup_keep.get(), self.dup_save.get(), self.df

        def build():
            unique, duplicates = modify.drop_duplicates(df, subset, keep)
            on = ", ".join(subset) if subset else "all columns"
            return unique, f"Removed {len(duplicates):,} duplicate rows (compared on {on})", \
                [(duplicates, "duplicates")] if save else []

        self._run(self.dup_status, "Working…", "deduplicated", build)

    def _apply_replace(self):
        if not self._need_table():
            return
        column, find, with_, df = self.replace_column.get(), self.replace_find.get(), self.replace_with.get(), self.df
        columns = list(df.columns) if column == ALL_COLUMNS else [column]
        options = dict(whole_cell=self.replace_whole.get(), case=self.replace_case.get(),
                       regex=self.replace_regex.get())
        if not find:
            messagebox.showwarning("Find and replace", "Enter the text to find.", parent=self.app.root)
            return

        def build():
            table, changed = modify.replace_values(df, columns, find, with_, **options)
            return table, f"Replaced {find!r} with {with_!r} in {changed:,} cells of {column}", []

        self._run(self.replace_status, "Replacing…", "replaced", build)

    def _apply_sort(self):
        if not self._need_table():
            return
        column, desc, numeric, df = self.sort_column.get(), self.sort_desc.get(), self.sort_numbers.get(), self.df

        def build():
            return modify.sort_rows(df, column, desc, numeric), f"Sorted by {column}" + (
                " (largest first)" if desc else ""), []

        self._run(self.sort_status, "Sorting…", "sorted", build)

    def _apply_tidy(self):
        if not self._need_table():
            return
        flags = {k: v.get() for k, v in self.tidy_vars.items()}
        df = self.df

        def build():
            table, notes = modify.tidy(df, flags["empty_columns"], flags["empty_rows"], flags["trim"])
            dropped = notes.get("columns_dropped", [])
            summary = f"Tidied up: {len(dropped)} empty columns and {notes.get('rows_dropped', 0):,} empty rows removed"
            if dropped:
                summary += "\n  Removed columns: " + ", ".join(dropped[:10]) + (" and more" if len(dropped) > 10 else "")
            return table, summary, []

        self._run(self.tidy_status, "Tidying…", "tidy", build)

    # ------------------------------------------------------------------ combine

    def _build_combine(self):
        page = self._page()
        page.rowconfigure(1, weight=1)
        self._hint(page, "Stack the rows of two or more CSV files into one. Columns are matched by name; a column "
                         "missing from a file is left empty for its rows. The result is always a new file.")
        holder = ttk.Frame(page)
        holder.grid(row=1, column=0, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        self.combine_list = tk.Listbox(holder, selectmode="extended", height=6, exportselection=False,
                                       background=self.app.palette["surface"], foreground=self.app.palette["text"],
                                       selectbackground=self.app.palette["select"],
                                       selectforeground=self.app.palette["text"], highlightthickness=1,
                                       highlightbackground=self.app.palette["border"], relief="flat")
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.combine_list.yview)
        self.combine_list.configure(yscrollcommand=bar.set)
        self.combine_list.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        buttons = ttk.Frame(page)
        buttons.grid(row=2, column=0, sticky="w", pady=6)
        for text, command in (("Add files…", self._combine_add), ("Add file A", self._combine_add_current),
                              ("Remove selected", self._combine_remove), ("Clear", lambda: self.combine_list.delete(0, "end"))):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 6))
        self.combine_source = tk.BooleanVar(value=True)
        self.combine_dedupe = tk.BooleanVar(value=False)
        ttk.Checkbutton(page, text="Add a source_file column with the name of the file each row came from",
                        variable=self.combine_source).grid(row=3, column=0, sticky="w")
        ttk.Checkbutton(page, text="Remove rows that are identical after combining",
                        variable=self.combine_dedupe).grid(row=4, column=0, sticky="w")
        self.combine_status = self.app._action(page, 5, [("Combine files", self._apply_combine)], columnspan=1)
        return page

    def _combine_add(self):
        paths = filedialog.askopenfilenames(parent=self.app.root, title="Select CSV files", filetypes=CSV_TYPES,
                                            initialdir=str(self.app.workspace.root) if self.app.workspace else None)
        for path in paths:
            self._combine_insert(path)

    def _combine_add_current(self):
        if self.source.path():
            self._combine_insert(self.source.path())

    def _combine_insert(self, path):
        if path not in self.combine_list.get(0, "end"):
            self.combine_list.insert("end", path)

    def _combine_remove(self):
        for index in reversed(self.combine_list.curselection()):
            self.combine_list.delete(index)

    def _apply_combine(self):
        paths = list(self.combine_list.get(0, "end"))
        if len(paths) < 2:
            messagebox.showwarning("Combine files", "Add at least two files.", parent=self.app.root)
            return
        add_source, dedupe, source = self.combine_source.get(), self.combine_dedupe.get(), self.source

        def build():
            tables = [(Path(p).name, source.table(p)) for p in paths]
            table = modify.combine(tables, "source_file" if add_source else None)
            summary = f"Combined {len(paths)} files into {len(table):,} rows"
            if dedupe:
                table, removed = modify.drop_duplicates(table)
                summary += f"; removed {len(removed):,} identical rows"
            return table, summary, []

        self._run(self.combine_status, "Combining…", "combined", build, needs_file=False, allow_replace=False,
                  source_path=paths[0])

    # ------------------------------------------------------------------ preview

    def _build_preview(self):
        page = self._page()
        page.rowconfigure(0, weight=1)
        self.preview = DataTable(page, height=12, max_rows=500, saveable=False, name="preview")
        self.preview.grid(row=0, column=0, sticky="nsew")
        return page

