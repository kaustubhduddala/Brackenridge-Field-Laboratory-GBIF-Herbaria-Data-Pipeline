"""The Review tab: look at what was measured on a sheet, correct it, accept it.

Sheets that need a person (a trait needs review or is only a lower bound) are listed least confident
first. On the sheet every measurement is drawn with its two points; drag a point to correct it, add
or delete measurements, mark a leaf folded or a panicle cut off, or set the scale from two points a
known distance apart. Accepting a sheet stores it as reviewed, rewrites its CSV row and saves a
training label, which is how the corrections flow back into the models.
"""
import copy
import math
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from config import RESPONSES_DIR_NAME
from . import labels, trained, training
from .calibration import manual_calibration
from .decision import ACCEPT, LOWER_BOUND, NEEDS_REVIEW
from .overlay import COLOURS, entry_label
from .pipeline import blank_result, load_result, new_entry, refine_outline, remeasure, via_position
from .benchmark import load_reference
from .records import describe, is_done, load_measurements, lowest_confidence, needs_review
from .taxa import LANDMARKS, TRAIT_LABELS, TRAITS, profile_for
from ..gui_widgets import HelpTip, help_label, menu_button, tip, titled_frame, wheel_steps
from ..utils import index_images

UNTOUCHED = "Untouched sheets"
FILTERS = ("Needs review", "Not reviewed yet", "Reviewed", "All measured", UNTOUCHED)
HANDLE = 6
ZOOM_STEP = 1.25
JSON_TYPES = [("COCO JSON", "*.json"), ("All files", "*.*")]
SHEET_HELP = ("Drag a point to correct a measurement.\n\nDrag along a panicle line to pull out a new point; "
              "double-click a point to remove it.\n\nScroll to zoom, drag the sheet to move it, double-click to "
              "fit it in the window.\n\nDelete removes the selected measurement. Esc stops a tool.")
SELECTED_HELP = ("Click a measurement on the sheet or in the list to select it.\n\nFolded or overlapped: it cannot "
                 "be measured, so its value becomes NA.\nAn end is hidden: the value is only a minimum.\nLeave "
                 "out: ignore this one.\n\nFit to plant traces the selected panicle along the plant, from each "
                 "of its points to the next; press it again after moving or adding points. Straighten goes back "
                 "to straight lines between the points.\n\nRe-outline draws the outline again with SAM 2, from "
                 "the points as they are now.")
ADD_HELP = ("Pick a tool, then click two points on the sheet: the base then the tip of a panicle, the two margins "
            "of a leaf, or the two ends of a seed. The tool stays on so you can add several; press Esc or the "
            "button again to stop.\n\nScale… sets the scale from two points a known distance apart (for example "
            "0 and 10 cm on the ruler), and every value on the sheet is recomputed.")
DRAG_START_PX = 5        # a press on a line becomes "pull out a new point" once the mouse moves this far
VIA_HANDLE = 4


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


