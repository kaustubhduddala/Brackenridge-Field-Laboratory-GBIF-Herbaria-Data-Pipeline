import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from config import (APP_NAME, APP_VERSION, BAD_GEOSPATIAL_ISSUES, BASE_DIR, DATA_DIR, DEFAULT_TOKEN_LIMIT,
                    GBIF_SIGNUP_URL, INSPECTION_ISSUES, LMSTUDIO_URL, LOG_FILE, PRECISION_NONE, PRECISION_RELAXED,
                    PRECISION_STRICT, PRESETS, THINKING_CHOICES, gbif_credentials, load_settings,
                    password_store_name, remove_gbif_account, save_gbif_account, save_settings, saved_gbif_account,
                    saved_gbif_password)

from . import site_login, sites, theme
from .analysis import (RESULT_COLUMNS, check_connection, count_matches, csv_columns, join_measurements,
                       measure_images, measurement_status)
from .analysis_tab import AnalysisTab
from .csv_source import CsvSource
from .gbif import check_gbif_login, download_dataset
from .media import download_media, media_status
from .processor import REQUIRED_COLUMNS, available_columns, run_cleaning
from .power import KeepAwake
from .modify_tab import ModifyTab
from .utils import TaskControl, find_dwca_files, open_path, scan_issues
from .widgets import CSV_TYPES, ColumnChecklist, PathCombo, ScrollFrame, _check_image, _set_entry, _set_text
from .workspace import (FULL_MASTER_NAME, INSPECT_NAME, list_workspaces, resolve_input, workspace_by_name,
                        workspace_of)

ISSUE_ACTIONS = ("Remove", "Inspect", "Nothing")
DEFAULT_PRESET = "Default"


class PresetBar(ttk.Frame):
    """Pick, save, delete and reset named presets. `presets` is edited in place."""

    def __init__(self, master, presets, apply, current, persist):
        super().__init__(master)
        self.presets, self.apply, self.current, self.persist = presets, apply, current, persist
        self.var = tk.StringVar(value=DEFAULT_PRESET)
        ttk.Label(self, text="Preset").pack(side="left")
        self.combo = ttk.Combobox(self, textvariable=self.var, state="readonly", width=22)
        self.combo.pack(side="left", padx=6)
        self.combo.bind("<<ComboboxSelected>>", lambda _: self.apply(self.var.get()))
        ttk.Button(self, text="Save as…", command=self._save).pack(side="left")
        ttk.Button(self, text="Delete", command=self._delete).pack(side="left", padx=6)
        ttk.Button(self, text="Reset to default", command=self.reset).pack(side="left")
        self._refresh()

    def _refresh(self):
        self.combo.configure(values=[DEFAULT_PRESET, *sorted(self.presets)])

    def _save(self):
        name = simpledialog.askstring("Save preset", "Preset name:", parent=self.winfo_toplevel())
        name = (name or "").strip()
        if not name:
            return
        if name == DEFAULT_PRESET:
            messagebox.showinfo("Save preset", f"{DEFAULT_PRESET} is reserved; choose another name.",
                                parent=self.winfo_toplevel())
            return
        self.presets[name] = self.current()
        self.persist()
        self._refresh()
        self.var.set(name)

    def _delete(self):
        name = self.var.get()
        if name in self.presets and messagebox.askyesno("Delete preset", f"Delete the preset {name}?",
                                                        parent=self.winfo_toplevel()):
            del self.presets[name]
            self.persist()
            self._refresh()
            self.reset()

    def reset(self):
        self.var.set(DEFAULT_PRESET)
        self.apply(DEFAULT_PRESET)


def _finish_dialog(dialog):
    dialog.bind("<Escape>", lambda _: dialog.destroy())
    try:
        dialog.wait_visibility()
        dialog.grab_set()
    except tk.TclError:
        pass


