import subprocess
import sys
from tkinter import ttk

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


def resolve(choice):
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
    set_title_bar(root, name == "dark")
    return p