class SheetView(tk.Canvas):
    """A zoomable, pannable view of one sheet with the measurements drawn on top as editable items."""

    def __init__(self, parent, tab):
        super().__init__(parent, highlightthickness=1, cursor="tcross")
        self.tab = tab
        self.image, self.levels, self.photo = None, [], None
        self.zoom, self.origin = 1.0, [0.0, 0.0]            # screen px per image px; image point at the top-left
        self.drag, self.pending = None, None
        self.bind("<Configure>", lambda _: self.after_idle(self.redraw))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Double-Button-1>", self._double)

    def restyle(self, palette):
        self.configure(background=palette["raised"], highlightbackground=palette["border"])

    # ------------------------------------------------------------------ image

    def show(self, image):
        """Display a PIL image (or None). Smaller copies are kept so zooming out stays quick."""
        self.image, self.levels = image, []
        if image is not None:
            level = image
            self.levels.append((1.0, level))
            while max(level.size) > 2000:
                level = level.reduce(2)
                self.levels.append((self.levels[-1][0] / 2, level))
        self.fit()

    def fit(self):
        if self.image is None:
            self.delete("all")
            return
        width, height = max(self.winfo_width(), 50), max(self.winfo_height(), 50)
        self.zoom = min(width / self.image.width, height / self.image.height)
        self.origin = [-(width / self.zoom - self.image.width) / 2, -(height / self.zoom - self.image.height) / 2]
        self.redraw()

    def to_screen(self, point):
        return ((point[0] - self.origin[0]) * self.zoom, (point[1] - self.origin[1]) * self.zoom)

    def to_image(self, x, y):
        return [x / self.zoom + self.origin[0], y / self.zoom + self.origin[1]]

    def redraw(self):
        from PIL import Image, ImageTk

        self.delete("all")
        if self.image is None:
            self.create_text(self.winfo_width() / 2, self.winfo_height() / 2, fill=self.tab.app.palette["muted"],
                             text="Choose a sheet on the left")
            return
        width, height = max(self.winfo_width(), 50), max(self.winfo_height(), 50)
        scale, level = next((s, im) for s, im in reversed(self.levels) if s >= min(self.zoom, 1.0) or s == 1.0)
        x0, y0 = max(0.0, self.origin[0]), max(0.0, self.origin[1])
        x1 = min(float(self.image.width), self.origin[0] + width / self.zoom)
        y1 = min(float(self.image.height), self.origin[1] + height / self.zoom)
        if x1 > x0 and y1 > y0:
            crop = level.crop((int(x0 * scale), int(y0 * scale), max(int(x0 * scale) + 1, int(x1 * scale)),
                               max(int(y0 * scale) + 1, int(y1 * scale))))
            shown = crop.resize((max(1, int((x1 - x0) * self.zoom)), max(1, int((y1 - y0) * self.zoom))),
                                Image.Resampling.BILINEAR if self.zoom < 2 else Image.Resampling.NEAREST)
            self.photo = ImageTk.PhotoImage(shown)
            self.create_image(*self.to_screen((x0, y0)), image=self.photo, anchor="nw")
        self.tab.draw_measurements(self)
        if self.pending:
            x, y = self.to_screen(self.pending)
            self.create_oval(x - HANDLE, y - HANDLE, x + HANDLE, y + HANDLE, outline="#ffffff", width=2)

    # ------------------------------------------------------------------ mouse

    def _handle_at(self, x, y):
        for item in self.find_overlapping(x - 3, y - 3, x + 3, y + 3)[::-1]:
            tags = self.gettags(item)
            if "handle" in tags:
                return tags[1], tags[2]
            if "entry" in tags:
                return tags[1], None
        return None

    def _press(self, event):
        self.focus_set()
        if self.image is None:
            return
        hit = self._handle_at(event.x, event.y)
        if self.tab.mode and not (hit and hit[1] and not self.tab.mode_points):
            # a tool is on: the click places a point. (A point that already exists can still be dragged, unless
            # a line is half drawn, when the click is that line's second point.)
            self.tab.place_point(self.to_image(event.x, event.y))
            return
        if hit:
            self.tab.select_entry(hit[0])
            if hit[1]:
                self.drag = ("handle", hit[0], hit[1])
            else:                                       # on the line itself: a drag from here pulls out a new point
                self.drag = ("line", hit[0], event.x, event.y, self.to_image(event.x, event.y))
        else:
            self.drag = ("pan", event.x, event.y, list(self.origin))

    def _double(self, event):
        hit = self._handle_at(event.x, event.y) if self.image is not None else None
        if hit and hit[1] and hit[1].startswith("via"):
            self.drag = None
            self.tab.remove_via(hit[0], hit[1])
        else:
            self.fit()

    def _motion(self, event):
        if not self.drag:
            return
        if self.drag[0] == "line":
            _, entry_id, x, y, start = self.drag
            if abs(event.x - x) + abs(event.y - y) < DRAG_START_PX:
                return
            name = self.tab.add_via(entry_id, start)
            self.drag = ("handle", entry_id, name) if name else None
            if not self.drag:
                return
        if self.drag[0] == "pan":
            _, x, y, origin = self.drag
            self.origin = [origin[0] - (event.x - x) / self.zoom, origin[1] - (event.y - y) / self.zoom]
            self.redraw()
        else:
            self.tab.move_point(self.drag[1], self.drag[2], self.to_image(event.x, event.y), final=False)

    def _release(self, event):
        if self.drag and self.drag[0] == "handle":
            self.tab.move_point(self.drag[1], self.drag[2], self.to_image(event.x, event.y), final=True)
        self.drag = None

    def _wheel(self, event):
        if self.image is None:
            return
        steps = wheel_steps(event)
        if not steps:
            return
        factor = ZOOM_STEP if steps < 0 else 1 / ZOOM_STEP
        smallest = 0.5 * min(self.winfo_width() / self.image.width, self.winfo_height() / self.image.height)
        zoom = min(max(self.zoom * factor, smallest), 8.0)
        anchor = self.to_image(event.x, event.y)
        self.zoom = zoom
        self.origin = [anchor[0] - event.x / zoom, anchor[1] - event.y / zoom]
        self.redraw()