class IssueFilterDialog(tk.Toplevel):
    def __init__(self, app, counts, remove_issues, inspect_issues, default, presets, persist):
        super().__init__(app.root)
        self.title("Issue filters")
        self.geometry("720x600")
        self.minsize(560, 400)
        self.transient(app.root)
        self.configure(background=app.palette["bg"])
        theme.set_title_bar(self, app.theme_name == "dark")
        self.result = None
        self.counts = counts
        self.default = default
        self.presets = presets
        self.remove_issues, self.inspect_issues = set(remove_issues), set(inspect_issues)
        self.vars = {code: tk.StringVar() for code in counts}
        self.order = sorted(counts, key=lambda c: (-counts[c], c))
        self.rows = {}

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Issues found in the occurrence file. Remove drops the records in phase 1, Inspect "
                             "sets inspect_flag, Nothing leaves them alone.", style="Muted.TLabel",
                  wraplength=660, justify="left").pack(anchor="w")
        PresetBar(body, presets, self._apply_preset, self._current, persist).pack(anchor="w", pady=(8, 0))
        search = ttk.Frame(body)
        search.pack(fill="x", pady=(8, 4))
        ttk.Label(search, text="Search").pack(side="left")
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self._filter())
        ttk.Entry(search, textvariable=self.query).pack(side="left", fill="x", expand=True, padx=6)
        self.shown_label = ttk.Label(search, style="Muted.TLabel")
        self.shown_label.pack(side="left")
        bulk = ttk.Frame(body)
        bulk.pack(fill="x", pady=(0, 4))
        ttk.Label(bulk, text="Set all shown to").pack(side="left")
        for action in ISSUE_ACTIONS:
            ttk.Button(bulk, text=action, command=lambda a=action: self._set_shown(a)).pack(side="left", padx=(6, 0))

        self.scroll = ScrollFrame(body, app.palette)
        self.scroll.pack(fill="both", expand=True)
        for code in self.order:
            row = ttk.Frame(self.scroll.inner)
            row.columnconfigure(0, weight=1)
            ttk.Label(row, text=code).grid(row=0, column=0, sticky="w")
            ttk.Label(row, text=f"{counts[code]:,}", style="Muted.TLabel", width=9, anchor="e").grid(row=0, column=1)
            for i, action in enumerate(ISSUE_ACTIONS):
                ttk.Radiobutton(row, text=action, variable=self.vars[code], value=action).grid(
                    row=0, column=2 + i, padx=(10, 0))
            self.rows[code] = row
        self._apply_preset(DEFAULT_PRESET)
        self._filter()

        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Button(footer, text="Save filters", style="Accent.TButton", command=self._save).pack(side="right")
        ttk.Button(footer, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        _finish_dialog(self)

    def _apply_preset(self, name):
        remove, inspect = self.default if name == DEFAULT_PRESET else (
            self.presets[name]["remove"], self.presets[name]["inspect"])
        self.remove_issues, self.inspect_issues = set(remove), set(inspect)
        for code, var in self.vars.items():
            var.set("Remove" if code in self.remove_issues else "Inspect" if code in self.inspect_issues
                    else "Nothing")

    def _filter(self):
        needle = self.query.get().strip().lower()
        shown = [c for c in self.order if needle in c.lower()]
        for code, row in self.rows.items():
            row.grid_forget()
        for i, code in enumerate(shown):
            self.rows[code].grid(row=i, column=0, sticky="ew", pady=1)
        self.shown = shown
        self.shown_label.configure(text=f"{len(shown)} of {len(self.order)}")

    def _set_shown(self, action):
        for code in self.shown:
            self.vars[code].set(action)

    def _lists(self):
        remove = (self.remove_issues - set(self.counts)) | {c for c, v in self.vars.items() if v.get() == "Remove"}
        inspect = (self.inspect_issues - set(self.counts)) | {c for c, v in self.vars.items() if v.get() == "Inspect"}
        return sorted(remove), sorted(inspect)

    def _current(self):
        remove, inspect = self._lists()
        return {"remove": remove, "inspect": inspect}

    def _save(self):
        self.result = self._lists()
        self.destroy()


class ColumnPickerDialog(tk.Toplevel):
    def __init__(self, app, columns, selected, source, presets, persist):
        super().__init__(app.root)
        self.title("Master columns")
        self.geometry("560x620")
        self.minsize(420, 400)
        self.transient(app.root)
        self.configure(background=app.palette["bg"])
        theme.set_title_bar(self, app.theme_name == "dark")
        self.result = None
        self.saved = False
        self.columns = columns
        self.presets = presets
        self.selected = selected
        self.vars = {c: tk.BooleanVar(value=selected is None or c in selected or c in REQUIRED_COLUMNS)
                     for c in columns}
        self.rows = {}

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Columns found in {source}. Ticked columns stay in master_cleaned.csv; the rest move "
                             "to removed_columns.csv. gbifID, media and the measurement columns are always kept.",
                  style="Muted.TLabel", wraplength=500, justify="left").pack(anchor="w")
        PresetBar(body, presets, self._apply_preset, self._current, persist).pack(anchor="w", pady=(8, 0))
        search = ttk.Frame(body)
        search.pack(fill="x", pady=(8, 4))
        ttk.Label(search, text="Search").pack(side="left")
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self._filter())
        ttk.Entry(search, textvariable=self.query).pack(side="left", fill="x", expand=True, padx=6)
        bulk = ttk.Frame(body)
        bulk.pack(fill="x", pady=(0, 4))
        ttk.Button(bulk, text="Tick all shown", command=lambda: self._set_shown(True)).pack(side="left")
        ttk.Button(bulk, text="Untick all shown", command=lambda: self._set_shown(False)).pack(side="left", padx=6)
        self.count_label = ttk.Label(bulk, style="Muted.TLabel")
        self.count_label.pack(side="right")

        self.scroll = ScrollFrame(body, app.palette)
        self.scroll.pack(fill="both", expand=True)
        for column in columns:
            required = column in REQUIRED_COLUMNS
            check = ttk.Checkbutton(self.scroll.inner, text=column + ("  (always kept)" if required else ""),
                                    variable=self.vars[column], command=self._count,
                                    state="disabled" if required else "normal")
            self.rows[column] = check
        self._filter()

        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Button(footer, text="Save columns", style="Accent.TButton", command=self._save).pack(side="right")
        ttk.Button(footer, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        _finish_dialog(self)

    def _apply_preset(self, name):
        wanted = None if name == DEFAULT_PRESET else set(self.presets[name])
        for column, var in self.vars.items():
            var.set(wanted is None or column in wanted or column in REQUIRED_COLUMNS)
        self._count()

    def _filter(self):
        needle = self.query.get().strip().lower()
        for row in self.rows.values():
            row.grid_forget()
        self.shown = [c for c in self.columns if needle in c.lower()]
        for i, column in enumerate(self.shown):
            self.rows[column].grid(row=i, column=0, sticky="w", pady=1)
        self._count()

    def _count(self):
        self.count_label.configure(text=f"{len(self._chosen())} of {len(self.columns)} ticked")

    def _set_shown(self, value):
        for column in self.shown:
            if column not in REQUIRED_COLUMNS:
                self.vars[column].set(value)
        self._count()

    def _chosen(self):
        return [c for c in self.columns if self.vars[c].get()]

    def _current(self):
        return self._chosen()

    def _save(self):
        chosen = self._chosen()
        self.result = None if len(chosen) == len(self.columns) else chosen
        self.saved = True
        self.destroy()


class ItemPicker(tk.Toplevel):
    def __init__(self, app, title, items, action_text, redo_text):
        super().__init__(app.root)
        self.title(title)
        self.geometry("980x640")
        self.minsize(720, 460)
        self.transient(app.root)
        self.configure(background=app.palette["bg"])
        theme.set_title_bar(self, app.theme_name == "dark")
        self.app = app
        self.items = {item["id"]: item for item in items}
        self.checked = set()
        self.result = None
        self.action_text = action_text
        self.sort_column, self.sort_reverse = "id", False
        self.box_on = _check_image(self, app.palette, True)
        self.box_off = _check_image(self, app.palette, False)
        self.preview_photo = None

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        top = ttk.Frame(body)
        top.pack(fill="x")
        ttk.Label(top, text="Search").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refresh())
        search = ttk.Entry(top, textvariable=self.search_var, width=26)
        search.pack(side="left", padx=(6, 14))
        ttk.Label(top, text="Status").pack(side="left")
        self.status_var = tk.StringVar(value="All")
        statuses = ["All"] + sorted({item["status"] for item in items})
        status_box = ttk.Combobox(top, textvariable=self.status_var, values=statuses, state="readonly", width=16)
        status_box.pack(side="left", padx=6)
        status_box.bind("<<ComboboxSelected>>", lambda _: self._refresh())
        self.count_label = ttk.Label(top, style="Muted.TLabel")
        self.count_label.pack(side="right")

        middle = ttk.PanedWindow(body, orient="horizontal")
        middle.pack(fill="both", expand=True, pady=10)
        tree_frame = ttk.Frame(middle)
        self.tree = ttk.Treeview(tree_frame, columns=("status", "detail"), show="tree headings", selectmode="extended")
        for column, text, width in (("#0", "gbifID", 170), ("status", "Status", 120), ("detail", "Details", 360)):
            key = "id" if column == "#0" else column
            self.tree.heading(column, text=text, anchor="w", command=lambda k=key: self._sort(k))
            self.tree.column(column, width=width, anchor="w", stretch=column == "detail")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        middle.add(tree_frame, weight=3)

        preview = ttk.Frame(middle, padding=(12, 0, 0, 0))
        self.preview_image = ttk.Label(preview)
        self.preview_image.pack(anchor="n")
        self.preview_text = ttk.Label(preview, style="Muted.TLabel", wraplength=270, justify="left",
                                      text="Select a row to preview it. Click the box, or press Space, to check it.")
        self.preview_text.pack(anchor="w", pady=(8, 0))
        self.response_button = ttk.Button(preview, text="Open raw model response", command=self._open_response)
        middle.add(preview, weight=1)

        paste = ttk.Frame(body)
        paste.pack(fill="x")
        paste.columnconfigure(0, weight=1)
        ttk.Label(paste, text="Paste gbifIDs separated by commas, spaces or new lines").grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        self.paste_text = tk.Text(paste, height=3, wrap="word", font=app.fonts["mono"], padx=8, pady=6)
        theme.style_text(self.paste_text, app.palette)
        self.paste_text.grid(row=1, column=0, sticky="ew")
        ttk.Button(paste, text="Check these", command=self._check_pasted).grid(row=1, column=1, sticky="n", padx=(8, 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        for text, command in (("Check shown", lambda: self._set_shown(True)),
                              ("Uncheck shown", lambda: self._set_shown(False)),
                              ("Check highlighted", self._check_highlighted)):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 6))
        self.redo_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(buttons, text=redo_text, variable=self.redo_var).pack(side="left", padx=12)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        self.run_button = ttk.Button(buttons, style="Accent.TButton", command=self._run)
        self.run_button.pack(side="right", padx=6)

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda _: self._toggle(self.tree.selection()) or "break")
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._show_preview())
        self.bind("<Escape>", lambda _: self.destroy())
        self._refresh()
        search.focus_set()
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass

    def _visible(self):
        term = self.search_var.get().strip().lower()
        status = self.status_var.get()
        rows = [item for item in self.items.values()
                if (status == "All" or item["status"] == status)
                and (not term or term in f"{item['id']} {item['status']} {item['detail']}".lower())]
        key = self.sort_column
        numeric = key == "id" and all(item["id"].isdigit() for item in rows)
        rows.sort(key=lambda item: int(item["id"]) if numeric else str(item[key]).lower(), reverse=self.sort_reverse)
        return rows

    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        shown = self._visible()
        for item in shown:
            self.tree.insert("", "end", iid=item["id"], text=item["id"],
                             image=self.box_on if item["id"] in self.checked else self.box_off,
                             values=(item["status"], str(item["detail"])[:200]))
        self._update_count(len(shown))

    def _update_count(self, shown=None):
        shown = len(self.tree.get_children()) if shown is None else shown
        self.count_label.configure(text=f"{len(self.checked)} checked, showing {shown} of {len(self.items)}")
        self.run_button.configure(text=f"{self.action_text} {len(self.checked)}")

    def _sort(self, column):
        self.sort_reverse = not self.sort_reverse if self.sort_column == column else False
        self.sort_column = column
        self._refresh()

    def _toggle(self, iids):
        iids = list(iids)
        target = any(iid not in self.checked for iid in iids)
        for iid in iids:
            (self.checked.add if target else self.checked.discard)(iid)
            self.tree.item(iid, image=self.box_on if target else self.box_off)
        self._update_count()

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) == "tree" and self.tree.identify_column(event.x) == "#0":
            iid = self.tree.identify_row(event.y)
            if iid:
                self._toggle([iid])

    def _set_shown(self, checked):
        for iid in self.tree.get_children():
            (self.checked.add if checked else self.checked.discard)(iid)
            self.tree.item(iid, image=self.box_on if checked else self.box_off)
        self._update_count()

    def _check_highlighted(self):
        for iid in self.tree.selection():
            self.checked.add(iid)
            self.tree.item(iid, image=self.box_on)
        self._update_count()

    def _check_pasted(self):
        wanted = [i for i in re.split(r"[\s,;]+", self.paste_text.get("1.0", "end")) if i]
        found = [i for i in wanted if i in self.items]
        self.checked.update(found)
        self._refresh()
        missing = len(wanted) - len(found)
        note = f" {missing} not found in this list." if missing else ""
        self.preview_text.configure(text=f"Checked {len(found)} pasted gbifIDs.{note}")

    def _show_preview(self):
        selection = self.tree.selection()
        if not selection:
            return
        item = self.items[selection[0]]
        lines = [f"{item['id']}: {item['status']}"]
        if item.get("detail"):
            lines.append(str(item["detail"]))
        if item.get("notes"):
            lines.append(str(item["notes"]))
        self.preview_text.configure(text="\n\n".join(lines))
        if item.get("response"):
            self.response_button.pack(anchor="w", pady=(10, 0))
        else:
            self.response_button.pack_forget()
        self.preview_photo = None
        self.preview_image.configure(image="")
        if item.get("image"):
            try:
                from PIL import Image, ImageTk
                Image.MAX_IMAGE_PIXELS = None
                with Image.open(item["image"]) as image:
                    image.draft("RGB", (540, 720))
                    image.thumbnail((270, 360))
                    self.preview_photo = ImageTk.PhotoImage(image.convert("RGB"))
                self.preview_image.configure(image=self.preview_photo)
            except Exception:
                pass

    def _open_response(self):
        selection = self.tree.selection()
        if selection and self.items[selection[0]].get("response"):
            self.app._open(self.items[selection[0]]["response"])

    def _run(self):
        if not self.checked:
            self.bell()
            return
        self.result = (set(self.checked), self.redo_var.get())
        self.destroy()


