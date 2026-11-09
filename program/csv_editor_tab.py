"""The Edit tab: open any CSV as a spreadsheet and change it in place.

Cells are edited by typing, with copy, paste, undo, find, sorting and row and column tools on the
right-click menu. Every cell is kept as text exactly as it is in the file (an "NA" stays "NA", an ID
keeps its leading zeros). Saving over the file first keeps the old version in backups/, so "Undo last
change" on the Modify tab brings it back.
"""
import csv
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import pandas as pd
from tksheet import Sheet

from . import csv_editing
from .gui_widgets import CSV_TYPES, CsvPicker

ENCODINGS = ("utf-8-sig", "cp1252")


def read_cells(path):
    """(headers, rows, note) for a CSV, every cell as text.

    Hand-edited files sometimes pad their cells (`, "gbif image, no ruler"`); when the plain reading
    leaves rows of different lengths the file is read again ignoring the spaces after each comma, and
    the reading with fewer ragged rows wins. Short rows are padded and unnamed extra columns named.
    """
    last_error = None
    for encoding in ENCODINGS:
        try:
            with open(path, newline="", encoding=encoding) as handle:
                text = handle.read()
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    else:
        raise ValueError(f"Could not read {Path(path).name} as text: {last_error}")

    def parse(skip):
        return [row for row in csv.reader(text.splitlines(keepends=True), skipinitialspace=skip)]

    def ragged(rows):
        return sum(1 for row in rows[1:] if len(row) != len(rows[0])) if rows else 0

    rows = parse(False)
    note = ""
    if ragged(rows):
        padded = parse(True)
        if ragged(padded) < ragged(rows):
            rows = [[cell.strip() for cell in row] for row in padded]
            note = "cells in this file were padded with spaces; they are shown trimmed"
    rows = [row for row in rows if row]                 # a blank line is not a record
    if not rows:
        return [], [], "the file is empty"
    width = max(len(row) for row in rows)
    headers = [str(h).strip() for h in rows[0]] + [f"column {n}" for n in range(len(rows[0]) + 1, width + 1)]
    body = [row + [""] * (width - len(row)) for row in rows[1:]]
    if width > len(rows[0]):
        note = (note + "; " if note else "") + f"{width - len(rows[0])} columns had no heading and were named"
    return headers, body, note