class ReviewTab(ttk.Frame):
    def __init__(self, notebook, app):
        super().__init__(notebook, padding=12)
        self.app = app
        self.rows, self.images, self.items = {}, {}, []
        self.gbif_id, self.result, self.raw, self.rgb, self.cache = None, None, {}, None, {}
        self.selected, self.dirty, self.mode, self.mode_points = None, False, None, []
        self.hand, self.hand_name, self.manual, self.read_error = {}, "", False, ""
        self.sam = None                                         # SAM 2, loaded the first time it is asked for
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # ---- left: the queue
        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        left.rowconfigure(2, weight=1)
        self.filter_choice = FILTERS[0]                         # the drop-down shows each with its count
        self.filter_var = tk.StringVar(value=FILTERS[0])
        box = self.filter_box = ttk.Combobox(left, textvariable=self.filter_var, values=FILTERS, state="readonly",
                                             width=22)
        box.grid(row=0, column=0, sticky="ew")
        box.bind("<<ComboboxSelected>>", lambda _: self.set_filter(FILTERS[box.current()]))
        tip(box, "Which sheets to list. Sheets that need review come least confident first; untouched sheets "
                 "were never measured, and you can measure them by hand.", delay=900)
        self.count = ttk.Label(left, style="Muted.TLabel", wraplength=210, justify="left")
        self.count.grid(row=1, column=0, sticky="w", pady=(2, 4))
        frame = ttk.Frame(left)
        frame.grid(row=2, column=0, sticky="ns")
        self.queue = ttk.Treeview(frame, columns=("conf",), show="tree headings", selectmode="browse", height=18)
        self.queue.heading("#0", text="Sheet", anchor="w")
        self.queue.heading("conf", text="Conf.", anchor="w")
        self.queue.column("#0", width=150, anchor="w")
        self.queue.column("conf", width=52, anchor="w", stretch=False)
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.queue.yview)
        self.queue.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.queue.pack(side="left", fill="y", expand=True)
        self.queue.bind("<<TreeviewSelect>>", lambda _: self._queue_selected())
        tip(ttk.Button(left, text="Refresh", command=self.refresh),
            "Read the measurements and the image folder again").grid(row=3, column=0, sticky="ew", pady=(6, 0))

        # ---- middle: the sheet
        self.view = SheetView(self, self)
        self.view.grid(row=0, column=1, sticky="nsew")
        self.view.restyle(app.palette)
        HelpTip(self.view, SHEET_HELP, on="raised").place(relx=1.0, x=-8, y=8, anchor="ne")
        app.restyle_hooks.append(lambda palette: (self.view.restyle(palette), self.view.redraw()))
        self.view.bind("<Key-Delete>", lambda _: self._delete_entry())
        self.view.bind("<Key-BackSpace>", lambda _: self._delete_entry())
        self.view.bind("<Escape>", lambda _: self._set_mode(None))
        top = self.winfo_toplevel()
        top.bind("<Escape>", self._escape, add="+")             # works whichever button has the focus
        top.bind("<Button-1>", self._click_elsewhere, add="+")

        # ---- right: the measurements on this sheet
        right = ttk.Frame(self)
        right.grid(row=0, column=2, sticky="ns", padx=(8, 0))
        right.rowconfigure(2, weight=1)
        self.title = ttk.Label(right, style="Heading.TLabel", wraplength=300, justify="left")
        self.title.grid(row=0, column=0, sticky="w")
        self.summary = ttk.Label(right, style="Muted.TLabel", wraplength=300, justify="left")
        self.summary.grid(row=1, column=0, sticky="w", pady=(2, 6))
        listing = self.listing_frame = ttk.Frame(right)
        listing.grid(row=2, column=0, sticky="nsew")
        self.objects = ttk.Treeview(listing, columns=("value", "status"), show="tree headings", selectmode="browse",
                                    height=4)
        for column, text, width in (("#0", "Measurement", 92), ("value", "Value", 80), ("status", "Status", 106)):
            self.objects.heading(column, text=text, anchor="w")
            self.objects.column(column, width=width, anchor="w", stretch=False)
        objects_bar = ttk.Scrollbar(listing, orient="vertical", command=self.objects.yview)
        self.objects.configure(yscrollcommand=objects_bar.set)
        objects_bar.pack(side="right", fill="y")
        self.objects.pack(side="left", fill="both", expand=True)
        self.objects.bind("<<TreeviewSelect>>", lambda _: self._object_selected())
        self.detail = ttk.Label(right, style="Muted.TLabel", wraplength=300, justify="left")
        self.detail.grid(row=3, column=0, sticky="nw", pady=(6, 6))

        attrs = self.attrs_frame = titled_frame(right, "Selected measurement", SELECTED_HELP, padding=(10, 4))
        attrs.grid(row=4, column=0, sticky="ew")
        attrs.columnconfigure(0, weight=1)
        self.attr_vars = {}
        for i, (key, text, hint) in enumerate((
                ("folded", "Folded or overlapped", "It cannot be measured: the value becomes NA."),
                ("cut_off", "An end is hidden", "The value is only a minimum (a lower bound)."),
                ("exclude", "Leave out", "Ignore this measurement."))):
            var = tk.BooleanVar()
            self.attr_vars[key] = var
            tip(ttk.Checkbutton(attrs, text=text, variable=var, command=self._attrs_changed), hint).grid(
                row=i, column=0, sticky="w")
        tip(ttk.Button(attrs, text="Re-outline", command=self._refine_outline),
            "Outline the selected measurement again with SAM 2, from its points as they are now"
            ).grid(row=0, column=1, rowspan=2, sticky="e", padx=(8, 0))
        tip(ttk.Button(attrs, text="Delete", command=self._delete_entry),
            "Remove the selected measurement (also the Delete key)").grid(row=2, column=1, sticky="e", padx=(8, 0))
        route = ttk.Frame(attrs)
        route.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(route, text="Panicle").pack(side="left", padx=(0, 8))
        tip(ttk.Button(route, text="Fit to plant", command=self._fit_to_plant),
            "Trace the selected panicle along the plant, through its points").pack(side="left")
        tip(ttk.Button(route, text="Straighten", command=self._straighten),
            "Back to straight lines between the points").pack(side="left", padx=(6, 0))

        tools = self.tools_frame = titled_frame(right, "Add by clicking", ADD_HELP, padding=(10, 4))
        self.tool_buttons = {}
        tools.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        ends = {"panicle_length": "base, then tip", "leaf_width": "one margin, then the other",
                "seed_length": "one end, then the other"}
        for i, trait in enumerate(TRAITS):
            self.tool_buttons[trait] = tip(ttk.Button(tools, text=TRAIT_LABELS[trait].capitalize(), width=6,
                                                      command=lambda t=trait: self._toggle_mode(t)),
                                           f"Add a {TRAIT_LABELS[trait]} with two clicks: {ends.get(trait, 'two points')}")
            self.tool_buttons[trait].grid(row=0, column=i, padx=(0, 4))
        self.tool_buttons["scale"] = tip(ttk.Button(tools, text="Scale…", width=6,
                                                    command=lambda: self._toggle_mode("scale")),
                                         "Set the scale from two points a known distance apart")
        self.tool_buttons["scale"].grid(row=0, column=3)
        self.mode_label = ttk.Label(right, style="Link.TLabel", wraplength=300, justify="left")
        self.mode_label.grid(row=6, column=0, sticky="w", pady=(6, 0))

        # ---- bottom: accepting, labels and training
        bottom = ttk.Frame(self)
        bottom.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.accept_button = tip(ttk.Button(bottom, text="Accept sheet", style="Accent.TButton",
                                            command=self._accept),
                                 "Store this sheet as reviewed: its row in the measurements CSV is rewritten and a "
                                 "training label is saved. Then the next sheet opens.")
        self.accept_button.pack(side="left")
        tip(ttk.Button(bottom, text="◀", width=3, command=lambda: self._step(-1)),
            "Previous sheet in the list").pack(side="left", padx=(6, 0))
        tip(ttk.Button(bottom, text="▶", width=3, command=lambda: self._step(1)),
            "Next sheet in the list, without accepting this one").pack(side="left", padx=(2, 6))
        tip(ttk.Button(bottom, text="Undo edits", command=self._reload_sheet),
            "Drop every change made to this sheet since it was opened or accepted").pack(side="left")
        more = menu_button(bottom, "Labels and training", [
            ("Export labels (reviewed sheets)…", lambda: self._export_coco(True)),
            ("Export pre-labels (everything found)…", lambda: self._export_coco(False)),
            ("Import corrected labels…", self._import_coco), None,
            ("Train models…", self._train)])
        tip(more, "Exchange labels with CVAT, Label Studio or Roboflow as COCO files, and train your own models on "
                  "the sheets you reviewed", delay=900)
        more.pack(side="right", padx=(6, 0))
        app.action_buttons.append(more)
        tip(ttk.Button(bottom, text="Hand values…", command=self._choose_hand),
            "Load a CSV of your own measurements (gbifID, Panicle length (cm), Leaf width (cm), Seed length (cm)). "
            "Each sheet then shows your value beside the measured one, with the difference.").pack(side="right",
                                                                                                    padx=(6, 0))
        self.status = ttk.Label(bottom, style="Muted.TLabel")
        self.status.pack(side="left", padx=10)

    # ------------------------------------------------------------------ the queue

    def _paths(self):
        csv_path, media = self.app.measurements_var.get().strip(), self.app.media_dir_var.get().strip()
        return (Path(csv_path) if csv_path else None), (Path(media) if media else None)

    def on_show(self):
        if not self.hand:
            self._load_hand()
        self.refresh()

    # ------------------------------------------------------------------ your hand measurements

    def _find_hand_csv(self):
        """The lab's hand-measurement file, when it sits in the download's folder."""
        ws = self.app.workspace
        for path in (sorted(ws.root.glob("*.csv")) if ws else []):
            if any(word in path.name.lower() for word in ("cfmeasurements", "hand_measurements", "hand-measurements")):
                return str(path)
        return ""

    def _load_hand(self, path=None):
        path = path or self.app.settings.get("hand_csv") or self._find_hand_csv()
        self.hand, self.hand_name = {}, ""
        if path and Path(path).is_file():
            try:
                self.hand, self.hand_name = load_reference(path), Path(path).name
            except Exception as exc:
                self.status.configure(text=f"Could not read the hand values: {exc}")
        if self.result is not None:
            self._refresh_panel()

    def _choose_hand(self):
        csv_path, _ = self._paths()
        path = filedialog.askopenfilename(parent=self.app.root, title="Your hand measurements (CSV with gbifID and "
                                          "Panicle length (cm), Leaf width (cm), Seed length (cm))",
                                          initialdir=str(csv_path.parent) if csv_path else None,
                                          filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self.app.settings["hand_csv"] = path
            self._load_hand(path)
            self.status.configure(text=f"{len(self.hand)} sheets with hand values from {self.hand_name}")

    def refresh(self):
        csv_path, media = self._paths()
        self.rows, self.images, self.read_error = {}, {}, ""
        if csv_path and media:
            try:
                self.rows = {k: v for k, v in load_measurements(csv_path).items() if is_done(v)}
                self.images = index_images(media)
            except Exception as exc:
                self.read_error = f"Could not read the measurements: {exc}"
        self._fill_queue()

    def set_filter(self, name):
        self.filter_choice = name
        self._fill_queue()

    def _fill_queue(self):
        measured = {gbif_id: row for gbif_id, row in self.rows.items() if gbif_id in self.images}
        reviewed = {gbif_id for gbif_id, row in measured.items() if row.get("ai_review") == "reviewed"}
        groups = {"Needs review": [i for i, row in measured.items() if needs_review(row)],
                  "Not reviewed yet": [i for i in measured if i not in reviewed], "Reviewed": list(reviewed),
                  "All measured": list(measured), UNTOUCHED: [i for i in self.images if i not in self.rows]}
        chosen = self.filter_choice
        if chosen == UNTOUCHED:
            self.items = sorted(groups[chosen])
        else:
            self.items = sorted(groups[chosen], key=lambda i: (lowest_confidence(self.rows[i]), i))
        labels = [f"{name}  ({len(groups[name])})" for name in FILTERS]
        self.filter_box.configure(values=labels)
        self.filter_var.set(labels[FILTERS.index(chosen)])
        self.queue.delete(*self.queue.get_children())
        for gbif_id in self.items:
            confidence = "-" if gbif_id not in self.rows else f"{lowest_confidence(self.rows[gbif_id]):.2f}"
            self.queue.insert("", "end", iid=gbif_id, text=gbif_id, values=(confidence,))
        if self.images or self.read_error:
            self.count.configure(text=self.read_error)
        else:
            self.count.configure(text="No images yet. Download them on the Images tab.")
        if self.gbif_id in self.items:
            self.queue.selection_set(self.gbif_id)
        elif self.gbif_id is None and self.items:
            self.queue.selection_set(self.items[0])

    def _queue_selected(self):
        selection = self.queue.selection()
        if not selection or selection[0] == self.gbif_id:
            return
        if self.dirty and not messagebox.askyesno("Unsaved edits", f"Discard the edits to sheet {self.gbif_id}?",
                                                  parent=self.app.root):
            if self.gbif_id in self.items:
                self.queue.selection_set(self.gbif_id)
            return
        self._open(selection[0])

    def _step(self, direction):
        if not self.items:
            return
        index = self.items.index(self.gbif_id) + direction if self.gbif_id in self.items else 0
        if 0 <= index < len(self.items):
            self.queue.selection_set(self.items[index])
            self.queue.see(self.items[index])

    # ------------------------------------------------------------------ one sheet

    def _open(self, gbif_id):
        from PIL import Image
        import numpy as np

        csv_path, _ = self._paths()
        record = load_result(csv_path.parent / RESPONSES_DIR_NAME, gbif_id)
        self.gbif_id, self.selected, self.dirty, self.cache = gbif_id, None, False, {}
        self._set_mode(None)
        try:
            self.app.root.configure(cursor="watch")
            self.app.root.update_idletasks()
            Image.MAX_IMAGE_PIXELS = None
            with Image.open(self.images[gbif_id]) as opened:
                image = opened.convert("RGB")
            self.rgb = np.asarray(image)
            self.manual = record is None
            if record is None:       # nothing measured yet: find the scale from the pixels, the person does the rest
                self.result, self.raw = blank_result(self.images[gbif_id], rgb=self.rgb), {}
            else:
                self.result, self.raw = record["result"], record.get("raw", {})
        except Exception as exc:
            self.result, self.rgb = None, None
            self.view.show(None)
            self.title.configure(text=gbif_id)
            self.summary.configure(text=f"Could not open this sheet: {exc}")
            self.objects.delete(*self.objects.get_children())
            return
        finally:
            self.app.root.configure(cursor="")
        self.saved = copy.deepcopy(self.result)
        self.view.show(image)
        self._refresh_panel()

    def _reload_sheet(self):
        if self.gbif_id and self.result is not None:
            self.result, self.dirty, self.selected, self.cache = copy.deepcopy(self.saved), False, None, {}
            self._set_mode(None)
            self._refresh_panel()
            self.view.redraw()

    def _entries(self):
        return [entry for trait in TRAITS for entry in self.result["traits"][trait]] if self.result else []

    def _entry(self, entry_id):
        return next((e for e in self._entries() if e["id"] == entry_id), None)

    def _refresh_panel(self):
        result = self.result
        calibration = result.get("calibration") or {}
        scale = (f"{calibration['px_per_cm']:.1f} px/cm from {calibration.get('source')}"
                 if calibration.get("px_per_cm") else "no scale: set one with Scale…")
        reviewed = " · reviewed" if result.get("review_status") == "reviewed" else ""
        untouched = " · untouched: add the measurements yourself" if self.manual and not self._entries() else ""
        self.title.configure(text=f"{self.gbif_id}{reviewed}{untouched}{' · edited' if self.dirty else ''}")
        lines = [scale]
        hand = self.hand.get(self.gbif_id) or {}
        for trait in TRAITS:
            decision = result["decisions"].get(trait) or {}
            value = (f"{decision['value_cm']:.2f} cm" if decision.get("value_cm") is not None else
                     f">= {decision['lower_bound_cm']:.2f} cm" if decision.get("lower_bound_cm") is not None else "NA")
            mine = hand.get(trait)
            versus = ""
            if mine:
                versus = f" · your hand value {mine:.2f} cm"
                if decision.get("value_cm") is not None:
                    versus += f" ({100 * (decision['value_cm'] - mine) / mine:+.0f}%)"
            lines.append(f"{TRAIT_LABELS[trait].capitalize()}: {value} ({decision.get('status', '')})" + versus
                         + (f" - {decision['note']}" if decision.get("note") and not self.manual else ""))
        self.summary.configure(text="\n".join(lines))
        self.objects.delete(*self.objects.get_children())
        for trait in TRAITS:
            for n, entry in enumerate(result["traits"][trait], start=1):
                value = "NA" if entry.get("value_cm") is None else f"{entry['value_cm']:.3f} cm"
                self.objects.insert("", "end", iid=entry["id"], text=f"{TRAIT_LABELS[trait]} {n}",
                                    values=(value, entry.get("status", "")))
        if self.selected and self.objects.exists(self.selected):
            self.objects.selection_set(self.selected)
        self._show_detail()

    def _show_detail(self):
        entry = self._entry(self.selected) if self.selected else None
        for key, var in self.attr_vars.items():
            var.set(bool(entry and (entry.get("attrs") or {}).get(key)))
        if not entry:
            self.detail.configure(text="")
            return
        lines = [entry.get("method", "")]
        if entry.get("confidence") is not None:
            lines.append(f"confidence {entry['confidence']:.2f}")
        lines += ["- " + flag["text"] for flag in entry.get("flags", [])]
        self.detail.configure(text="\n".join(line for line in lines if line))

    def _object_selected(self):
        selection = self.objects.selection()
        if selection and selection[0] != self.selected:
            self.select_entry(selection[0], centre=True)

    def select_entry(self, entry_id, centre=False):
        self.selected = entry_id
        if self.objects.exists(entry_id) and self.objects.selection() != (entry_id,):
            self.objects.selection_set(entry_id)
        entry = self._entry(entry_id)
        if centre and entry and entry.get("anchors"):
            points = list(entry["anchors"].values())
            cx, cy = sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)
            span = max(math.hypot(points[0][0] - points[-1][0], points[0][1] - points[-1][1]), 200.0)
            width, height = self.view.winfo_width(), self.view.winfo_height()
            self.view.zoom = min(max(0.5 * min(width, height) / span, self.view.zoom), 6.0)
            self.view.origin = [cx - width / self.view.zoom / 2, cy - height / self.view.zoom / 2]
        self._show_detail()
        self.view.redraw()

    # ------------------------------------------------------------------ drawing

    def draw_measurements(self, view):
        if not self.result:
            return
        calibration = self.result.get("calibration") or {}
        for box in (self.result.get("layout") or {}).get("non_plant_boxes", []):
            view.create_rectangle(*view.to_screen(box[:2]), *view.to_screen(box[2:]), outline=_hex(COLOURS["excluded"]))
        if calibration.get("ruler_line"):
            a, b = (view.to_screen(p) for p in calibration["ruler_line"])
            view.create_line(*a, *b, fill=_hex(COLOURS["ruler"]), width=3)
        for trait in TRAITS:
            for n, entry in enumerate(self.result["traits"][trait], start=1):
                usable = entry.get("status") in (ACCEPT, NEEDS_REVIEW, LOWER_BOUND)
                colour = _hex(COLOURS[trait] if usable else COLOURS["rejected"])
                chosen = entry["id"] == self.selected
                if entry.get("polygon"):
                    flat = [v for p in entry["polygon"] for v in view.to_screen(p)]
                    view.create_polygon(*flat, outline=colour, fill="", width=1, tags=("entry", entry["id"]))
                points = [view.to_screen(p) for p in entry.get("points") or []]
                if len(points) >= 2:
                    straight = trait == "panicle_length" and not entry.get("fit", True)
                    view.create_line(*[v for p in points for v in p], fill=colour, width=4 if chosen else 2,
                                     dash=(8, 5) if straight else None, tags=("entry", entry["id"]))
                for name, point in (entry.get("anchors") or {}).items():
                    x, y = view.to_screen(point)
                    view.create_oval(x - HANDLE, y - HANDLE, x + HANDLE, y + HANDLE, outline=colour,
                                     fill="#ffffff" if chosen else "", width=2, tags=("handle", entry["id"], name))
                for i, point in enumerate(entry.get("via") or []):
                    x, y = view.to_screen(point)
                    view.create_oval(x - VIA_HANDLE, y - VIA_HANDLE, x + VIA_HANDLE, y + VIA_HANDLE, outline="#000000",
                                     fill=colour, width=1, tags=("handle", entry["id"], f"via{i}"))
                if points:
                    view.create_text(points[0][0] + 10, points[0][1] - 10, text=entry_label(entry, n), anchor="w",
                                     fill=colour, font=self.app.fonts["bold"])

    # ------------------------------------------------------------------ editing

    def _measure_again(self):
        try:
            self.app.root.configure(cursor="watch")
            self.app.root.update_idletasks()
            remeasure(self.result, rgb=self.rgb, profile=profile_for(self.result.get("profile")), cache=self.cache)
        finally:
            self.app.root.configure(cursor="")
        self.dirty = True
        self._refresh_panel()
        self.view.redraw()

    def move_point(self, entry_id, name, point, final):
        entry = self._entry(entry_id)
        if not entry or self.rgb is None:
            return
        height, width = self.rgb.shape[:2]
        spot = [round(min(max(point[0], 0), width - 1), 1), round(min(max(point[1], 0), height - 1), 1)]
        if name.startswith("via"):
            index = int(name[3:])
            if index >= len(entry.get("via") or []):
                return
            entry["via"][index] = spot
        else:
            entry["anchors"][name] = spot
        entry["points"] = self._chain(entry)
        entry["polygon"], entry["edited"], entry["reviewed"] = None, True, True
        if final:
            self._measure_again()
        else:
            self.view.redraw()

    @staticmethod
    def _chain(entry):
        """The points of a line in order, as straight stretches (until the route is traced again)."""
        names = LANDMARKS[entry["trait"]]
        return [entry["anchors"][names[0]], *(entry.get("via") or []), entry["anchors"][names[1]]]

    def add_via(self, entry_id, point, measure=False):
        """Add a point to a panicle line, between the two points the click is nearest to. Returns its name."""
        entry = self._entry(entry_id)
        if not entry or entry["trait"] != "panicle_length" or not all((entry.get("anchors") or {}).get(n)
                                                                      for n in ("base", "tip")):
            return None
        index = via_position(entry, point)
        entry.setdefault("via", []).insert(index, [round(point[0], 1), round(point[1], 1)])
        entry["points"] = self._chain(entry)
        entry["polygon"], entry["edited"], entry["reviewed"] = None, True, True
        if measure:
            self._measure_again()
        return f"via{index}"

    def remove_via(self, entry_id, name):
        entry = self._entry(entry_id)
        index = int(name[3:])
        if entry and index < len(entry.get("via") or []):
            entry["via"].pop(index)
            entry["points"] = self._chain(entry)
            entry["polygon"], entry["edited"], entry["reviewed"] = None, True, True
            self._measure_again()

    def _attrs_changed(self):
        entry = self._entry(self.selected) if self.selected else None
        if not entry:
            return
        entry["attrs"] = {key: var.get() for key, var in self.attr_vars.items() if var.get()}
        entry["reviewed"] = True
        self._measure_again()

    def _selected_panicle(self):
        entry = self._entry(self.selected) if self.selected else None
        if not entry or entry["trait"] != "panicle_length" or not (entry.get("anchors") or {}).get("tip"):
            messagebox.showinfo("Panicle route", "Select a panicle line first (click it on the sheet or in the list).",
                                parent=self.app.root)
            return None
        return entry

    def _set_route(self, fit):
        entry = self._selected_panicle()
        if not entry:
            return
        before = entry.get("value_cm")
        entry["fit"], entry["edited"], entry["reviewed"] = fit, True, True
        self._measure_again()
        entry = self._entry(entry["id"]) or entry
        after, method = entry.get("value_cm"), entry.get("method", "")
        change = f"{before:.2f} → {after:.2f} cm" if before is not None and after is not None else ""
        if not fit:
            message = f"Straightened: {after:.2f} cm." if after is not None else "Straightened."
        elif method.startswith("straight"):
            why = next((n["reason"] for n in entry.get("fit_notes") or [] if n.get("reason")), "")
            message = ("The plant could not be followed between these points, so the line stays straight"
                       + (f": {why}." if why else ".") + " Add points along the panicle and fit again.")
        elif "stretches straight" in method:
            why = next((n["reason"] for n in entry.get("fit_notes") or [] if n.get("reason")), "")
            message = (f"Fitted, but part of the line could not follow the plant and stays straight ({change})"
                       + (f": {why}." if why else "."))
        elif before is not None and after is not None and abs(after - before) < 0.05:
            message = f"Fitted: the line already followed the plant ({after:.2f} cm)."
        else:
            message = f"Fitted to the plant: {change}."
        self.status.configure(text=message)

    def _load_sam(self):
        """SAM 2, loaded once; None (with the reason shown) when the add-on or its weights are missing."""
        if self.sam is not None:
            return self.sam
        if not trained.available():
            messagebox.showinfo("SAM 2", "The model add-on is not installed. Install it in Model setup first.",
                                parent=self.app.root)
            return None
        if trained.sam_weights() is None:
            messagebox.showinfo("SAM 2", "The SAM 2 weights are not downloaded yet. Download them in Model setup "
                                "(about 160 MB), then try again.", parent=self.app.root)
            return None
        try:
            self.status.configure(text="Loading SAM 2...")
            self.app.root.update_idletasks()
            self.sam = trained.SamRefiner()
        except Exception as exc:
            messagebox.showerror("SAM 2", f"SAM 2 could not be loaded: {exc}", parent=self.app.root)
        return self.sam

    def _refine_outline(self):
        """Outline the selected measurement again with SAM 2, prompted by the points as they are now.

        A panicle that is fitted to the plant is then traced again inside the new outline, which keeps the route on
        its own panicle and off a neighbouring stem.
        """
        entry = self._entry(self.selected) if self.selected else None
        if not entry or self.rgb is None:
            messagebox.showinfo("SAM 2", "Select a measurement first (click it on the sheet or in the list).",
                                parent=self.app.root)
            return
        try:
            self.app.root.configure(cursor="watch")
            sam = self._load_sam()
            if sam is None:
                self.status.configure(text="")
                return
            self.status.configure(text="Outlining with SAM 2...")
            self.app.root.update_idletasks()
            polygon, note = refine_outline(self.result, self.rgb, entry, sam)
        except Exception as exc:
            polygon, note = None, f"SAM 2 failed: {exc}"
        finally:
            self.app.root.configure(cursor="")
        if not polygon:
            self.status.configure(text=f"No new outline: {note}.")
            return
        entry["polygon"], entry["reviewed"] = polygon, True
        if entry["trait"] == "panicle_length" and entry.get("fit", True):
            entry["edited"] = True                      # trace the route again, now inside the outline
            self._measure_again()
        else:
            self.dirty = True
            self._refresh_panel()
            self.view.redraw()
        self.status.configure(text=f"SAM 2: {note}.")

    def _fit_to_plant(self):
        """Trace the selected panicle along the plant, from each of its points to the next. Press it again after
        adding or moving points to fit again."""
        self._set_route(True)

    def _straighten(self):
        """Back to straight lines between the points."""
        self._set_route(False)

    def _delete_entry(self):
        entry = self._entry(self.selected) if self.selected else None
        if not entry:
            return
        self.result["traits"][entry["trait"]].remove(entry)
        self.selected = None
        self._measure_again()

    def _toggle_mode(self, mode):
        """A tool button: switch that tool on, or off when it is already the one that is on."""
        self._set_mode(None if self.mode == mode else mode)

    def _escape(self, _event=None):
        if self.mode:
            self._set_mode(None)

    def _click_elsewhere(self, event):
        """A click on something unrelated to drawing (the sheet list, the other tabs, the bottom buttons...) ends the
        tool. The sheet, the tool buttons, the measurement list and the Selected measurement box belong to it."""
        if not self.mode:
            return
        where = str(event.widget)
        for part in (self.view, self.tools_frame, self.attrs_frame, self.listing_frame):
            if where == str(part) or where.startswith(str(part) + "."):
                return
        self._set_mode(None)

    def _set_mode(self, mode):
        if mode and self.result is None:
            return
        self.mode, self.mode_points, self.view.pending = mode, [], None
        for tool, button in self.tool_buttons.items():          # the tool that is on stands out
            button.configure(style="Accent.TButton" if tool == mode else "TButton")
        stop = "Esc, or the button again, stops."
        if mode == "scale":
            text = "Click the two ends of a known length (for example 0 and 10 cm on the ruler). Esc cancels."
        elif mode:
            first, second = LANDMARKS[mode]
            names = {"a": "one margin", "b": "the opposite margin"}
            text = (f"Click the {names.get(first, first)}, then the {names.get(second, second)} of the "
                    f"{TRAIT_LABELS[mode]}, and keep going to add more. {stop}"
                    + (" Each starts as a straight line: drag along it to add points, and use Fit to plant to trace "
                       "it along the plant." if mode == "panicle_length" else ""))
            self.view.focus_set()
        else:
            text = ""
        self.mode_label.configure(text=text)
        self.view.redraw()

    def place_point(self, point):
        self.mode_points.append([round(point[0], 1), round(point[1], 1)])
        self.view.pending = self.mode_points[-1]
        if len(self.mode_points) < 2:
            self.view.redraw()
            return
        mode, (first, second) = self.mode, self.mode_points
        self.mode_points, self.view.pending = [], None          # the tool stays on for the next line
        if mode == "scale":
            self._set_mode(None)                                # a scale is set once
            length = simpledialog.askfloat("Set scale", "Distance between the two points, in cm:", minvalue=0.1,
                                           parent=self.app.root)
            if length:
                try:
                    self.result["calibration"] = manual_calibration(self.result.get("calibration"), first, second,
                                                                    length).as_dict()
                except ValueError as exc:
                    messagebox.showwarning("Set scale", str(exc), parent=self.app.root)
                    return
                self.cache = {}
                self._measure_again()
            return
        entries = self.result["traits"][mode]
        number = 1 + max([int(e["id"].rsplit("-", 1)[-1]) for e in entries if e["id"].rsplit("-", 1)[-1].isdigit()],
                         default=0)
        entry = new_entry(mode, {"source": "review"}, number)
        names = LANDMARKS[mode]
        entry.update(anchors={names[0]: first, names[1]: second}, points=[first, second], edited=True, reviewed=True)
        if mode == "panicle_length":
            entry["fit"] = False                            # straight until "Fit to plant"
        entries.append(entry)
        self.selected = entry["id"]
        self._measure_again()                                   # redraws, with the tool still on

    # ------------------------------------------------------------------ accepting

    def _accept(self):
        if self.result is None or self.app.running:
            return
        csv_path, _ = self._paths()
        if self.manual and not self._entries():
            if not messagebox.askyesno("Nothing measured", "No measurements were added to this sheet. Accept it as "
                                       "having nothing measurable?", parent=self.app.root):
                return
        if not (self.result.get("calibration") or {}).get("px_per_cm"):
            if not messagebox.askyesno("No scale", "This sheet has no scale, so nothing on it has a value. Accept it "
                                                   "as not measurable?", parent=self.app.root):
                return
        try:
            self.app.root.configure(cursor="watch")
            self.app.root.update_idletasks()
            row = labels.save_review(csv_path, self.gbif_id, self.images[self.gbif_id], self.result, rgb=self.rgb,
                                     profile=profile_for(self.result.get("profile")), raw=self.raw)
        except Exception as exc:
            messagebox.showerror("Could not save the review", str(exc), parent=self.app.root)
            return
        finally:
            self.app.root.configure(cursor="")
        self.app._log(f"Reviewed {self.gbif_id}: {describe(row)}")
        self.app.csv_source.forget(csv_path)
        self.rows[self.gbif_id] = row
        self.dirty, self.saved = False, copy.deepcopy(self.result)
        position = self.items.index(self.gbif_id) if self.gbif_id in self.items else 0
        accepted = self.gbif_id
        self._fill_queue()
        following = [i for i in self.items if i != accepted]
        if following:
            target = following[min(position, len(following) - 1)]
            self.queue.selection_set(target)
            self.queue.see(target)
        else:
            self._refresh_panel()
            self.view.redraw()

    def has_unsaved_changes(self):
        return bool(self.dirty and self.result is not None)

    # ------------------------------------------------------------------ labels and training

    def _need_csv(self):
        csv_path, media = self._paths()
        if not csv_path or not media:
            messagebox.showwarning("No download", "Choose a download at the top of the window first.",
                                   parent=self.app.root)
            return None, None
        return csv_path, media

    def _export_coco(self, reviewed_only):
        csv_path, _ = self._need_csv()
        if not csv_path or self.app.running:
            return
        name = "labels_coco.json" if reviewed_only else "prelabels_coco.json"
        output = filedialog.asksaveasfilename(parent=self.app.root, title="Export labels (COCO)", initialfile=name,
                                              initialdir=str(csv_path.parent), defaultextension=".json",
                                              filetypes=JSON_TYPES)
        if not output:
            return

        def work():
            _, images, annotations = labels.export_coco(csv_path, output, reviewed_only, self.app._log)
            return f"{images} sheets, {annotations} objects exported"

        self.app._start(self.status, "Exporting…", work)

    def _import_coco(self):
        csv_path, media = self._need_csv()
        if not csv_path or self.app.running:
            return
        source = filedialog.askopenfilename(parent=self.app.root, title="Import corrected labels (COCO)",
                                            initialdir=str(csv_path.parent), filetypes=JSON_TYPES)
        if not source:
            return

        def work():
            count = labels.import_coco(source, csv_path, media, self.app._log)
            self.app._ui(self.refresh)
            return f"{count} sheets imported"

        self.app._start(self.status, "Importing…", work)

    def _train(self):
        if self.app.running:
            return
        if not trained.available():
            if messagebox.askyesno("Model add-on needed", "Training needs the optional PyTorch add-on, which is not "
                                   "installed. Open Model setup to install it?", parent=self.app.root):
                self.app.open_model_setup()
            return
        labelled = training.gather_labels()
        dialog = TrainDialog(self.app, len(labelled))
        self.app.root.wait_window(dialog)
        if not dialog.result:
            return
        tasks, epochs, imgsz = dialog.result

        def work():
            for task in tasks:
                training.train(task, labelled, epochs=epochs, imgsz=imgsz, log=self.app._log)
            return "Training complete"

        self.app._start(self.status, "Training…", work)


