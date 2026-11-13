"""File > ML settings: how training runs (device, epochs, image size, batch), with a button that picks values
suited to this computer and the labelled sheets. Saved in settings.json under "training"; training.train() reads
them, and anything passed to it (the Train dialog, the command line) wins over them."""
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from config import save_settings
from . import training
from ..gui_widgets import help_label, set_title_bar


class MLSettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("ML settings")
        self.transient(app.root)
        self.configure(background=app.palette["bg"], padx=16, pady=14)
        self.resizable(False, False)
        set_title_bar(self, app.theme_name == "dark")
        saved = training.saved_settings()
        self.device = tk.StringVar(value=training.DEVICE_CHOICES[saved["device"]])
        self.epochs = tk.StringVar(value=str(saved["epochs"]))
        self.imgsz = tk.StringVar(value=str(saved["imgsz"]))
        self.batch = tk.StringVar(value=str(saved["batch"] or ""))
        memory = training.system_memory_gb()
        self.hardware = tk.StringVar(value=f"This computer: {memory:.0f} GB memory." if memory else "")

        help_label(self, "Training settings", "These apply to every training run (Measure tab > Train models; the "
                   "Train window still lets you change the epochs and image size for one run).\n\nSet optimal "
                   "chooses values for this computer and your reviewed sheets. If a run still runs out of memory, "
                   "training lowers the batch size and then the image size by itself and carries on.",
                   style="Heading.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(self, textvariable=self.hardware, wraplength=380, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 8))
        rows = (("Run on", self.device, "Which processor trains the model. Automatic uses an Apple or NVIDIA GPU "
                 "when there is one. A GPU that is not there falls back to the CPU."),
                ("Epochs", self.epochs, "How many passes over the sheets. More takes longer and fits closer."),
                ("Image size (px)", self.imgsz, "The size the sheets are trained at, at least 320. Larger sees "
                 "small seeds better but needs much more memory."),
                ("Batch size", self.batch, "Images trained at once. Larger is faster but needs more memory. "
                 "Leave it empty to let training pick one for the device."))
        for row, (text, var, hint) in enumerate(rows, start=2):
            help_label(self, text, hint).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 14))
            if var is self.device:
                ttk.Combobox(self, textvariable=var, values=list(training.DEVICE_CHOICES.values()),
                             state="readonly", width=14).grid(row=row, column=1, sticky="w", pady=4)
            else:
                ttk.Entry(self, textvariable=var, width=10).grid(row=row, column=1, sticky="w", pady=4)
        self.note = ttk.Label(self, text="", wraplength=380, justify="left", style="Muted.TLabel")
        self.note.grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(self)
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.optimal_button = ttk.Button(buttons, text="Set optimal", command=self._optimal)
        self.optimal_button.pack(side="left")
        ttk.Button(buttons, text="Defaults", command=self._defaults).pack(side="left", padx=6)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save).pack(side="right", padx=6)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.grab_set()

    def _device_key(self):
        return next((key for key, label in training.DEVICE_CHOICES.items() if label == self.device.get()), "auto")

    def _fill(self, settings):
        self.device.set(training.DEVICE_CHOICES[settings.get("device", "auto")])
        self.epochs.set(str(settings["epochs"]))
        self.imgsz.set(str(settings["imgsz"]))
        self.batch.set(str(settings.get("batch") or ""))

    def _defaults(self):
        self._fill({**training.DEFAULTS, "batch": None, "device": "auto"})
        self.note.configure(text="The built-in defaults. Training picks the batch size for the device.")

    def _optimal(self):
        """Look at the computer and the labelled sheets (off the window thread: PyTorch takes a moment to load)."""
        self.optimal_button.state(["disabled"])
        self.hardware.set("Checking this computer…")

        def work():
            try:
                found = training.hardware()
                sheets = len(training.gather_labels())
                error = None
            except Exception as exc:
                found, sheets, error = None, 0, exc
            try:
                self.after(0, lambda: self._optimal_done(found, sheets, error))
            except tk.TclError:
                pass                      # the window was closed meanwhile

        threading.Thread(target=work, daemon=True).start()

    def _optimal_done(self, found, sheets, error):
        self.optimal_button.state(["!disabled"])
        if error or not found:
            self.hardware.set("")
            messagebox.showwarning("ML settings", f"Could not check this computer: {error}", parent=self)
            return
        memory = found["memory_gb"]
        self.hardware.set(f"This computer: {found['name']}" + (f", {memory:.0f} GB memory." if memory else "."))
        train_images = round(sheets * dict(training.SPLITS)["train"]) if sheets else None
        settings = training.recommend(found["device"], memory, train_images)
        self._fill(settings)
        self.note.configure(text=(
            f"Suggested for {found['name']}: image size {settings['imgsz']}, batch {settings['batch']}, "
            f"{settings['epochs']} epochs" + (f" for about {train_images} training sheets." if train_images else
                                              " (no reviewed sheets yet, so the default epochs).")
            + " Press Save to keep them."))

    def _save(self):
        try:
            epochs, imgsz = int(self.epochs.get()), int(self.imgsz.get())
            batch = int(self.batch.get()) if self.batch.get().strip() else None
        except ValueError:
            messagebox.showwarning("ML settings", "Epochs, image size and batch size must be whole numbers.",
                                   parent=self)
            return
        if epochs < 1 or imgsz < 320 or (batch is not None and batch < 1):
            messagebox.showwarning("ML settings", "Use at least 1 epoch, 320 px and a batch of 1.", parent=self)
            return
        self.app.settings["training"] = {"device": self._device_key(), "epochs": epochs, "imgsz": imgsz,
                                         "batch": batch}
        save_settings(self.app.settings)
        self.app._log(f"ML settings saved: {training.DEVICE_CHOICES[self._device_key()]}, {epochs} epochs, "
                      f"image size {imgsz}, batch {batch or 'automatic'}.")
        self.destroy()
