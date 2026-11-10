"""Model setup: everything the optional models need, in one window.

The components are plain objects (what it is, how big it is, whether it is ready, how to get it) so the
checks work without a window; ModelSetupDialog is the window on top of them, like the Site logins
window. Nothing is installed or downloaded until a button is pressed.
"""
import importlib.metadata
import shutil
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk
from typing import Callable

from config import ADDONS_DIR, BASE_DIR, FROZEN
from . import downloads, ocr, trained
from .detectors import DETECTOR_CHOICES, LABEL_CHOICES
from ..gui_widgets import HelpTip, help_label, set_title_bar, style_text, tip

READY, MISSING, PARTIAL = "ready", "missing", "partial"
LOW_DISK_GB = 6.0                # warn before the PyTorch add-on when less than this is free


@dataclass
class Component:
    key: str
    title: str
    detail: str
    size: str
    status: Callable                 # () -> (READY | MISSING | PARTIAL, a sentence)
    install: Callable = None         # (log, cancel) -> None; None when it cannot be done here
    label: str = "Install"


def _version(package):
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "?"


def _addon_status():
    if trained.available():
        return READY, f"Installed (PyTorch {_version('torch')}, Ultralytics {_version('ultralytics')})"
    return MISSING, "Not installed"


def _ocr_status():
    if ocr.available():
        return READY, f"Installed (RapidOCR {_version('rapidocr-onnxruntime')})"
    return MISSING, "Not installed"


def _sam_status():
    path = trained.sam_weights()
    return (READY, "Downloaded") if path else (MISSING, "Not downloaded")


def _weights_status():
    have = [task for task in ("pose", "seg") if trained.base_weights(task)]
    if len(have) == 2:
        return READY, "Downloaded"
    return (PARTIAL, f"Only the {have[0]} weights are downloaded") if have else (MISSING, "Not downloaded")


def _trained_status():
    registry = trained.load_registry()
    parts = []
    for task, name in (("pose", "landmark model"), ("seg", "outline model")):
        path, version = trained.current(task)
        parts.append(f"{name}: " + (f"version {version}, trained on {registry[task].get('train_images', '?')} images"
                                    if path else "not trained yet"))
    return (READY if any(trained.current(t)[0] for t in ("pose", "seg")) else MISSING), "; ".join(parts)


def components():
    where = f" Installed into {ADDONS_DIR}." if FROZEN else ""
    return [
        Component("addon", "Training and outlines add-on (PyTorch + Ultralytics)",
                  "Runs trained models and SAM 2, and trains new ones. Not needed to measure with the vision model."
                  + where, "about 2-3 GB", _addon_status, trained.install),
        Component("ocr", "Label reader (RapidOCR, open source)",
                  "Reads printed label text on this computer, with no vision model. Handwriting is read poorly."
                  + where, "about 140 MB", _ocr_status, ocr.install),
        Component("sam", "SAM 2 outlines", "Precise outlines of leaves and seeds, used with the add-on.",
                  "about 160 MB", _sam_status, trained.download_sam, "Download"),
        Component("weights", "Starting weights for training",
                  "The pretrained models your landmark and outline models are trained from.", "about 90 MB",
                  _weights_status, trained.download_base_weights, "Download"),
        Component("trained", "Your trained models", "Trained from the sheets you review (Review tab, Train models).",
                  "", _trained_status),
    ]


def to_get(items):
    """The components that are not ready and can be installed or downloaded here."""
    return [c for c in items if c.install and c.status()[0] != READY]


def free_gb(path=BASE_DIR):
    try:
        return shutil.disk_usage(path if path.exists() else path.parent).free / 1e9
    except OSError:
        return None


def get_all(items, log=print, cancel=None, only=None):
    """Install or download the missing components (or just `only`) in turn, carrying on after a failure.

    Returns {key: None or an error text}; raises Cancelled when the person cancels.
    """
    outcome = {}
    for component in (only if only is not None else to_get(items)):
        try:
            component.install(log, cancel)
            outcome[component.key] = None
        except downloads.Cancelled:
            raise
        except Exception as exc:
            log(f"{component.title}: failed: {exc}")
            outcome[component.key] = str(exc)
    return outcome