class JoinDialog(tk.Toplevel):
    def __init__(self, app, dataset_csv, measurements_csv):
        super().__init__(app.root)
        self.title("Join measurements into the dataset")
        self.geometry("1000x700")
        self.minsize(760, 520)
        self.transient(app.root)
        self.configure(background=app.palette["bg"])
        theme.set_title_bar(self, app.theme_name == "dark")
        self.app = app
        self.dataset_csv, self.measurements_csv = dataset_csv, measurements_csv
        self.result = None
        saved = app.settings.get("join", {})

        dataset_columns = csv_columns(dataset_csv)
        measurement_columns = csv_columns(measurements_csv)
        default_added = [c for c in measurement_columns if c in RESULT_COLUMNS]
        added = [c for c in saved.get("measurement_columns", default_added) if c in measurement_columns]

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Check the columns to include from each file and the column each file is matched on. "
                             "Where a column is in both files, the measurement value is used.",
                  style="Muted.TLabel", wraplength=900, justify="left").pack(anchor="w")

        lists = ttk.Frame(body)
        lists.pack(fill="both", expand=True, pady=10)
        lists.columnconfigure((0, 1), weight=1, uniform="lists")
        lists.rowconfigure(0, weight=1)
        self.dataset_list = ColumnChecklist(lists, app, f"Dataset: {Path(dataset_csv).name}", dataset_columns,
                                            dataset_columns, saved.get("master_key", "gbifID"))
        self.dataset_list.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.measurement_list = ColumnChecklist(lists, app, f"Measurements: {Path(measurements_csv).name}",
                                                measurement_columns, added, saved.get("measurement_key", "gbifID"))
        self.measurement_list.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        for checklist in (self.dataset_list, self.measurement_list):
            checklist.key_box.bind("<<ComboboxSelected>>", lambda _: self._update_matches())

        self.match_label = ttk.Label(body, style="Heading.TLabel")
        self.match_label.pack(anchor="w")

        options = ttk.Frame(body)
        options.pack(fill="x", pady=(8, 0))
        options.columnconfigure(1, weight=1)
        self.fill_var = tk.BooleanVar(value=app.fill_blanks_var.get())
        ttk.Checkbutton(options, text="Fill empty dataset fields with details read from the voucher label "
                                      "(needs the voucher_ columns checked)",
                        variable=self.fill_var).grid(row=0, column=0, columnspan=3, sticky="w")
        self.matched_only_var = tk.BooleanVar(value=saved.get("matched_only", False))
        ttk.Checkbutton(options, text="Keep only records that have a measurement",
                        variable=self.matched_only_var).grid(row=1, column=0, columnspan=3, sticky="w")
        self.output_mode = tk.StringVar(value="update")
        ttk.Radiobutton(options, text="Update the dataset CSV (the previous version is kept as a backup)",
                        variable=self.output_mode, value="update").grid(row=2, column=0, columnspan=3, sticky="w",
                                                                        pady=(8, 0))
        ttk.Radiobutton(options, text="Save as a new file", variable=self.output_mode,
                        value="new").grid(row=3, column=0, sticky="w")
        default_output = Path(dataset_csv).with_name(Path(dataset_csv).stem + "_joined.csv")
        self.output_entry = ttk.Entry(options)
        self.output_entry.insert(0, str(default_output))
        self.output_entry.grid(row=3, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(options, text="Browse…", command=self._browse_output).grid(row=3, column=2, padx=(6, 0))

        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Button(footer, text="Join", style="Accent.TButton", command=self._join).pack(side="right")
        ttk.Button(footer, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Escape>", lambda _: self.destroy())
        self._update_matches()
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass

    def _update_matches(self):
        try:
            matched, total, measured = count_matches(self.dataset_csv, self.dataset_list.key_var.get(),
                                                     self.measurements_csv, self.measurement_list.key_var.get())
            self.match_label.configure(text=f"{matched} of {total} dataset rows match one of {measured} measured rows")
        except Exception as exc:
            self.match_label.configure(text=f"Could not compare the match columns: {exc}")

    def _browse_output(self):
        path = filedialog.asksaveasfilename(parent=self, title="Save joined CSV as", defaultextension=".csv",
                                            filetypes=CSV_TYPES)
        if path:
            _set_entry(self.output_entry, path)
            self.output_mode.set("new")

    def _join(self):
        master_columns = self.dataset_list.selected()
        measurement_columns = self.measurement_list.selected()
        if not measurement_columns:
            messagebox.showwarning("Nothing to add", "Check at least one measurement column.", parent=self)
            return
        output = None
        if self.output_mode.get() == "new":
            output = self.output_entry.get().strip()
            if not output:
                messagebox.showwarning("No file name", "Enter a file name for the new CSV.", parent=self)
                return
        self.result = {
            "master_key": self.dataset_list.key_var.get(),
            "measurement_key": self.measurement_list.key_var.get(),
            "master_columns": master_columns,
            "measurement_columns": measurement_columns,
            "fill_blanks": self.fill_var.get(),
            "matched_only": self.matched_only_var.get(),
            "output_csv": output,
        }
        self.destroy()


class SiteLoginDialog(tk.Toplevel):
    """Sites that refused image downloads, and the sign-ins saved for them."""

    def __init__(self, app):
        super().__init__(app.root)
        self.title("Site logins")
        self.geometry("700x640")
        self.minsize(520, 380)
        self.transient(app.root)
        self.configure(background=app.palette["bg"])
        theme.set_title_bar(self, app.theme_name == "dark")
        self.app = app
        self.retry_ids = None
        self.done = None
        self.signing_in = False

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        ttk.Label(body, text="Some image sites only allow downloads from a signed-in or verified browser. Sign in to "
                             "a site in the window that opens, then click Done. The app keeps the cookies and uses "
                             "them for that site's downloads.", style="Muted.TLabel", wraplength=580,
                  justify="left").grid(row=0, column=0, sticky="w")
        self.list_frame = ttk.Frame(body)
        self.list_frame.grid(row=1, column=0, sticky="nsew", pady=10)
        self.list_frame.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        other = ttk.LabelFrame(body, text="Sign in to another site")
        other.grid(row=2, column=0, sticky="ew")
        other.columnconfigure(0, weight=1)
        self.url_entry = ttk.Entry(other)
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(other, text="Open browser", command=lambda: self._start(self.url_entry.get())).grid(row=0, column=1)

        prefs = ttk.LabelFrame(body, text="When a site refuses a download")
        prefs.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        prefs.columnconfigure(1, weight=1)
        current = sites.PREFS
        labels = {v: k for k, v in sites.STRATEGIES.items()}
        self.strategy_var = tk.StringVar(value=labels.get(current["strategy"], next(iter(sites.STRATEGIES))))
        self.wait_var = tk.StringVar(value=str(int(current["challenge_wait"] // 60)))
        self.delay_var = tk.StringVar(value=str(current["browser_delay"]))
        ttk.Label(prefs, text="Method").grid(row=0, column=0, sticky="w", padx=(0, 8))
        box = ttk.Combobox(prefs, textvariable=self.strategy_var, values=list(sites.STRATEGIES), state="readonly")
        box.grid(row=0, column=1, columnspan=3, sticky="ew", pady=2)
        box.bind("<<ComboboxSelected>>", lambda _: self._save_prefs())
        ttk.Label(prefs, text="Minutes to wait for you to pass a check").grid(row=1, column=0, sticky="w", padx=(0, 8))
        wait = ttk.Spinbox(prefs, textvariable=self.wait_var, from_=1, to=30, width=5, command=self._save_prefs)
        wait.grid(row=1, column=1, sticky="w")
        ttk.Label(prefs, text="Seconds between browser downloads").grid(row=1, column=2, sticky="e", padx=(12, 8))
        delay = ttk.Spinbox(prefs, textvariable=self.delay_var, from_=0, to=60, increment=0.5, width=5,
                            command=self._save_prefs)
        delay.grid(row=1, column=3, sticky="w")
        for widget in (wait, delay):
            widget.bind("<FocusOut>", lambda _: self._save_prefs())

        self.status = ttk.Label(body, style="Muted.TLabel", wraplength=580, justify="left")
        self.status.grid(row=4, column=0, sticky="w", pady=(10, 0))
        footer = ttk.Frame(body)
        footer.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self.done_button = ttk.Button(footer, text="Done, save my sign-in", style="Accent.TButton",
                                      command=self._finish, state="disabled")
        self.done_button.pack(side="left")
        ttk.Button(footer, text="Close", command=self.destroy).pack(side="right")
        if not site_login.available():
            self._say("Browser sign-in needs Playwright. " + site_login.INSTALL_HINT, "error")
        self._render()
        _finish_dialog(self)

    def _save_prefs(self):
        try:
            wait = max(int(float(self.wait_var.get())), 1) * 60
            delay = max(float(self.delay_var.get()), 0.0)
        except ValueError:
            return
        self.app._set_site_prefs({"strategy": sites.STRATEGIES[self.strategy_var.get()],
                                  "challenge_wait": wait, "browser_delay": delay})

    def _say(self, text, kind="muted"):
        self.status.configure(text=text, foreground=self.app.palette[kind])

    def _render(self):
        for child in self.list_frame.winfo_children():
            child.destroy()
        self.mode_vars = []
        saved = sites.saved_hosts()
        refused = sites.known_sites()
        row = 0
        if refused:
            ttk.Label(self.list_frame, text="Sites that have refused access", style="Heading.TLabel").grid(
                row=row, column=0, columnspan=3, sticky="w")
            row += 1
            for host in sorted(refused):
                ids = self.app._refused_ids(host)
                signed_in = any(sites._domain_matches(host, h) or sites._domain_matches(h, host) for h in saved)
                ttk.Label(self.list_frame, text=f"{host}   {len(ids)} images waiting"
                          + ("   (signed in)" if signed_in else "")).grid(row=row, column=0, sticky="w")
                ttk.Button(self.list_frame, text="Sign in…", command=lambda h=host: self._start(h)).grid(
                    row=row, column=1, padx=6, pady=2)
                ttk.Button(self.list_frame, text=f"Retry {len(ids)}", command=lambda i=list(ids): self._retry(i),
                           state="normal" if ids else "disabled").grid(row=row, column=2, pady=2)
                row += 1
        ttk.Label(self.list_frame, text="Saved sign-ins", style="Heading.TLabel").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(8 if row else 0, 0))
        row += 1
        if not saved:
            ttk.Label(self.list_frame, text="None yet", style="Muted.TLabel").grid(row=row, column=0, sticky="w")
        for host, when in sorted(saved.items()):
            ttk.Label(self.list_frame, text=f"{host}   saved {when.replace('T', ' ')}").grid(
                row=row, column=0, sticky="w")
            ttk.Button(self.list_frame, text="Sign in again", command=lambda h=host: self._start(h)).grid(
                row=row, column=1, padx=6, pady=2)
            ttk.Button(self.list_frame, text="Forget", command=lambda h=host: self._forget(h)).grid(
                row=row, column=2, pady=2)
            mode = tk.BooleanVar(value=sites.site_mode(host) == sites.MODE_BROWSER)
            self.mode_vars.append(mode)
            ttk.Checkbutton(self.list_frame, text="Download through the browser", variable=mode,
                            command=lambda h=host, v=mode: sites.set_site_mode(
                                h, sites.MODE_BROWSER if v.get() else sites.MODE_REQUESTS)).grid(
                row=row, column=3, padx=(10, 0))
            row += 1

    def _forget(self, host):
        sites.forget_login(host)
        self._render()

    def _retry(self, ids):
        self.retry_ids = set(ids)
        self.destroy()

    def _start(self, url):
        url = url.strip()
        if not url:
            self.bell()
            return
        if self.signing_in:
            return
        if not site_login.available():
            self._say("Browser sign-in needs Playwright. " + site_login.INSTALL_HINT, "error")
            return
        self.signing_in = True
        self.done = threading.Event()
        self.done_button.configure(state="normal")
        self._say("A browser window is opening. Sign in or pass the check, then click Done here.", "busy")
        done = self.done

        def work():
            try:
                host = site_login.sign_in(url, done, self.app._log)
                result = (True, f"Saved the sign-in for {host}. You can retry its images now.")
            except site_login.BrowserMissing as exc:
                self.after(0, lambda: self._offer_browser(url, str(exc)))
                return
            except Exception as exc:
                result = (False, str(exc))
            self.after(0, lambda: self._finished(*result))

        threading.Thread(target=work, daemon=True).start()

    def _offer_browser(self, url, message):
        self.signing_in = False
        self.done_button.configure(state="disabled")
        if not self.winfo_exists():
            return
        if messagebox.askyesno("Browser needed", message + "\n\nDownload Chromium now?", parent=self):
            self._say("Downloading Chromium…", "busy")
            self.signing_in = True

            def work():
                try:
                    site_login.install_chromium(self.app._log)
                    error = None
                except Exception as exc:
                    error = str(exc)
                self.after(0, lambda: self._installed(url, error))

            threading.Thread(target=work, daemon=True).start()
        else:
            self._say(message, "error")

    def _installed(self, url, error):
        self.signing_in = False
        if not self.winfo_exists():
            return
        if error:
            self._say(error, "error")
        else:
            self._start(url)

    def _finish(self):
        if self.done:
            self.done.set()
            self._say("Saving…", "busy")

    def _finished(self, ok, message):
        self.signing_in = False
        if not self.winfo_exists():
            return
        self.done_button.configure(state="disabled")
        self._say(message, "ok" if ok else "error")
        self._render()


class AccountDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.title("GBIF account")
        self.resizable(False, False)
        self.transient(app.root)
        self.configure(background=app.palette["bg"])
        theme.set_title_bar(self, app.theme_name == "dark")
        self.app = app
        self.saved = False
        account = saved_gbif_account()
        self.had_password = account["has_password"]

        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="New GBIF downloads are requested with your GBIF account. Downloading by DOI "
                             "does not need one.", style="Muted.TLabel", wraplength=440,
                  justify="left").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self.user_var = tk.StringVar(value=account["user"])
        self.email_var = tk.StringVar(value=account["email"])
        self.password_var = tk.StringVar()
        rows = (("Username", self.user_var, None), ("Email", self.email_var, None), ("Password", self.password_var, "•"))
        for row, (label, variable, show) in enumerate(rows, start=1):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            entry = ttk.Entry(body, textvariable=variable, width=40, show=show or "")
            entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=3)
            if label == "Password":
                self.password_entry = entry
            if row == 1:
                entry.focus_set()
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="Show password", variable=self.show_var,
                        command=lambda: self.password_entry.configure(show="" if self.show_var.get() else "•")
                        ).grid(row=4, column=1, sticky="w")

        store = password_store_name()
        notes = []
        if self.had_password:
            notes.append("A password is saved. Leave the field empty to keep it.")
        notes.append(f"The password is kept in {store}." if store else
                     "No system keychain was found, so the password is kept in a file readable only by your user.")
        source = gbif_credentials()[3]
        if source == "environment variables":
            notes.append("GBIF_ environment variables are set and are used instead of the account saved here.")
        ttk.Label(body, text=" ".join(notes), style="Muted.TLabel", wraplength=440, justify="left").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        link = ttk.Label(body, text="Create a free GBIF account", style="Link.TLabel", cursor="hand2")
        link.grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))
        link.bind("<Button-1>", lambda _: webbrowser.open(GBIF_SIGNUP_URL))

        self.status = ttk.Label(body, style="Muted.TLabel", wraplength=440, justify="left")
        self.status.grid(row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))

        footer = ttk.Frame(body)
        footer.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        self.test_button = ttk.Button(footer, text="Test sign-in", command=self._test)
        self.test_button.pack(side="left")
        ttk.Button(footer, text="Remove saved account", command=self._remove).pack(side="left", padx=6)
        ttk.Button(footer, text="Save", style="Accent.TButton", command=self._save).pack(side="right")
        ttk.Button(footer, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Return>", lambda _: self._save())
        self.bind("<Escape>", lambda _: self.destroy())
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass

    def _password(self):
        return self.password_var.get() or saved_gbif_password(self.user_var.get().strip())

    def _set_status(self, text, kind="muted"):
        self.status.configure(text=text, foreground=self.app.palette[kind])

    def _test(self):
        user, password = self.user_var.get().strip(), self._password()
        if not user or not password:
            self._set_status("Enter a username and password first.", "error")
            return
        self.test_button.configure(state="disabled")
        self._set_status("Checking with GBIF…", "busy")

        def work():
            try:
                ok, message = check_gbif_login(user, password)
            except Exception as exc:
                ok, message = False, f"Could not reach GBIF: {exc}"
            self.after(0, lambda: self._test_done(ok, message))

        threading.Thread(target=work, daemon=True).start()

    def _test_done(self, ok, message):
        if self.winfo_exists():
            self.test_button.configure(state="normal")
            self._set_status(message, "ok" if ok else "error")

    def _save(self):
        user, email = self.user_var.get().strip(), self.email_var.get().strip()
        if not user or not email:
            self._set_status("Enter your GBIF username and email.", "error")
            return
        if "@" not in email:
            self._set_status("The email address does not look right.", "error")
            return
        if not self.password_var.get() and not self.had_password:
            self._set_status("Enter your GBIF password.", "error")
            return
        try:
            where = save_gbif_account(user, email, self.password_var.get() or None)
        except OSError as exc:
            self._set_status(f"Could not save the account: {exc}", "error")
            return
        self.app._log(f"GBIF account saved for {user}" + (f"; the password is kept in {where}." if where else "."))
        self.saved = True
        self.destroy()

    def _remove(self):
        if not saved_gbif_account()["user"]:
            self._set_status("No account is saved in this app.")
            return
        if messagebox.askyesno("Remove account", "Remove the saved GBIF account and password?", parent=self):
            remove_gbif_account()
            self.app._log("Saved GBIF account removed.")
            self.saved = True
            self.destroy()


class PipelineApp:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.control = TaskControl()
        self.running = False
        self.action_buttons = []
        self.text_widgets = []
        self.status_kinds = {}
        self.bad_issues = list(BAD_GEOSPATIAL_ISSUES)
        self.inspect_issues = list(INSPECTION_ISSUES)
        self.master_columns = self.settings.get("master_columns")
        self.default_issues = (list(BAD_GEOSPATIAL_ISSUES), list(INSPECTION_ISSUES))
        self.settings.setdefault("issue_presets", {})
        self.settings.setdefault("column_presets", {})
        self.progress_started = None
        self.workspace = None
        sites.configure(**self.settings.get("site_prefs", {}))
        self.restyle_hooks = []
        self.idle_callbacks = []

        get = self.settings.get
        self.theme_var = tk.StringVar(value=get("theme", "System"))
        self.dataset_var = tk.StringVar(value=str(get("dataset_csv") or ""))
        self.media_dir_var = tk.StringVar(value=get("media_dir", ""))
        self.measurements_var = tk.StringVar(value=get("measurements_csv", ""))
        self.workspace_var = tk.StringVar()
        self.csv_source = CsvSource(self)
        self.server_var = tk.StringVar(value=get("server", LMSTUDIO_URL))
        self.token_var = tk.StringVar(value=str(get("token_limit", DEFAULT_TOKEN_LIMIT)))
        self.thinking_var = tk.StringVar(value=get("thinking", "Model default"))
        self.fill_blanks_var = tk.BooleanVar(value=get("fill_blanks", False))
        self.keep_awake_var = tk.BooleanVar(value=get("keep_awake", False))
        self.awake = KeepAwake()

        root.title(f"GBIF Herbaria Data Pipeline {APP_VERSION}")
        root.geometry(f"{min(1040, root.winfo_screenwidth() - 40)}x{min(960, root.winfo_screenheight() - 110)}")
        root.minsize(800, 680)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._create_fonts()
        self._apply_theme()
        self._build_ui()
        self._restyle_widgets()
        self._load_preset()
        self._restore_workspace()
        if self.keep_awake_var.get():
            self._toggle_keep_awake()

    def _create_fonts(self):
        base = tkfont.nametofont("TkDefaultFont")
        base.configure(size=10)
        tkfont.nametofont("TkTextFont").configure(size=10)
        self.fonts = {name: base.copy() for name in ("title", "bold", "small")}
        self.fonts["title"].configure(size=17, weight="bold")
        self.fonts["bold"].configure(weight="bold")
        self.fonts["small"].configure(size=9)
        self.fonts["mono"] = tkfont.nametofont("TkFixedFont").copy()
        self.fonts["mono"].configure(size=9)

    def _apply_theme(self):
        self.theme_name = theme.resolve(self.theme_var.get())
        self.palette = theme.apply_theme(self.root, self.theme_name, self.fonts)

    def _change_theme(self, _event=None):
        self._apply_theme()
        self._restyle_widgets()
        self._save_settings()

    def _restyle_widgets(self):
        for widget in self.text_widgets:
            theme.style_text(widget, self.palette)
        self.console.tag_configure("error", foreground=self.palette["error"])
        self.console.tag_configure("ok", foreground=self.palette["ok"])
        for label, kind in self.status_kinds.items():
            label.configure(foreground=self.palette[kind])
        for hook in self.restyle_hooks:
            hook(self.palette)

    def _text(self, parent, **options):
        widget = tk.Text(parent, wrap="word", font=self.fonts["mono"], padx=8, pady=6, **options)
        self.text_widgets.append(widget)
        return widget

    def _build_menu(self):
        aqua = self.root.tk.call("tk", "windowingsystem") == "aqua"
        menubar = tk.Menu(self.root)
        if aqua:
            app_menu = tk.Menu(menubar, name="apple")
            app_menu.add_command(label=f"About {APP_NAME}", command=self._show_about)
            menubar.add_cascade(menu=app_menu)
            self.root.createcommand("tk::mac::ShowPreferences", self._open_account)
            self.root.createcommand("tk::mac::Quit", self._on_close)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="GBIF account…", command=self._open_account)
        file_menu.add_separator()
        file_menu.add_command(label="Open this download's folder", command=self._open_workspace)
        file_menu.add_command(label="Open data folder", command=lambda: self._open(DATA_DIR))
        file_menu.add_command(label="Open log file", command=lambda: self._open(LOG_FILE))
        if not aqua:
            file_menu.add_separator()
            file_menu.add_command(label="Quit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=False)
        for choice in theme.THEME_CHOICES:
            view_menu.add_radiobutton(label=f"{choice} theme", value=choice, variable=self.theme_var,
                                      command=self._change_theme)
        view_menu.add_separator()
        view_menu.add_checkbutton(label="Keep computer awake", variable=self.keep_awake_var,
                                  command=self._toggle_keep_awake,
                                  state="normal" if KeepAwake.supported() else "disabled")
        menubar.add_cascade(label="View", menu=view_menu)

        if not aqua:
            help_menu = tk.Menu(menubar, tearoff=False)
            help_menu.add_command(label="About", command=self._show_about)
            menubar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menubar)

    def _show_about(self):
        messagebox.showinfo(f"About {APP_NAME}",
                            f"{APP_NAME}\nVersion {APP_VERSION}\n\nBy Kaustubh Duddala\n\n"
                            f"Your data and settings are kept in:\n{BASE_DIR}", parent=self.root)

    def _open_account(self):
        dialog = AccountDialog(self)
        self.root.wait_window(dialog)
        if dialog.saved:
            self._refresh_account()

    def _refresh_account(self):
        user, password, email, source = gbif_credentials()
        if user and password and email:
            text = f"Logged into {user} (saved in {source})."
        else:
            text = "No GBIF account set. You can download via DOI but cannot request new downloads."
        self.account_label.configure(text=text)

    def _build_ui(self):
        self._build_menu()
        header = ttk.Frame(self.root, padding=(18, 14, 18, 8))
        header.pack(fill="x")
        titles = ttk.Frame(header)
        titles.pack(side="left")
        ttk.Label(titles, text="GBIF Herbaria Data Pipeline", style="Title.TLabel").pack(anchor="w")
        chooser = ttk.Frame(titles)
        chooser.pack(anchor="w", pady=(6, 0))
        ttk.Label(chooser, text="Download", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.workspace_box = ttk.Combobox(chooser, textvariable=self.workspace_var, state="readonly", width=46,
                                          postcommand=self._refresh_workspaces)
        self.workspace_box.pack(side="left")
        self.workspace_box.bind("<<ComboboxSelected>>", self._choose_workspace)
        ttk.Button(chooser, text="Open folder", command=self._open_workspace).pack(side="left", padx=(6, 0))
        picker = ttk.Frame(header)
        picker.pack(side="right", anchor="n")
        awake = ttk.Checkbutton(picker, text="Keep computer awake", variable=self.keep_awake_var,
                                command=self._toggle_keep_awake)
        awake.pack(side="left", padx=(0, 18))
        if not KeepAwake.supported():
            awake.configure(state="disabled")
            self.keep_awake_var.set(False)
        ttk.Label(picker, text="Theme", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        combo = ttk.Combobox(picker, textvariable=self.theme_var, values=theme.THEME_CHOICES,
                             state="readonly", width=8)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", self._change_theme)

        status = ttk.Frame(self.root, padding=(18, 6, 18, 12))
        status.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="")
        self.detail_var = tk.StringVar()
        ttk.Label(status, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")
        ttk.Label(status, textvariable=self.detail_var, style="Muted.TLabel").pack(side="left", padx=12)
        self.cancel_btn = ttk.Button(status, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="right")
        self.skip_btn = ttk.Button(status, text="Skip this item", command=self._skip, state="disabled")
        self.skip_btn.pack(side="right", padx=6)
        self.progress = ttk.Progressbar(status, length=220, mode="determinate")
        self.progress.pack(side="right", padx=6)

        panes = ttk.PanedWindow(self.root, orient="vertical")
        panes.pack(fill="both", expand=True, padx=14)
        self.notebook = ttk.Notebook(panes)
        self.notebook.add(self._build_download_tab(), text="Download")
        self.notebook.add(self._build_clean_tab(), text="Clean")
        self.notebook.add(self._build_images_tab(), text="Images")
        self.notebook.add(self._build_measure_tab(), text="Measure")
        self.modify_tab = ModifyTab(self.notebook, self, self.csv_source)
        self.analysis_tab = AnalysisTab(self.notebook, self, self.csv_source)
        self.notebook.add(self.modify_tab, text="Modify")
        self.notebook.add(self.analysis_tab, text="Analysis")
        self.notebook.bind("<<NotebookTabChanged>>", self._tab_changed)
        panes.add(self.notebook, weight=4)
        panes.add(self._build_console(panes), weight=1)

    def _build_console(self, parent):
        frame = ttk.Frame(parent, padding=(0, 10, 0, 0))
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Log", style="Heading.TLabel").pack(side="left")
        ttk.Button(bar, text="Clear", command=self._clear_console).pack(side="right")
        ttk.Button(bar, text="Open log file", command=lambda: self._open(LOG_FILE)).pack(side="right", padx=6)
        ttk.Button(bar, text="Open data folder", command=lambda: self._open(DATA_DIR)).pack(side="right")
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        self.console = self._text(body, height=9, state="disabled")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.console.yview)
        self.console.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.console.pack(side="left", fill="both", expand=True)
        return frame

    def _section(self, parent, text, row, column=0, columnspan=1, padx=0):
        frame = ttk.LabelFrame(parent, text=text)
        frame.grid(row=row, column=column, columnspan=columnspan, sticky="nsew", pady=(0, 12), padx=padx)
        frame.columnconfigure(1, weight=1)
        return frame

    def _path_choices(self, kind):
        """Full paths offered in a file or folder drop-down, the active download's first."""
        spaces = list_workspaces()
        spaces.sort(key=lambda ws: ws != self.workspace)
        if kind == "csv":
            paths = self.csv_source.choices()
            for ws in spaces:
                paths += [str(ws.master_csv), str(ws.measurements_csv)]
        elif kind == "folder":
            paths = [str(ws.media_dir) for ws in spaces]
        else:
            paths = []
            for ws in spaces:
                paths += [str(ws.raw_dir), *map(str, sorted(ws.root.glob("*.zip")))]
        return [p for p in dict.fromkeys(paths) if Path(p).exists()]

    def _field(self, parent, row, label, value="", browse=None, variable=None, openable=False, kind=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
        if kind:
            variable = variable if variable is not None else tk.StringVar(value=str(value))
            entry = PathCombo(parent, self, variable, lambda: self._path_choices(kind))
            entry.var = variable
        else:
            entry = ttk.Entry(parent, textvariable=variable) if variable is not None else ttk.Entry(parent)
        if variable is None:
            entry.insert(0, str(value))
        entry.grid(row=row, column=1, sticky="ew", pady=3)
        if browse:
            ttk.Button(parent, text="Browse…", command=lambda: self._browse(entry, browse)).grid(
                row=row, column=2, padx=(6, 0), pady=3)
        if openable:
            ttk.Button(parent, text="Open", command=lambda: self._open(
                (variable.get() if kind else entry.get()).strip())).grid(
                row=row, column=3, padx=(6, 0), pady=3)
        return entry

    def _hint(self, parent, row, text, column=0, columnspan=4):
        ttk.Label(parent, text=text, style="Muted.TLabel", wraplength=660, justify="left").grid(
            row=row, column=column, columnspan=columnspan, sticky="w", pady=(2, 4))

    def _action(self, parent, row, buttons, columnspan=4):
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, columnspan=columnspan, sticky="w", pady=(8, 0))
        for i, (text, command) in enumerate(buttons):
            button = ttk.Button(frame, text=text, command=command, style="Accent.TButton" if i == 0 else "TButton")
            button.pack(side="left", padx=(0, 6))
            self.action_buttons.append(button)
        status = ttk.Label(frame, style="Muted.TLabel")
        status.pack(side="left", padx=8)
        return status

    def _build_download_tab(self):
        tab = ttk.Frame(self.notebook, padding=16)
        tab.columnconfigure(0, weight=1)

        source = self._section(tab, "Data source", 0)
        ttk.Label(source, text="Preset*").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
        self.preset_var = tk.StringVar(value=next(iter(PRESETS)))
        combo = ttk.Combobox(source, textvariable=self.preset_var, values=list(PRESETS), state="readonly", width=24)
        combo.grid(row=0, column=1, sticky="w", pady=3)
        combo.bind("<<ComboboxSelected>>", lambda _: self._load_preset())
        self.species_entry = self._field(source, 1, "Species")
        self.doi_entry = self._field(source, 2, "DOI")
        account_row = ttk.Frame(source)
        account_row.grid(row=4, column=1, columnspan=3, sticky="w", pady=(2, 4))
        self.account_label = ttk.Label(account_row, style="Muted.TLabel", wraplength=520, justify="left")
        ttk.Button(account_row, text="Set GBIF Credentials", command=self._open_account).pack(side="left", padx=(0, 10))
        self.account_label.pack(side="left")
        self._refresh_account()
        self.download_status = self._action(source, 5, [("Download only", self._run_download)])
        self.full_status = self._action(source, 6, [("Download + clean", self._run_full)])

        return tab

    def _build_clean_tab(self):
        tab = ttk.Frame(self.notebook, padding=16)
        tab.columnconfigure((0, 1), weight=1, uniform="half")

        data = self._section(tab, "Input data", 0, columnspan=2)
        self.data_entry = self._field(data, 0, "Folder or ZIP", kind="input")
        ttk.Button(data, text="Folder…", command=lambda: self._browse(
            self.data_entry, lambda: filedialog.askdirectory(title="Select data folder"))).grid(
            row=0, column=2, padx=(6, 0))
        ttk.Button(data, text="ZIP…", command=lambda: self._browse(
            self.data_entry, lambda: filedialog.askopenfilename(
                title="Select ZIP file", filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")]))).grid(
            row=0, column=3, padx=(6, 0))
        self._hint(data, 1, f"Each download is kept in its own folder under data/. Phase 2 reads {INSPECT_NAME} and "
                            f"phase 3 reads {FULL_MASTER_NAME} from the folder shown in Download at the top.",
                   column=1, columnspan=3)

        steps = self._section(tab, "Steps", 1, padx=(0, 6))
        self.phase_vars = []
        for i, label in enumerate(("Phase 1: clean and merge", "Phase 2: review, deduplicate and finalize",
                                   "Phase 3: choose master columns")):
            var = tk.BooleanVar(value=True)
            self.phase_vars.append(var)
            ttk.Checkbutton(steps, text=label, variable=var).grid(row=i, column=0, sticky="w")

        precision = self._section(tab, "Coordinate precision", 1, column=1, padx=(6, 0))
        self.precision_var = tk.StringVar(value=PRECISION_RELAXED)
        for i, (label, value) in enumerate((("Keep all coordinates", PRECISION_NONE),
                                            ("At least 1 decimal place", PRECISION_RELAXED),
                                            ("At least 3 decimal places", PRECISION_STRICT))):
            ttk.Radiobutton(precision, text=label, variable=self.precision_var, value=value).grid(
                row=i, column=0, sticky="w")

        filters = self._section(tab, "Filters", 2, columnspan=2)
        ttk.Button(filters, text="Edit issue filters…", command=self._open_issue_dialog).grid(
            row=0, column=0, sticky="w")
        self.issue_summary = ttk.Label(filters, style="Muted.TLabel")
        self.issue_summary.grid(row=0, column=1, sticky="w", padx=12)
        ttk.Label(filters, text="Excluded taxa, one name per line").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(10, 4))
        self.taxa_text = self._text(filters, height=4)
        self.taxa_text.grid(row=2, column=0, columnspan=2, sticky="ew")

        columns = self._section(tab, "Master columns (phase 3)", 3, columnspan=2)
        ttk.Button(columns, text="Choose columns…", command=self._open_column_dialog).grid(row=0, column=0, sticky="w")
        self.column_summary = ttk.Label(columns, style="Muted.TLabel")
        self.column_summary.grid(row=0, column=1, sticky="w", padx=12)
        self._refresh_column_summary()

        self.clean_status = self._action(tab, 4, [("Run", self._run_clean)], columnspan=2)
        return tab

    def _build_images_tab(self):
        tab = ttk.Frame(self.notebook, padding=16)
        tab.columnconfigure(0, weight=1)
        files = self._section(tab, "Files", 0)
        self._field(files, 0, "Dataset CSV", variable=self.dataset_var, openable=True, kind="csv",
                    browse=lambda: filedialog.askopenfilename(title="Select dataset CSV", filetypes=CSV_TYPES))
        self._field(files, 1, "Image folder", variable=self.media_dir_var, openable=True, kind="folder",
                    browse=lambda: filedialog.askdirectory(title="Select image folder"))

        download = self._section(tab, "Download images", 1)
        self.media_status = self._action(download, 1, [("Download missing images", self._run_download_media),
                                                       ("Choose records…", self._choose_downloads),
                                                       ("Site logins…", self._open_site_logins)])
        return tab

    def _build_measure_tab(self):
        tab = ttk.Frame(self.notebook, padding=16)
        tab.columnconfigure(0, weight=1)

        files = self._section(tab, "Files", 0)
        self._field(files, 0, "Image folder", variable=self.media_dir_var, openable=True, kind="folder",
                    browse=lambda: filedialog.askdirectory(title="Select image folder"))
        self._field(files, 1, "Measurements CSV", variable=self.measurements_var, openable=True, kind="csv",
                    browse=lambda: filedialog.asksaveasfilename(title="Measurements CSV", defaultextension=".csv",
                                                                filetypes=CSV_TYPES, confirmoverwrite=False))

        model = self._section(tab, "Model", 1)
        self._field(model, 0, "LM Studio server", variable=self.server_var)
        test = ttk.Button(model, text="Test connection", command=self._test_connection)
        test.grid(row=0, column=2, columnspan=2, padx=(6, 0), sticky="w")
        self.action_buttons.append(test)
        options = ttk.Frame(model)
        options.grid(row=1, column=1, columnspan=3, sticky="w", pady=3)
        ttk.Spinbox(options, textvariable=self.token_var, from_=0, to=65536, increment=1000, width=8).pack(side="left")
        ttk.Label(options, text="token limit per image (0 for none)", style="Muted.TLabel").pack(side="left", padx=(6, 20))
        ttk.Label(options, text="Thinking").pack(side="left")
        ttk.Combobox(options, textvariable=self.thinking_var, values=list(THINKING_CHOICES), state="readonly",
                     width=14).pack(side="left", padx=6)
        ttk.Label(model, text="Options").grid(row=1, column=0, sticky="w")
        self.measure_status = self._action(model, 3, [("Measure new images", self._run_measurements),
                                                      ("Choose images…", self._choose_measurements)])

        join = self._section(tab, "Add results to the dataset", 2)
        self._field(join, 0, "Dataset CSV", variable=self.dataset_var, openable=True, kind="csv",
                    browse=lambda: filedialog.askopenfilename(title="Select dataset CSV", filetypes=CSV_TYPES))
        self.join_status = self._action(join, 2, [("Join into dataset…", self._run_join)])
        return tab

    def _workspace_labels(self):
        return {ws.title: ws for ws in list_workspaces()}

    def _refresh_workspaces(self):
        labels = self._workspace_labels()
        self.workspace_box.configure(values=list(labels))
        if self.workspace and self.workspace.root.is_dir():
            self.workspace_var.set(self.workspace.title)

    def _choose_workspace(self, _event=None):
        ws = self._workspace_labels().get(self.workspace_var.get())
        if ws:
            self._set_workspace(ws)

    def _restore_workspace(self):
        ws = workspace_by_name(self.settings.get("workspace")) or (list_workspaces() or [None])[0]
        self._refresh_workspaces()
        if ws:
            self._set_workspace(ws)
        else:
            self.workspace_var.set("No downloads yet")

    def _set_workspace(self, ws):
        """Make `ws` the active download and point the file boxes at its files."""
        if ws is None:
            return
        self.workspace = ws
        self._refresh_workspaces()
        self.workspace_var.set(ws.title)
        self.media_dir_var.set(str(ws.media_dir))
        self.measurements_var.set(str(ws.measurements_csv))
        self.dataset_var.set(str(next((p for p in (ws.master_csv, ws.full_master_csv, ws.inspect_csv)
                                       if p.is_file()), ws.master_csv)))
        if ws.raw_dir.is_dir():
            self.data_entry.var.set(str(ws.raw_dir))
        self.csv_source.select(self.dataset_var.get() if Path(self.dataset_var.get()).is_file() else "")
        self._save_settings()

    def _open_workspace(self):
        self._open(self.workspace.root if self.workspace else DATA_DIR)

    def _tab_changed(self, _event=None):
        tab = self.root.nametowidget(self.notebook.select())
        if tab in (self.modify_tab, self.analysis_tab):
            tab.on_show()

    def _browse(self, entry, ask):
        path = ask()
        if path:
            (entry.var.set if hasattr(entry, "var") else lambda p: _set_entry(entry, p))(path)

    def _open(self, path):
        try:
            open_path(path)
        except Exception as exc:
            messagebox.showinfo("Nothing to open", str(exc), parent=self.root)

    def _load_preset(self):
        preset = PRESETS[self.preset_var.get()]
        _set_entry(self.species_entry, preset["species"])
        self.precision_var.set(preset["precision"])
        _set_text(self.taxa_text, "\n".join(preset["exclude_taxa"]))
        self.bad_issues = list(preset["bad_issues"])
        self.inspect_issues = list(preset["inspect_issues"])
        self.default_issues = (list(preset["bad_issues"]), list(preset["inspect_issues"]))
        self._refresh_issue_summary()

    def _data_folder(self):
        path = self.data_entry.var.get().strip()
        if not path:
            return None
        try:
            return resolve_input(path, self._log, create=False)[0]
        except Exception as exc:
            messagebox.showerror("Input data", str(exc), parent=self.root)
            return False

    def _open_issue_dialog(self):
        folder = self._data_folder()
        if folder is False:
            return
        counts = {}
        if folder:
            try:
                counts = scan_issues(find_dwca_files(folder)[0])
            except Exception as exc:
                messagebox.showerror("Issue filters", str(exc), parent=self.root)
                return
        if not counts:
            messagebox.showinfo("Issue filters", "Select downloaded data in Input data first. This list shows only "
                                                 "the issues found in its occurrence file.", parent=self.root)
            return
        dialog = IssueFilterDialog(self, counts, self.bad_issues, self.inspect_issues, self.default_issues,
                                   self.settings["issue_presets"], self._persist_presets)
        self.root.wait_window(dialog)
        if dialog.result:
            self.bad_issues, self.inspect_issues = map(list, dialog.result)
            self._refresh_issue_summary()

    def _refresh_issue_summary(self):
        self.issue_summary.configure(
            text=f"Removing {len(self.bad_issues)} issue codes, flagging {len(self.inspect_issues)}")

    def _open_column_dialog(self):
        folder = self._data_folder()
        if folder is False:
            return
        try:
            columns, source = available_columns(self.workspace, folder)
        except Exception as exc:
            messagebox.showerror("Master columns", str(exc), parent=self.root)
            return
        if not columns:
            messagebox.showinfo("Master columns", "No columns to choose from yet. Download data or run phases 1 and 2 "
                                                  "first; the first full run keeps every column, and you can run "
                                                  "phase 3 again afterwards.", parent=self.root)
            return
        saved, selection = self._column_dialog(columns, source)
        if saved:
            self._set_master_columns(selection)

    def _column_dialog(self, columns, source):
        dialog = ColumnPickerDialog(self, columns, self.master_columns, source, self.settings["column_presets"],
                                    self._persist_presets)
        self.root.wait_window(dialog)
        return dialog.saved, dialog.result

    def _set_master_columns(self, selection):
        self.master_columns = selection
        self._refresh_column_summary()
        self._save_settings()

    def _persist_presets(self):
        save_settings(self.settings)

    def _choose_columns_blocking(self, columns):
        answer = queue.Queue()

        def show():
            saved, selection = self._column_dialog(columns, "all available data")
            if saved:
                self._set_master_columns(selection)
            answer.put(selection if saved else False)

        self.root.after(0, show)
        return answer.get()

    def _refresh_column_summary(self):
        self.column_summary.configure(
            text="Keeping all columns" if not self.master_columns else f"Keeping {len(self.master_columns)} columns")

    def _clean_options(self):
        return {
            "exclude_taxa": [line.strip() for line in self.taxa_text.get("1.0", "end").splitlines() if line.strip()],
            "coordinate_precision": self.precision_var.get(),
            "bad_geospatial_issues": list(self.bad_issues),
            "inspection_issues": list(self.inspect_issues),
        }

    def _ui(self, fn, *args):
        if threading.current_thread() is threading.main_thread():
            fn(*args)
        else:
            self.root.after(0, fn, *args)

    def _log(self, message):
        self._ui(self._append_log, str(message))

    def _append_log(self, message):
        text = message.strip().lower()
        if text.startswith(("error", "failed")) or " failed:" in text:
            tag = "error"
        elif "complete" in text:
            tag = "ok"
        else:
            tag = None
        self.console.configure(state="normal")
        self.console.insert("end", message + "\n", tag)
        self.console.see("end")
        self.console.configure(state="disabled")
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}\n")
        except OSError:
            pass

    def _clear_console(self):
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def _progress(self, done, total):
        self._ui(self._set_progress, done, total)

    def _detail(self, text):
        self._ui(self.detail_var.set, text)

    def _set_progress(self, done, total):
        if str(self.progress.cget("mode")) != "determinate":
            self.progress.stop()
            self.progress.configure(mode="determinate")
        self.progress.configure(maximum=max(total, 1), value=done)
        self.detail_var.set("")
        if self.progress_started is None or done == 0:
            self.progress_started = (time.monotonic(), done)
        if self.control.cancelled:
            return
        text = f"Working… {done} of {total}"
        started, first = self.progress_started
        if done > first and done < total:
            remaining = (time.monotonic() - started) / (done - first) * (total - done)
            text += f", about {self._duration(remaining)} left"
        self.status_var.set(text)

    @staticmethod
    def _duration(seconds):
        minutes = int(seconds // 60)
        if minutes >= 90:
            return f"{minutes // 60} h {minutes % 60} min"
        return f"{max(minutes, 1)} min"

    def _set_status(self, label, text, kind):
        self.status_kinds[label] = kind
        label.configure(text=text, foreground=self.palette[kind])

    def _set_running(self, running, cancellable=False):
        self.running = running
        for button in self.action_buttons:
            button.configure(state="disabled" if running else "normal")
        state = "normal" if running and cancellable else "disabled"
        self.cancel_btn.configure(state=state)
        self.skip_btn.configure(state=state)
        self.detail_var.set("")
        self.progress_started = None
        if running:
            self.control.reset()
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(15)
            self.status_var.set("Working…")
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.status_var.set("Ready")
            while self.idle_callbacks:
                self.root.after(0, self.idle_callbacks.pop(0))

    def _start(self, status_label, busy_text, work, cancellable=False):
        if self.running:
            return
        self._set_running(True, cancellable)
        self._set_status(status_label, busy_text, "busy")

        def runner():
            try:
                result = work()
            except Exception as exc:
                traceback.print_exc()
                message = str(exc)
                self._log(f"Error: {message}")
                self._ui(self._set_status, status_label, "Failed", "error")
                self._ui(lambda: messagebox.showerror("Error", message, parent=self.root))
            else:
                text = result or "Done"
                self._ui(self._set_status, status_label, text, "muted" if text == "Cancelled" else "ok")
            finally:
                self._ui(self._set_running, False)
                self._ui(self.root.bell)

        threading.Thread(target=runner, daemon=True).start()

    def _ask_yesno(self, title, message):
        answer = queue.Queue()
        self.root.after(0, lambda: answer.put(messagebox.askyesno(title, message, parent=self.root)))
        return answer.get()

    def _confirm_phase_2(self):
        return self._ask_yesno("Run phase 2?", f"Phase 1 is done. Review {INSPECT_NAME} now if needed "
                                               "(type Remove in the Action column to drop a record), "
                                               "then choose Yes to run phase 2.")

    def _skip(self):
        self.control.skip_event.set()
        self.detail_var.set("Skipping…")

    def _cancel(self):
        self.control.cancel_event.set()
        self._log("Cancelling…")
        self.cancel_btn.configure(state="disabled")
        self.skip_btn.configure(state="disabled")
        self.status_var.set("Cancelling…")

    def _save_settings(self):
        self.settings.update(
            theme=self.theme_var.get(), server=self.server_var.get().strip(), token_limit=self._token_limit(),
            thinking=self.thinking_var.get(), fill_blanks=self.fill_blanks_var.get(),
            dataset_csv=self.dataset_var.get().strip(), media_dir=self.media_dir_var.get().strip(),
            measurements_csv=self.measurements_var.get().strip(), keep_awake=self.keep_awake_var.get(),
            master_columns=self.master_columns, workspace=self.workspace.name if self.workspace else "",
            recent_csvs=self.csv_source.recent)
        save_settings(self.settings)

    def _toggle_keep_awake(self):
        if self.keep_awake_var.get():
            try:
                self.awake.enable()
                self._log("Keeping the computer awake while the app is open. The screen may still turn off.")
            except Exception as exc:
                self.keep_awake_var.set(False)
                messagebox.showerror("Keep awake", f"Could not keep the computer awake: {exc}", parent=self.root)
        else:
            self.awake.disable()
            self._log("The computer can sleep normally again.")
        self._save_settings()

    def _on_close(self):
        if self.running and not messagebox.askyesno("Quit", "A task is still running. Quit anyway?",
                                                    parent=self.root):
            return
        self.control.cancel_event.set()
        self._save_settings()
        self.awake.disable()
        self.root.destroy()

    def _token_limit(self):
        try:
            return max(int(self.token_var.get()), 0)
        except ValueError:
            return DEFAULT_TOKEN_LIMIT

    def _download_inputs(self):
        species, doi = self.species_entry.get().strip(), self.doi_entry.get().strip()
        if not species and not doi:
            messagebox.showwarning("Missing input", "Enter a species name or a DOI.", parent=self.root)
            return None
        return species, doi, self.preset_var.get()

    def _run_download(self):
        inputs = self._download_inputs()
        if not inputs:
            return

        def work():
            folder = download_dataset(*inputs, log=self._log)
            self._ui(self._set_workspace, workspace_of(folder))
            self._ui(self.data_entry.var.set, folder)
            return "Downloaded"

        self._start(self.download_status, "Downloading…", work)

    def _run_full(self):
        inputs = self._download_inputs()
        if not inputs:
            return
        options = self._clean_options()
        columns = self.master_columns

        def work():
            folder = download_dataset(*inputs, log=self._log)
            ws = workspace_of(folder)
            self._ui(self._set_workspace, ws)
            master = run_cleaning(ws, folder, (1, 2, 3), self._confirm_phase_2, self._log,
                                  master_columns=columns, **options)
            if not master:
                return "Stopped after phase 1"
            self._ui(self._set_workspace, ws)
            return "Workflow complete"

        self._start(self.full_status, "Running…", work)

    def _run_clean(self):
        phases = [n for n, var in enumerate(self.phase_vars, 1) if var.get()]
        path = self.data_entry.var.get().strip()
        if not phases:
            messagebox.showwarning("No steps", "Tick at least one phase to run.", parent=self.root)
            return
        if 1 in phases and not path:
            messagebox.showwarning("Missing input", "Select a data folder or ZIP file.", parent=self.root)
            return
        if 1 not in phases and not self.workspace:
            messagebox.showwarning("No download", "Choose a download at the top of the window first.",
                                   parent=self.root)
            return
        options = self._clean_options()
        workspace = self.workspace

        def work():
            if 1 in phases:
                folder, ws = resolve_input(path, self._log)
                self._ui(self._set_workspace, ws)
            else:
                folder, ws = None, workspace
            master = run_cleaning(ws, folder, phases, self._confirm_phase_2, self._log,
                                  column_chooser=self._choose_columns_blocking, **options)
            if not master:
                return "Stopped after phase 1" if 1 in phases else "Done"
            self._ui(self._set_workspace, ws)
            return "Finalized"

        self._start(self.clean_status, "Running…", work)

    def _dataset_csv(self):
        csv_path = self.dataset_var.get().strip()
        if not csv_path or not Path(csv_path).is_file():
            messagebox.showwarning("Missing CSV", "Select an existing dataset CSV first.", parent=self.root)
            return None
        return csv_path

    def _media_dir(self):
        path = self.media_dir_var.get().strip()
        if not path:
            messagebox.showwarning("Missing folder", "Choose a download at the top of the window, or select an "
                                                     "image folder.", parent=self.root)
        return path

    def _measurements_csv(self):
        path = self.measurements_var.get().strip()
        if not path:
            messagebox.showwarning("Missing file", "Choose a download at the top of the window, or select a "
                                                   "measurements CSV.", parent=self.root)
        return path

    def _pick(self, title, items, action_text, redo_text):
        if not items:
            messagebox.showinfo("Nothing to choose", "There are no items to choose from.", parent=self.root)
            return None
        dialog = ItemPicker(self, title, items, action_text, redo_text)
        self.root.wait_window(dialog)
        return dialog.result

    def _choose_downloads(self):
        csv_path = self._dataset_csv()
        if not csv_path or self.running:
            return
        media_dir = self._media_dir()
        if not media_dir:
            return
        try:
            items = media_status(csv_path, media_dir)
        except Exception as exc:
            messagebox.showerror("Could not read the dataset", str(exc), parent=self.root)
            return
        choice = self._pick("Choose records to download", items, "Download", "Download again if the image exists")
        if choice:
            self._run_download_media(*choice)

    def _set_site_prefs(self, prefs):
        self.settings["site_prefs"] = prefs
        sites.configure(**prefs)
        save_settings(self.settings)

    def _refused_ids(self, host):
        """gbifIDs in the current dataset whose last image download was refused by `host`."""
        csv_path, media_dir = self.dataset_var.get().strip(), self.media_dir_var.get().strip()
        if not csv_path or not media_dir or not Path(csv_path).is_file():
            return []
        try:
            items = media_status(csv_path, media_dir, lambda *_: None)
        except Exception:
            return []
        return [i["id"] for i in items if i["status"] == "Failed" and f"by {host}" in str(i["detail"])]

    def _open_site_logins(self):
        dialog = SiteLoginDialog(self)
        self.root.wait_window(dialog)
        if dialog.retry_ids:
            self._run_download_media(dialog.retry_ids)

    def _run_download_media(self, only_ids=None, force=False):
        csv_path = self._dataset_csv()
        if not csv_path:
            return
        media_dir = self._media_dir()
        if not media_dir:
            return
        self._save_settings()

        def work():
            r = download_media(csv_path, media_dir, self._log, self.control, self._progress, only_ids, force)
            if r["cancelled"]:
                return "Cancelled"
            if r["blocked"]:
                self.idle_callbacks.append(self._open_site_logins)
            return (f"{r['downloaded']} downloaded, {r['skipped']} already present, "
                    f"{r['skipped_by_user']} skipped, {r['failed']} failed")

        self._start(self.media_status, "Downloading…", work, cancellable=True)

    def _choose_measurements(self):
        if self.running:
            return
        media_dir, measurements_csv = self._media_dir(), self._measurements_csv()
        if not media_dir or not measurements_csv:
            return
        try:
            items = measurement_status(media_dir, measurements_csv)
        except Exception as exc:
            messagebox.showerror("Could not read measurements", str(exc), parent=self.root)
            return
        choice = self._pick("Choose images to measure", items, "Measure", "Measure again if already measured")
        if choice:
            self._run_measurements(*choice)

    def _run_measurements(self, only_ids=None, force=False):
        media_dir, measurements_csv = self._media_dir(), self._measurements_csv()
        if not media_dir or not measurements_csv:
            return
        base_url = self.server_var.get().strip() or LMSTUDIO_URL
        token_limit = self._token_limit()
        thinking = THINKING_CHOICES.get(self.thinking_var.get())
        self._save_settings()

        def work():
            r = measure_images(media_dir, measurements_csv, self._log, self.control, self._progress, self._detail,
                               base_url=base_url, token_limit=token_limit, reasoning_effort=thinking,
                               only_ids=only_ids, force=force)
            if r["cancelled"]:
                return "Cancelled"
            return (f"{r['measured']} measured, {r['skipped']} already done, "
                    f"{r['skipped_by_user']} skipped, {r['failed']} failed")

        self._start(self.measure_status, "Measuring…", work, cancellable=True)

    def _test_connection(self):
        base_url = self.server_var.get().strip() or LMSTUDIO_URL

        def work():
            message = check_connection(base_url)
            self._log(message)
            return "Connected" if message.startswith("Connected.") else "No model loaded"

        self._start(self.measure_status, "Connecting…", work)

    def _run_join(self):
        csv_path = self._dataset_csv()
        if not csv_path or self.running:
            return
        measurements_csv = self._measurements_csv()
        if not measurements_csv:
            return
        if not Path(measurements_csv).is_file():
            messagebox.showwarning("No measurements yet", f"{measurements_csv} does not exist yet. "
                                   "Measure some images first.", parent=self.root)
            return
        try:
            dialog = JoinDialog(self, csv_path, measurements_csv)
        except Exception as exc:
            messagebox.showerror("Could not read the CSV files", str(exc), parent=self.root)
            return
        self.root.wait_window(dialog)
        options = dialog.result
        if not options:
            return
        self.fill_blanks_var.set(options["fill_blanks"])
        self.settings["join"] = {k: options[k] for k in ("master_key", "measurement_key", "measurement_columns",
                                                         "matched_only")}
        self._save_settings()

        def work():
            r = join_measurements(csv_path, measurements_csv, self._log, **options)
            if not options["output_csv"]:
                self._ui(self.dataset_var.set, csv_path)
            return f"{r['matched']} of {r['total']} records matched, saved to {Path(r['output']).name}"

        self._start(self.join_status, "Joining…", work)


def launch():
    root = tk.Tk()
    PipelineApp(root)
    root.mainloop()