class TrainDialog(tk.Toplevel):
    def __init__(self, app, sheets):
        super().__init__(app.root)
        self.title("Train models")
        self.transient(app.root)
        self.configure(background=app.palette["bg"], padx=16, pady=14)
        self.resizable(False, False)
        self.result = None
        enough = sheets >= training.MIN_SHEETS
        help_label(self, f"{sheets} reviewed sheets with labels" + ("" if enough else
                                                                    f" (at least {training.MIN_SHEETS} are needed)"),
                   "Training starts from pretrained weights and can take from minutes to hours.\n\nThe new model "
                   "is used for measuring as soon as it is done. Check it afterwards with the benchmark on the "
                   "held-out sheets (python main.py benchmark).", style="Heading.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.pose = tk.BooleanVar(value=True)
        self.seg = tk.BooleanVar(value=False)
        tip(ttk.Checkbutton(self, text="Landmark model", variable=self.pose),
            "Finds the points: panicle base and tip, leaf margins, seed ends (a pose model).").grid(
            row=1, column=0, columnspan=2, sticky="w")
        tip(ttk.Checkbutton(self, text="Outline model", variable=self.seg),
            "Finds the outlines (a segmentation model). It needs labels that have outlines, from SAM 2 or "
            "CVAT.").grid(row=2, column=0, columnspan=2, sticky="w")
        self.epochs = tk.StringVar(value=str(training.DEFAULTS["epochs"]))
        self.imgsz = tk.StringVar(value=str(training.DEFAULTS["imgsz"]))
        for row, (text, var, hint) in enumerate((
                ("Epochs", self.epochs, "How many passes over the sheets. More takes longer and fits closer."),
                ("Image size (px)", self.imgsz, "The size the sheets are trained at, at least 320. Larger sees "
                                                "small seeds better but is slower and needs more memory.")),
                start=3):
            help_label(self, text, hint).grid(row=row, column=0, sticky="w", pady=(8, 0), padx=(0, 12))
            ttk.Entry(self, textvariable=var, width=10).grid(row=row, column=1, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(self)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Train", style="Accent.TButton", command=self._train).pack(side="right", padx=6)
        self.grab_set()

    def _train(self):
        tasks = [task for task, var in (("pose", self.pose), ("seg", self.seg)) if var.get()]
        try:
            epochs, imgsz = int(self.epochs.get()), int(self.imgsz.get())
        except ValueError:
            messagebox.showwarning("Train models", "Epochs and image size must be whole numbers.", parent=self)
            return
        if not tasks or epochs < 1 or imgsz < 320:
            messagebox.showwarning("Train models", "Tick at least one model; use at least 1 epoch and 320 px.",
                                   parent=self)
            return
        self.result = (tasks, epochs, imgsz)
        self.destroy()
