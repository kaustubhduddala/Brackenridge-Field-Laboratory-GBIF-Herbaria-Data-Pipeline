import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import program.theme as theme
from program.power import KeepAwake
from program.analysis import (RESULT_COLUMNS, check_connection, count_matches, csv_columns, join_measurements,
                      measure_images, measurement_status)
from program.config import (BAD_GEOSPATIAL_ISSUES, DATA_DIR, DEFAULT_TOKEN_LIMIT, GBIF_USER, INSPECT_CSV, INSPECTION_ISSUES,
                    LMSTUDIO_URL, LOG_FILE, MASTER_CSV, MEASUREMENTS_CSV, MEDIA_DIR, PRECISION_NONE,
                    PRECISION_RELAXED, PRECISION_STRICT, PRESETS, THINKING_CHOICES, has_gbif_credentials,
                    load_settings, save_settings)
from program.gbif import download_dataset
from program.media import download_media, media_status
from program.processor import run_cleaning
from program.utils import TaskControl, open_path, parse_issue_list, prepare_folder

CSV_TYPES = [("CSV files", "*.csv"), ("All files", "*.*")]
CHECK_MARK = [(4, 8), (5, 9), (6, 10), (7, 11), (8, 10), (9, 9), (10, 8), (11, 7), (12, 6)]


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