class ModelSetupDialog(tk.Toplevel):
    """Install, download and choose what the models use."""

    def __init__(self, app):
        super().__init__(app.root)
        self.title("Model setup")
        self.geometry("780x640")
        self.minsize(640, 520)
        self.transient(app.root)
        self.configure(background=app.palette["bg"])
        set_title_bar(self, app.theme_name == "dark")
        self.app, self.cancel, self.busy = app, threading.Event(), False
        self.rows = []

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)
        self.list_frame = ttk.Frame(body)
        self.list_frame.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        self.list_frame.columnconfigure(0, weight=1)

        choices = ttk.LabelFrame(body, text="Choices")
        choices.grid(row=2, column=0, sticky="ew", pady=(6, 8))
        choices.columnconfigure(1, weight=1)
        ttk.Label(choices, text="Find structures with").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=2)
        ttk.Combobox(choices, textvariable=app.detector_var, values=list(DETECTOR_CHOICES), state="readonly",
                     width=42).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(choices, text="Read labels with").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=2)
        ttk.Combobox(choices, textvariable=app.label_var, values=list(LABEL_CHOICES), state="readonly",
                     width=42).grid(row=1, column=1, sticky="w", pady=2)
        flags = ttk.Frame(choices)
        flags.grid(row=2, column=1, sticky="w")
        tip(ttk.Checkbutton(flags, text="SAM 2 outlines", variable=app.refine_masks_var),
            "Outline leaves and seeds precisely with SAM 2 (needs the add-on and the SAM 2 download).").pack(
            side="left", padx=(0, 16))
        tip(ttk.Checkbutton(flags, text="Second-opinion checks", variable=app.second_opinion_var),
            "Ask the vision model yes or no questions about each measured object. Slower, but catches more "
            "mistakes.").pack(side="left")

        log_frame = ttk.Frame(body)
        log_frame.grid(row=3, column=0, sticky="nsew")
        self.log_box = tk.Text(log_frame, height=9, wrap="word", font=app.fonts["mono"], padx=8, pady=6,
                               state="disabled")
        style_text(self.log_box, app.palette)
        app.text_widgets.append(self.log_box)
        bar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.log_box.pack(side="left", fill="both", expand=True)

        self.disk = ttk.Label(body, style="Muted.TLabel", wraplength=740, justify="left")
        self.disk.grid(row=4, column=0, sticky="w", pady=(6, 0))
        footer = ttk.Frame(body)
        footer.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self.all_button = ttk.Button(footer, text="Get everything missing", style="Accent.TButton",
                                     command=self._get_all)
        self.all_button.pack(side="left")
        self.cancel_button = ttk.Button(footer, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=6)
        HelpTip(footer, "Everything here is optional: the app measures with a vision model (such as LM Studio) "
                        "without any of it.\n\nThe add-on and your own trained models replace the vision model "
                        "for finding structures, and the label reader replaces it for reading labels.\n\nNothing "
                        "is installed or downloaded until you press a button.").pack(side="left", padx=(4, 0))
        ttk.Button(footer, text="Close", command=self._close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _: self._close())
        self._render()
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ the list

    def _render(self):
        for child in self.list_frame.winfo_children():
            child.destroy()
        self.rows = []
        items = components()
        for row, component in enumerate(items):
            state, text = component.status()
            frame = ttk.Frame(self.list_frame)
            frame.grid(row=row, column=0, sticky="ew", pady=3)
            frame.columnconfigure(1, weight=1)
            mark = {READY: "✓", PARTIAL: "◐", MISSING: "○"}[state]
            colour = {READY: "ok", PARTIAL: "busy", MISSING: "muted"}[state]
            ttk.Label(frame, text=mark, width=2, foreground=self.app.palette[colour],
                      font=self.app.fonts["bold"]).grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 6))
            help_label(frame, component.title, component.detail, style="Heading.TLabel").grid(row=0, column=1,
                                                                                               sticky="w")
            ttk.Label(frame, text=text + (f"  ·  {component.size}" if component.size and state != READY else ""),
                      foreground=self.app.palette[colour]).grid(row=1, column=1, sticky="w")
            if component.install and state != READY:
                button = ttk.Button(frame, text=component.label, command=lambda c=component: self._run([c]))
                button.grid(row=0, column=2, rowspan=2, padx=(8, 0))
                self.rows.append(button)
        free = free_gb()
        addon_missing = trained.available() is False
        warning = (f" Less than {LOW_DISK_GB:g} GB is free, which is tight for the add-on."
                   if free is not None and free < LOW_DISK_GB and addon_missing else "")
        self.disk.configure(text=(f"Free space on this disk: {free:.1f} GB." if free is not None else "") + warning,
                            foreground=self.app.palette["error" if warning else "muted"])
        self.all_button.configure(state="normal" if to_get(items) and not self.busy else "disabled")

    # ------------------------------------------------------------------ running

    def _say(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", str(text) + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _log(self, text):
        self.app._ui(self._say, text)

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in self.rows:
            button.configure(state=state)
        self.all_button.configure(state=state if to_get(components()) else "disabled")
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def _run(self, chosen):
        if self.busy:
            return
        if self.app.running:
            messagebox.showinfo("Model setup", "Wait for the task that is running to finish first.", parent=self)
            return
        self.cancel.clear()
        self._set_busy(True)

        def work():
            try:
                outcome = get_all(chosen, self._log, self.cancel, only=chosen)
                failed = [key for key, error in outcome.items() if error]
                self._log("Done." if not failed else f"Finished, but these did not work: {', '.join(failed)}. "
                                                     "You can try them again.")
            except downloads.Cancelled:
                self._log("Cancelled. A partly downloaded file is kept and resumed next time.")
            finally:
                self.app._ui(self._finished)

        threading.Thread(target=work, daemon=True).start()

    def _get_all(self):
        self._run([c for c in components() if c.install and c.status()[0] != READY])

    def _finished(self):
        if not self.winfo_exists():
            return
        self._set_busy(False)
        self._render()
        self.app.model_setup_changed()

    def _cancel(self):
        self.cancel.set()
        self._say("Cancelling...")

    def _close(self):
        if self.busy and not messagebox.askyesno("Model setup", "A download or install is still running. Cancel it "
                                                 "and close?", parent=self):
            return
        self.cancel.set()
        self.app._save_settings()
        self.app.model_setup_changed()
        self.destroy()
