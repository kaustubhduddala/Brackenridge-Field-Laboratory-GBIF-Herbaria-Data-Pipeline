"""The window that offers an update, downloads it and restarts the app into it."""
import shutil
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from config import APP_NAME, APP_VERSION
from . import updater
from .gui_widgets import set_title_bar, style_text


class UpdateDialog(tk.Toplevel):
    """Shows a newer release with its notes. "Install and restart" downloads it, checks it, then quits and
    starts the new version; with `start` it begins at once (the automatic-install setting)."""

    def __init__(self, app, update, start=False):
        super().__init__(app.root)
        self.app, self.update_info = app, update
        self.title("Update available")
        self.resizable(False, False)
        self.transient(app.root)
        self.configure(background=app.palette["bg"])
        set_title_bar(self, app.theme_name == "dark")
        self.cancel_flag = threading.Event()
        self.working = False
        self.folder = None
        self.can_install = updater.install_location() is not None

        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"{APP_NAME} {update.tag} is available", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"You have {APP_VERSION}.").pack(anchor="w", pady=(2, 8))
        if update.notes:
            notes = tk.Text(body, width=64, height=9, wrap="word", font=app.fonts["mono"], padx=8, pady=6)
            notes.insert("1.0", update.notes)
            notes.configure(state="disabled")
            style_text(notes, app.palette)
            app.text_widgets.append(notes)
            notes.pack(fill="x")
        self.status = tk.StringVar()
        self.bar = ttk.Progressbar(body, length=420, mode="determinate")
        self.status_label = ttk.Label(body, textvariable=self.status, style="Muted.TLabel")

        self.buttons = ttk.Frame(body)
        self.buttons.pack(fill="x", pady=(12, 0))
        self.install_button = ttk.Button(
            self.buttons, text="Install and restart" if self.can_install else "Open download page",
            style="Accent.TButton", command=self._install if self.can_install else self._open_page)
        self.install_button.pack(side="right")
        self.later_button = ttk.Button(self.buttons, text="Later", command=self._close)
        self.later_button.pack(side="right", padx=6)
        self.skip_button = ttk.Button(self.buttons, text="Skip this version", command=self._skip)
        self.skip_button.pack(side="left")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _: self._close())
        if start and self.can_install:
            self.after(200, self._install)

    # ------------------------------------------------------------------ buttons

    def _open_page(self):
        webbrowser.open(self.update_info.page)
        self._close()

    def _skip(self):
        self.app.settings["skipped_update"] = self.update_info.tag
        self.app._save_settings()
        self._close()

    def _close(self):
        if self.working:                              # while downloading, closing the window cancels
            self.cancel_flag.set()
            return
        self.destroy()

    def _show_progress(self, text):
        self.skip_button.state(["disabled"])
        self.install_button.state(["disabled"])
        self.later_button.configure(text="Cancel")
        self.bar.pack(fill="x", pady=(12, 0))
        self.status_label.pack(anchor="w", pady=(4, 0))
        self.status.set(text)

    # ------------------------------------------------------------------ installing

    def _install(self):
        if self.working:
            return
        self.working = True
        self._show_progress("Downloading…")
        threading.Thread(target=self._work, daemon=True).start()

    def _tell(self, fn, *args):
        self.app.root.after(0, fn, *args)

    def _progress(self, done, total):
        if total:
            self._tell(self._set_bar, done, total)

    def _set_bar(self, done, total):
        if self.winfo_exists():
            self.bar.configure(maximum=total, value=done)
            self.status.set(f"Downloading… {done / 1e6:.0f} of {total / 1e6:.0f} MB")

    def _work(self):
        import tempfile
        install = updater.install_location()
        try:
            self.folder = tempfile.mkdtemp(prefix=f"{APP_NAME}-download-")
            archive = updater.download(self.update_info, self.folder, self._progress, self.cancel_flag.is_set)
            self._tell(self._set_status, "Checking the new version…")
            staging, unpacked = updater.stage(archive, install)
        except updater.UpdateError as exc:
            self._tell(self._failed, str(exc))
            return
        except Exception as exc:                      # never leave the person with a frozen dialog
            self._tell(self._failed, f"The update failed: {exc}")
            return
        self._tell(self._staged, install, staging, unpacked)

    def _set_status(self, text):
        if self.winfo_exists():
            self.bar.configure(mode="indeterminate")
            self.bar.start(12)
            self.status.set(text)

    def _cleanup(self):
        if self.folder:
            shutil.rmtree(self.folder, ignore_errors=True)

    def _failed(self, message):
        self._cleanup()
        self.working = False
        cancelled = self.cancel_flag.is_set()
        if not self.winfo_exists():
            return
        self.destroy()
        if not cancelled:
            self.app._log(f"Update to {self.update_info.tag} failed: {message}")
            if messagebox.askyesno("Update", f"{message}\n\nOpen the releases page to download it yourself?",
                                   parent=self.app.root):
                webbrowser.open(self.update_info.page)

    def _staged(self, install, staging, unpacked):
        self._cleanup()
        self.working = False
        if self.cancel_flag.is_set() or not self.app._can_quit():
            shutil.rmtree(staging, ignore_errors=True)
            if self.winfo_exists():
                self.destroy()
            return
        try:
            updater.apply_on_exit(install, unpacked, staging)
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            self.destroy()
            messagebox.showerror("Update", f"The update could not be started: {exc}", parent=self.app.root)
            return
        self.app._shutdown()