class EditTab(ttk.Frame):
    def __init__(self, notebook, app, source):
        super().__init__(notebook, padding=16)
        self.app, self.source = app, source
        self.path, self.modified, self.stale, self.note = "", False, True, ""
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        file_box = ttk.LabelFrame(self, text="CSV file to edit")
        file_box.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        file_box.columnconfigure(0, weight=1)
        self.picker = CsvPicker(file_box, app, source.path_var, source.choices, source.select,
                                extras=[("Reload", self._reload)])
        self.picker.grid(row=0, column=0, sticky="ew")

        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.save_button = ttk.Button(bar, text="Save", style="Accent.TButton", command=self.save)
        self.save_button.pack(side="left")
        ttk.Button(bar, text="Save as…", command=self.save_as).pack(side="left", padx=(6, 14))
        for text, command in (("Add row", self._add_row), ("Delete rows", self._delete_rows),
                              ("Add column…", self._add_column), ("Delete columns", self._delete_columns),
                              ("Rename…", self._rename_column)):
            ttk.Button(bar, text=text, command=command).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Clear", command=self._clear_filter).pack(side="right")
        self.filter_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.filter_var, width=16)
        entry.pack(side="right", padx=6)
        entry.bind("<Return>", lambda _: self._apply_filter())
        ttk.Label(bar, text="Filter rows").pack(side="right")

        self.sheet = Sheet(self, data=[[]], headers=[], show_row_index=True, default_column_width=130,
                           max_undos=50, font=("TkDefaultFont", 10, "normal"),
                           header_font=("TkDefaultFont", 10, "bold"))
        self.sheet.enable_bindings("all", "edit_header", "ctrl_select")
        self.sheet.grid(row=2, column=0, sticky="nsew")
        self.sheet.bind("<<SheetModified>>", self._on_modified)
        foot = ttk.Frame(self)
        foot.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.state = ttk.Label(foot, style="Heading.TLabel")
        self.state.pack(side="right", padx=(12, 0))
        ttk.Label(foot, style="Muted.TLabel", wraplength=820, justify="left",
                  text="Double-click a cell to edit it, or just type. Right-click for sorting and row and column "
                       "tools; double-click a heading to rename it. Copy, paste, undo and find use the usual keys. "
                       "Type in Filter rows and press Enter to show only matching rows.").pack(side="left")
        self.restyle(app.palette)
        app.restyle_hooks.append(self.restyle)
        source.listeners.append(self._file_changed)

    # ------------------------------------------------------------------ appearance

    def restyle(self, p):
        self.sheet.set_options(
            table_bg=p["surface"], table_fg=p["text"], table_grid_fg=p["border"], frame_bg=p["bg"],
            header_bg=p["raised"], header_fg=p["text"], header_grid_fg=p["border"], header_border_fg=p["border"],
            index_bg=p["raised"], index_fg=p["muted"], index_grid_fg=p["border"], index_border_fg=p["border"],
            top_left_bg=p["raised"], top_left_fg=p["muted"], outline_color=p["border"],
            table_selected_cells_bg=p["select"], table_selected_cells_fg=p["text"],
            table_selected_cells_border_fg=p["accent"], table_selected_rows_bg=p["select"],
            table_selected_rows_fg=p["text"], table_selected_rows_border_fg=p["accent"],
            table_selected_columns_bg=p["select"], table_selected_columns_fg=p["text"],
            table_selected_columns_border_fg=p["accent"], header_selected_cells_bg=p["hover"],
            header_selected_cells_fg=p["text"], header_selected_columns_bg=p["hover"],
            header_selected_columns_fg=p["text"], index_selected_cells_bg=p["hover"],
            index_selected_cells_fg=p["text"], index_selected_rows_bg=p["hover"], index_selected_rows_fg=p["text"],
            table_editor_bg=p["surface"], table_editor_fg=p["text"], table_editor_select_bg=p["select"],
            table_editor_select_fg=p["text"], header_editor_bg=p["surface"], header_editor_fg=p["text"],
            popup_menu_bg=p["surface"], popup_menu_fg=p["text"], popup_menu_highlight_bg=p["select"],
            popup_menu_highlight_fg=p["text"], vertical_scroll_background=p["bg"],
            horizontal_scroll_background=p["bg"], resizing_line_fg=p["accent"])

    def _show_state(self):
        if not self.path:
            self.state.configure(text="")
            self.picker.set_info("Choose a CSV file. The files of the selected download are in the drop-down.")
            return
        rows, columns = self.sheet.total_rows(), self.sheet.total_columns()
        self.state.configure(text="Unsaved changes" if self.modified else "Saved",
                             foreground=self.app.palette["busy" if self.modified else "muted"])
        waiting = self.source.path()
        info = f"{Path(self.path).name}: {rows:,} rows, {columns} columns"
        if self.note:
            info += f"; {self.note}"
        if waiting and waiting != self.path:
            info += f". Save these edits (or Reload to drop them) to open {Path(waiting).name}."
        self.picker.set_info(info)

    # ------------------------------------------------------------------ loading

    def on_show(self):
        if self.stale:
            self._load_selected()

    def _file_changed(self):
        self.stale = True
        if self.app.notebook.select() == str(self):
            self._load_selected()

    def _load_selected(self):
        self.picker.refresh_choices()
        path = self.source.path()
        if self.modified and self.path and path != self.path:
            self._show_state()                          # keep the edits; say what is waiting
            return
        self.stale = False
        if not path or not Path(path).is_file():
            self.path, self.modified, self.note = "", False, ""
            self.sheet.set_sheet_data([[]])
            self.sheet.headers([])
            self.picker.set_info("Choose a CSV file. The files of the selected download are in the drop-down."
                                 if not path else f"{path} does not exist")
            self.state.configure(text="")
            return
        self._load(path)

    def _load(self, path):
        try:
            self.app.root.configure(cursor="watch")
            self.app.root.update_idletasks()
            headers, rows, note = read_cells(path)
        except Exception as exc:
            self.picker.set_info(f"Could not read the file: {exc}")
            return
        finally:
            self.app.root.configure(cursor="")
        self.filter_var.set("")
        self.sheet.reset(table=True, header=True, index=True, undo_stack=True, selections=True)
        self.sheet.set_sheet_data(rows if rows else [[""] * max(len(headers), 1)])
        self.sheet.headers(headers)
        self.sheet.display_rows("all", redraw=False)
        widths = []
        for i, header in enumerate(headers):
            longest = max((len(str(row[i])) for row in rows[:200]), default=0)
            widths.append(int(min(max(len(header) * 8, longest * 7) + 26, 320)))
        if widths:
            self.sheet.set_column_widths(widths)
        self.sheet.refresh()
        self.path, self.modified, self.note = str(path), False, note
        self._show_state()

    def _reload(self):
        if self.modified and not messagebox.askyesno(
                "Reload", f"Drop the unsaved changes to {Path(self.path).name}?", parent=self.app.root):
            return
        self.modified = False
        self.source.forget(self.source.path())
        self.stale = True
        self._load_selected()

    def _on_modified(self, _event=None):
        if self.path and not self.modified:
            self.modified = True
        self._show_state()

    # ------------------------------------------------------------------ saving

    def has_unsaved_changes(self):
        return bool(self.path and self.modified)

    def table(self):
        """The sheet as a DataFrame, or raises ValueError when the headings cannot be saved."""
        headers = [str(h).strip() for h in self.sheet.headers()]
        blank = [str(i + 1) for i, h in enumerate(headers) if not h]
        if blank:
            raise ValueError("Give every column a name (column " + ", ".join(blank) + " has none).")
        repeated = sorted({h for h in headers if headers.count(h) > 1})
        if repeated:
            raise ValueError("Two columns have the same name: " + ", ".join(repeated))
        rows = [["" if cell is None else str(cell) for cell in row] for row in self.sheet.get_sheet_data()]
        rows = [row[:len(headers)] + [""] * (len(headers) - len(row)) for row in rows]
        return pd.DataFrame(rows, columns=headers, dtype="object")

    def save(self, target=None):
        """Write the sheet to its file (or to `target`). Returns True when it was saved."""
        if not self.path:
            messagebox.showinfo("Save", "Choose a CSV file to edit first.", parent=self.app.root)
            return False
        try:
            table = self.table()
            out, note = csv_editing.write_result(table, self.path, target)
        except Exception as exc:
            messagebox.showerror("Could not save", str(exc), parent=self.app.root)
            return False
        self.app._log(f"Saved {len(table):,} rows and {len(table.columns)} columns to {out}"
                      + (f" ({note})" if note else ""))
        self.source.forget(out)
        self.modified = False
        if target is not None:
            self.path = str(out)                        # carry on in the new file
            self.source.select(str(out))
        elif self.source.path() != self.path:
            self.stale = True                           # another file was waiting for these edits to be saved
            self._load_selected()
        else:
            for listener in self.source.listeners:      # the Modify and Analysis tabs read the file again
                if listener != self._file_changed:
                    listener()
            self._show_state()
        return True

    def save_as(self):
        if not self.path:
            messagebox.showinfo("Save as", "Choose a CSV file to edit first.", parent=self.app.root)
            return
        suggested = csv_editing.unique_output(self.path, "edited")
        path = filedialog.asksaveasfilename(parent=self.app.root, title="Save as", defaultextension=".csv",
                                            filetypes=CSV_TYPES, initialdir=str(suggested.parent),
                                            initialfile=suggested.name)
        if path:
            self.save(Path(path))

    def confirm_close(self):
        """Ask about unsaved changes before the app closes. Returns False to keep the app open."""
        if not self.has_unsaved_changes():
            return True
        answer = messagebox.askyesnocancel("Unsaved changes", f"Save the changes to {Path(self.path).name}?",
                                           parent=self.app.root)
        if answer is None:
            return False
        return self.save() if answer else True

    # ------------------------------------------------------------------ rows and columns

    def _need_file(self):
        if not self.path:
            self.bell()
        return bool(self.path)

    def _selected_rows(self):
        rows = set(self.sheet.get_selected_rows(get_cells_as_rows=True))
        return sorted(self.sheet.displayed_row_to_data(r) for r in rows)

    def _selected_columns(self):
        return sorted(set(self.sheet.get_selected_columns(get_cells_as_columns=True)))

    def _add_row(self):
        if not self._need_file():
            return
        rows = self._selected_rows()
        index = rows[-1] + 1 if rows else self.sheet.total_rows()
        self.sheet.insert_rows(1, idx=index, emit_event=True)
        self.sheet.see(index, 0)

    def _delete_rows(self):
        rows = self._selected_rows()
        if not rows:
            self.bell()
            return
        if len(rows) > 1 and not messagebox.askyesno("Delete rows", f"Delete {len(rows)} rows?", parent=self.app.root):
            return
        self.sheet.del_rows(rows, emit_event=True)

    def _add_column(self):
        if not self._need_file():
            return
        name = simpledialog.askstring("Add column", "Name of the new column:", parent=self.app.root)
        if not name or not name.strip():
            return
        if name.strip() in [str(h) for h in self.sheet.headers()]:
            messagebox.showwarning("Add column", f"There is already a column named {name.strip()}.",
                                   parent=self.app.root)
            return
        columns = self._selected_columns()
        index = columns[-1] + 1 if columns else self.sheet.total_columns()
        self.sheet.insert_columns(1, idx=index, emit_event=True)
        self.sheet.headers(name.strip(), index=index)
        self.sheet.see(0, index)
        self._on_modified()

    def _delete_columns(self):
        columns = self._selected_columns()
        if not columns:
            self.bell()
            return
        names = [str(self.sheet.headers()[c]) for c in columns]
        if not messagebox.askyesno("Delete columns", "Delete " + (
                f"the column {names[0]}?" if len(names) == 1 else f"{len(names)} columns ({', '.join(names[:5])}"
                + (" …" if len(names) > 5 else "") + ")?"), parent=self.app.root):
            return
        self.sheet.del_columns(columns, emit_event=True)

    def _rename_column(self):
        columns = self._selected_columns()
        if len(columns) != 1:
            messagebox.showinfo("Rename column", "Select one cell or heading in the column to rename.",
                                parent=self.app.root)
            return
        current = str(self.sheet.headers()[columns[0]])
        name = simpledialog.askstring("Rename column", f"New name for {current}:", initialvalue=current,
                                      parent=self.app.root)
        if name and name.strip() and name.strip() != current:
            self.sheet.headers(name.strip(), index=columns[0])
            self._on_modified()

    # ------------------------------------------------------------------ filter

    def _apply_filter(self):
        term = self.filter_var.get().strip().lower()
        if not term:
            return self._clear_filter()
        rows = [i for i, row in enumerate(self.sheet.get_sheet_data())
                if any(term in str(cell).lower() for cell in row)]
        self.sheet.display_rows(rows, all_rows_displayed=False, redraw=True)
        self.picker.set_info(f"{Path(self.path).name}: {len(rows):,} of {self.sheet.total_rows():,} rows contain "
                             f"“{self.filter_var.get().strip()}”. Edits still apply to the whole file.")

    def _clear_filter(self):
        self.filter_var.set("")
        self.sheet.display_rows("all", redraw=True)
        self._show_state()