class IssueFilterDialog(tk.Toplevel):
    def __init__(self, app, remove_issues, inspect_issues):
        super().__init__(app.root)
        self.title("Issue filters")
        self.geometry("640x480")
        self.minsize(480, 360)
        self.transient(app.root)
        self.configure(background=app.palette["bg"])
        theme.set_title_bar(self, app.theme_name == "dark")
        self.result = None
        self.texts = []

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        notebook = ttk.Notebook(body)
        notebook.pack(fill="both", expand=True)
        tabs = (
            ("Remove records", remove_issues, BAD_GEOSPATIAL_ISSUES,
             "Records with any of these GBIF issue codes are removed in phase 1."),
            ("Flag for inspection", inspect_issues, INSPECTION_ISSUES,
             "Records with any of these GBIF issue codes get inspect_flag set to True."),
        )
        for title, current, defaults, hint in tabs:
            frame = ttk.Frame(notebook, padding=10)
            ttk.Label(frame, text=f"{hint} One code per line.", style="Muted.TLabel",
                      wraplength=560, justify="left").pack(anchor="w")
            text = tk.Text(frame, height=14, wrap="word", font=app.fonts["mono"], padx=8, pady=6)
            theme.style_text(text, app.palette)
            text.pack(fill="both", expand=True, pady=8)
            _set_text(text, "\n".join(current))
            buttons = ttk.Frame(frame)
            buttons.pack(fill="x")
            ttk.Button(buttons, text="Restore defaults",
                       command=lambda t=text, d=defaults: _set_text(t, "\n".join(d))).pack(side="left")
            ttk.Button(buttons, text="Clear", command=lambda t=text: _set_text(t, "")).pack(side="left", padx=6)
            notebook.add(frame, text=f"{title} ({len(current)})")
            self.texts.append(text)

        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Button(footer, text="Save filters", style="Accent.TButton", command=self._save).pack(side="right")
        ttk.Button(footer, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Escape>", lambda _: self.destroy())
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass

    def _save(self):
        self.result = tuple(parse_issue_list(t.get("1.0", "end")) for t in self.texts)
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


class ColumnChecklist(ttk.LabelFrame):
    def __init__(self, parent, app, title, columns, checked, key_default):
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
        self.tree = ttk.Treeview(frame, show="tree", selectmode="extended")
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
        self.progress_started = None

        get = self.settings.get
        self.theme_var = tk.StringVar(value=get("theme", "System"))
        self.dataset_var = tk.StringVar(value=str(get("dataset_csv") or (MASTER_CSV if MASTER_CSV.exists() else "")))
        self.media_dir_var = tk.StringVar(value=get("media_dir", str(MEDIA_DIR)))
        self.measurements_var = tk.StringVar(value=get("measurements_csv", str(MEASUREMENTS_CSV)))
        self.server_var = tk.StringVar(value=get("server", LMSTUDIO_URL))
        self.token_var = tk.StringVar(value=str(get("token_limit", DEFAULT_TOKEN_LIMIT)))
        self.thinking_var = tk.StringVar(value=get("thinking", "Model default"))
        self.fill_blanks_var = tk.BooleanVar(value=get("fill_blanks", False))
        self.keep_awake_var = tk.BooleanVar(value=get("keep_awake", False))
        self.awake = KeepAwake()

        root.title("GBIF Herbaria Data Pipeline")
        root.geometry("960x880")
        root.minsize(800, 680)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._create_fonts()
        self._apply_theme()
        self._build_ui()
        self._restyle_widgets()
        self._load_preset()
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

    def _text(self, parent, **options):
        widget = tk.Text(parent, wrap="word", font=self.fonts["mono"], padx=8, pady=6, **options)
        self.text_widgets.append(widget)
        return widget

    def _build_ui(self):
        header = ttk.Frame(self.root, padding=(18, 14, 18, 8))
        header.pack(fill="x")
        titles = ttk.Frame(header)
        titles.pack(side="left")
        ttk.Label(titles, text="GBIF Herbaria Data Pipeline", style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="Download, clean and measure herbarium specimen records. By Kaustubh Duddala",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
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
        self.status_var = tk.StringVar(value="Ready")
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
        self.notebook.add(self._build_download_tab(), text="1. Download")
        self.notebook.add(self._build_clean_tab(), text="2. Clean")
        self.notebook.add(self._build_images_tab(), text="3. Images")
        self.notebook.add(self._build_measure_tab(), text="4. Measure")
        panes.add(self.notebook, weight=3)
        panes.add(self._build_console(panes), weight=2)

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

    def _field(self, parent, row, label, value="", browse=None, variable=None, openable=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
        entry = ttk.Entry(parent, textvariable=variable) if variable is not None else ttk.Entry(parent)
        if variable is None:
            entry.insert(0, str(value))
        entry.grid(row=row, column=1, sticky="ew", pady=3)
        if browse:
            ttk.Button(parent, text="Browse…", command=lambda: self._browse(entry, browse)).grid(
                row=row, column=2, padx=(6, 0), pady=3)
        if openable:
            ttk.Button(parent, text="Open", command=lambda: self._open(entry.get().strip())).grid(
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
        ttk.Label(source, text="Preset").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
        self.preset_var = tk.StringVar(value=next(iter(PRESETS)))
        combo = ttk.Combobox(source, textvariable=self.preset_var, values=list(PRESETS), state="readonly", width=24)
        combo.grid(row=0, column=1, sticky="w", pady=3)
        combo.bind("<<ComboboxSelected>>", lambda _: self._load_preset())
        self.species_entry = self._field(source, 1, "Species")
        self.doi_entry = self._field(source, 2, "DOI (optional)")
        self._hint(source, 3, "Leave the DOI blank to request a new download for the species. "
                              "Paste a GBIF download DOI to fetch an existing dataset.", column=1, columnspan=3)
        if has_gbif_credentials():
            account = f"Signed in to GBIF as {GBIF_USER}."
        else:
            account = ("No GBIF credentials found. New downloads need GBIF_USER, GBIF_PASSWORD and GBIF_EMAIL "
                       "or ~/credentials.json; DOI downloads work without them.")
        self._hint(source, 4, account, column=1, columnspan=3)
        self.download_status = self._action(source, 5, [("Download dataset", self._run_download)])

        full = self._section(tab, "Full workflow", 1)
        self._hint(full, 0, "Download the dataset, run phase 1, pause so you can review the flagged CSV, "
                            "then finish with phase 2.")
        self.full_status = self._action(full, 1, [("Run full workflow", self._run_full)])
        return tab

    def _build_clean_tab(self):
        tab = ttk.Frame(self.notebook, padding=16)
        tab.columnconfigure((0, 1), weight=1, uniform="half")

        data = self._section(tab, "Input data", 0, columnspan=2)
        self.data_entry = self._field(data, 0, "Folder or ZIP")
        ttk.Button(data, text="Folder…", command=lambda: self._browse(
            self.data_entry, lambda: filedialog.askdirectory(title="Select data folder"))).grid(
            row=0, column=2, padx=(6, 0))
        ttk.Button(data, text="ZIP…", command=lambda: self._browse(
            self.data_entry, lambda: filedialog.askopenfilename(
                title="Select ZIP file", filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")]))).grid(
            row=0, column=3, padx=(6, 0))
        self._hint(data, 1, f"Not needed for phase 2 only, which reads {INSPECT_CSV.name}.", column=1, columnspan=3)

        steps = self._section(tab, "Steps", 1, padx=(0, 6))
        self.phase_var = tk.StringVar(value="both")
        for i, (label, value) in enumerate((("Phase 1, review, then phase 2", "both"),
                                            ("Phase 1 only: clean and merge", "phase1"),
                                            ("Phase 2 only: deduplicate and finalize", "phase2"))):
            ttk.Radiobutton(steps, text=label, variable=self.phase_var, value=value).grid(row=i, column=0, sticky="w")

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

        self.clean_status = self._action(tab, 3, [("Run cleaning", self._run_clean)], columnspan=2)
        return tab

    def _build_images_tab(self):
        tab = ttk.Frame(self.notebook, padding=16)
        tab.columnconfigure(0, weight=1)
        files = self._section(tab, "Files", 0)
        self._field(files, 0, "Dataset CSV", variable=self.dataset_var, openable=True,
                    browse=lambda: filedialog.askopenfilename(title="Select dataset CSV", filetypes=CSV_TYPES))
        self._field(files, 1, "Image folder", variable=self.media_dir_var, openable=True,
                    browse=lambda: filedialog.askdirectory(title="Select image folder"))

        download = self._section(tab, "Download images", 1)
        self._hint(download, 0, "Each image is saved as <gbifID>.<extension> in the image folder, and its path is "
                                "recorded in the media_path column. Records that already have an image are skipped. "
                                "Use Choose records to pick specific gbifIDs or to download an image again.")
        self.media_status = self._action(download, 1, [("Download missing images", self._run_download_media),
                                                       ("Choose records…", self._choose_downloads)])
        return tab

    def _build_measure_tab(self):
        tab = ttk.Frame(self.notebook, padding=16)
        tab.columnconfigure(0, weight=1)

        files = self._section(tab, "Files", 0)
        self._field(files, 0, "Image folder", variable=self.media_dir_var, openable=True,
                    browse=lambda: filedialog.askdirectory(title="Select image folder"))
        self._field(files, 1, "Measurements CSV", variable=self.measurements_var, openable=True,
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
        self._hint(model, 2, "Measurements use whichever vision model is loaded in LM Studio; the name of the model "
                             "that answered is saved with each result. Lower thinking or a token limit stops a "
                             "model that gets stuck reasoning.", column=1, columnspan=3)
        self.measure_status = self._action(model, 3, [("Measure new images", self._run_measurements),
                                                      ("Choose images…", self._choose_measurements)])

        join = self._section(tab, "Add results to the dataset", 2)
        self._field(join, 0, "Dataset CSV", variable=self.dataset_var, openable=True,
                    browse=lambda: filedialog.askopenfilename(title="Select dataset CSV", filetypes=CSV_TYPES))
        self._hint(join, 1, "Opens a window where you choose the columns to include from each file, the column "
                            "each file is matched on (gbifID by default), and whether to update the dataset or save "
                            "a new file. Updating keeps the previous version as <name>_before_join.csv.",
                   column=1, columnspan=3)
        self.join_status = self._action(join, 2, [("Join into dataset…", self._run_join)])
        return tab

    def _browse(self, entry, ask):
        path = ask()
        if path:
            _set_entry(entry, path)

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
        self._refresh_issue_summary()

    def _open_issue_dialog(self):
        dialog = IssueFilterDialog(self, self.bad_issues, self.inspect_issues)
        self.root.wait_window(dialog)
        if dialog.result:
            self.bad_issues, self.inspect_issues = map(list, dialog.result)
            self._refresh_issue_summary()

    def _refresh_issue_summary(self):
        self.issue_summary.configure(
            text=f"Removing {len(self.bad_issues)} issue codes, flagging {len(self.inspect_issues)}")

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
        return self._ask_yesno("Run phase 2?", f"Phase 1 is done. Review {INSPECT_CSV.name} now if needed "
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
            measurements_csv=self.measurements_var.get().strip(), keep_awake=self.keep_awake_var.get())
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
            self._ui(_set_entry, self.data_entry, folder)
            return "Downloaded"

        self._start(self.download_status, "Downloading…", work)

    def _run_full(self):
        inputs = self._download_inputs()
        if not inputs:
            return
        options = self._clean_options()

        def work():
            folder = download_dataset(*inputs, log=self._log)
            self._ui(_set_entry, self.data_entry, folder)
            master = run_cleaning(folder, "both", self._confirm_phase_2, self._log, **options)
            if not master:
                return "Stopped after phase 1"
            self._ui(self.dataset_var.set, master)
            return "Workflow complete"

        self._start(self.full_status, "Running…", work)

    def _run_clean(self):
        steps = self.phase_var.get()
        path = self.data_entry.get().strip()
        if steps != "phase2" and not path:
            messagebox.showwarning("Missing input", "Select a data folder or ZIP file.", parent=self.root)
            return
        options = self._clean_options()

        def work():
            folder = prepare_folder(path, self._log) if steps != "phase2" else None
            master = run_cleaning(folder, steps, self._confirm_phase_2, self._log, **options)
            if not master:
                return "Phase 1 done"
            self._ui(self.dataset_var.set, master)
            return "Finalized"

        self._start(self.clean_status, "Running…", work)

    def _dataset_csv(self):
        csv_path = self.dataset_var.get().strip()
        if not csv_path or not Path(csv_path).is_file():
            messagebox.showwarning("Missing CSV", "Select an existing dataset CSV first.", parent=self.root)
            return None
        return csv_path

    def _media_dir(self):
        return self.media_dir_var.get().strip() or str(MEDIA_DIR)

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
        try:
            items = media_status(csv_path, self._media_dir())
        except Exception as exc:
            messagebox.showerror("Could not read the dataset", str(exc), parent=self.root)
            return
        choice = self._pick("Choose records to download", items, "Download", "Download again if the image exists")
        if choice:
            self._run_download_media(*choice)

    def _run_download_media(self, only_ids=None, force=False):
        csv_path = self._dataset_csv()
        if not csv_path:
            return
        media_dir = self._media_dir()
        self._save_settings()

        def work():
            r = download_media(csv_path, media_dir, self._log, self.control, self._progress, only_ids, force)
            if r["cancelled"]:
                return "Cancelled"
            return (f"{r['downloaded']} downloaded, {r['skipped']} already present, "
                    f"{r['skipped_by_user']} skipped, {r['failed']} failed")

        self._start(self.media_status, "Downloading…", work, cancellable=True)

    def _choose_measurements(self):
        if self.running:
            return
        try:
            items = measurement_status(self._media_dir(), self.measurements_var.get().strip() or MEASUREMENTS_CSV)
        except Exception as exc:
            messagebox.showerror("Could not read measurements", str(exc), parent=self.root)
            return
        choice = self._pick("Choose images to measure", items, "Measure", "Measure again if already measured")
        if choice:
            self._run_measurements(*choice)

    def _run_measurements(self, only_ids=None, force=False):
        measurements_csv = self.measurements_var.get().strip() or str(MEASUREMENTS_CSV)
        media_dir = self._media_dir()
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
        measurements_csv = self.measurements_var.get().strip() or str(MEASUREMENTS_CSV)
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
