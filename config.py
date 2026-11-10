import json
import os
import sys
from pathlib import Path

APP_NAME = "Graas"      # short name: used for folders, keychain entries and records, so no spaces or punctuation
APP_TITLE = "Graas: Grass Research & Analysis Automation System"   # shown in the window title and About box
LEGACY_APP_NAME = "GBIF Herbaria Pipeline"                          # what earlier versions were called
FROZEN = getattr(sys, "frozen", False)
CODE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _read_version():
    try:
        return (CODE_DIR / "VERSION").read_text().strip() or "dev"
    except OSError:
        return "dev"


APP_VERSION = _read_version()
UPDATE_REPO = "kaustubhduddala/GRAAS"     # GitHub repository whose releases the app updates itself from



def _packaged_home():
    """Documents/Graas, or the folder an earlier version used when that exists and this one has none yet, so
    renaming the app does not hide anyone's data and settings."""
    documents = Path.home() / "Documents"
    legacy = documents / LEGACY_APP_NAME
    return legacy if legacy.is_dir() and not (documents / APP_NAME).exists() else documents / APP_NAME


if os.environ.get("GRAAS_HOME") or os.environ.get("GBIF_PIPELINE_HOME"):    # the old variable keeps working
    BASE_DIR = Path(os.environ.get("GRAAS_HOME") or os.environ["GBIF_PIPELINE_HOME"]).expanduser()
elif FROZEN:
    BASE_DIR = _packaged_home()
else:
    BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RESPONSES_DIR_NAME = "responses"
LOG_FILE = DATA_DIR / "pipeline.log"
SETTINGS_FILE = BASE_DIR / "settings.json"
SITE_LOGINS_FILE = BASE_DIR / "site_logins.json"
BROWSER_PROFILE_DIR = BASE_DIR / "browser_profile"
BROWSERS_DIR = BASE_DIR / "browsers"  # Playwright's Chromium, downloaded on demand
ADDONS_DIR = BASE_DIR / "addons"      # packaged app only: the optional PyTorch and label-reader packages (see below)


def activate_addons():
    """Let the packaged app import the optional packages installed into ADDONS_DIR.

    The packaged app cannot add packages to itself (its own folder is signed and may be read-only, and it
    carries no Python to run pip with), so Model setup installs them into Documents/Graas/addons instead.
    The folder goes last on sys.path: what is already bundled (numpy, OpenCV...) is always the copy used,
    and the add-ons only supply what the bundle lacks. Does nothing from source, where pip installs into the
    environment, or while the folder does not exist yet.
    """
    if FROZEN and ADDONS_DIR.is_dir() and str(ADDONS_DIR) not in sys.path:
        sys.path.append(str(ADDONS_DIR))
        import importlib
        importlib.invalidate_caches()


activate_addons()
BROWSER_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0.0.0 Safari/537.36")

LMSTUDIO_URL = os.environ.get("LMSTUDIO_URL", "http://localhost:1234/v1")
LMSTUDIO_MODEL = os.environ.get("LMSTUDIO_MODEL", "")
MIN_CONTEXT_LENGTH = 16384
DEFAULT_TOKEN_LIMIT = 8000
THINKING_CHOICES = {"Model default": None, "Low": "low", "Off": "none"}
MAX_IMAGE_SIDE = 2560
REQUEST_TIMEOUT_S = 900
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".gif"}
HEADERS = {"User-Agent": "GBIF-HerbariaPipeline/1.0 (research use; Python requests)"}


LEGACY_CREDENTIALS_FILE = Path.home() / "credentials.json"
ACCOUNT_FILE = BASE_DIR / "gbif_account.json"
KEYRING_SERVICE = APP_NAME + " (GBIF)"
LEGACY_KEYRING_SERVICE = LEGACY_APP_NAME + " (GBIF)"
GBIF_SIGNUP_URL = "https://www.gbif.org/user/profile"


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _keyring():
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def password_store_name():
    if _keyring() is None:
        return None
    if sys.platform == "darwin":
        return "the macOS Keychain"
    if sys.platform.startswith("win"):
        return "Windows Credential Manager"
    return "the system keyring"


def _stored_secret(store, service, legacy_service, name):
    """A secret from the keychain: under this app's name, else under the name an earlier version used (and then
    copied across, so the next lookup finds it under the new name)."""
    secret = store.get_password(service, name)
    if secret:
        return secret
    secret = store.get_password(legacy_service, name)
    if secret:
        try:
            store.set_password(service, name, secret)
        except Exception:
            pass
    return secret


def _saved_password(user, account):
    store = _keyring()
    if store and user:
        try:
            password = _stored_secret(store, KEYRING_SERVICE, LEGACY_KEYRING_SERVICE, user)
            if password:
                return password
        except Exception:
            pass
    return account.get("password", "")


def gbif_credentials():
    env = {key: os.environ.get(f"GBIF_{key.upper()}", "") for key in ("user", "password", "email")}
    if all(env.values()):
        return env["user"], env["password"], env["email"], "environment variables"
    account = _read_json(ACCOUNT_FILE)
    if account.get("user"):
        user = env["user"] or account["user"]
        password = env["password"] or _saved_password(account["user"], account)
        email = env["email"] or account.get("email", "")
        return user, password, email, "this app"
    legacy = _read_json(LEGACY_CREDENTIALS_FILE)
    if legacy.get("user"):
        return (env["user"] or legacy.get("user", ""), env["password"] or legacy.get("password", ""),
                env["email"] or legacy.get("email", ""), f"{LEGACY_CREDENTIALS_FILE}")
    return env["user"], env["password"], env["email"], "environment variables" if any(env.values()) else ""


def has_gbif_credentials():
    user, password, email, _ = gbif_credentials()
    return bool(user and password and email)


def saved_gbif_account():
    account = _read_json(ACCOUNT_FILE)
    user = account.get("user", "")
    return {"user": user, "email": account.get("email", ""), "has_password": bool(_saved_password(user, account))}


def saved_gbif_password(user):
    account = _read_json(ACCOUNT_FILE)
    return _saved_password(user, account) if user and account.get("user") == user else ""


def save_gbif_account(user, email, password=None):
    user, email = user.strip(), email.strip()
    previous = _read_json(ACCOUNT_FILE)
    if password is None:
        password = _saved_password(previous.get("user", ""), previous)
    store = _keyring()
    account = {"user": user, "email": email}
    where = None
    if password:
        if store:
            try:
                if previous.get("user") and previous["user"] != user:
                    _forget_password(previous["user"])
                store.set_password(KEYRING_SERVICE, user, password)
                where = password_store_name()
            except Exception:
                store = None
        if not store:
            account["password"] = password
            where = ACCOUNT_FILE.name
    ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNT_FILE.write_text(json.dumps(account, indent=2))
    try:
        os.chmod(ACCOUNT_FILE, 0o600)
    except OSError:
        pass
    return where


def _forget_password(user):
    store = _keyring()
    if store and user:
        try:
            store.delete_password(KEYRING_SERVICE, user)
        except Exception:
            pass
        try:
            store.delete_password(LEGACY_KEYRING_SERVICE, user)
        except Exception:
            pass


def remove_gbif_account():
    _forget_password(_read_json(ACCOUNT_FILE).get("user", ""))
    ACCOUNT_FILE.unlink(missing_ok=True)


AI_KEYRING_SERVICE = APP_NAME + " (AI)"
LEGACY_AI_KEYRING_SERVICE = LEGACY_APP_NAME + " (AI)"
AI_KEYS_FILE = BASE_DIR / "ai_keys.json"


def ai_api_key(provider, env_names=()):
    """(key, where it came from) for an AI provider; environment variables win over saved keys."""
    for name in env_names:
        if os.environ.get(name):
            return os.environ[name], f"the {name} environment variable"
    store = _keyring()
    if store:
        try:
            key = _stored_secret(store, AI_KEYRING_SERVICE, LEGACY_AI_KEYRING_SERVICE, provider)
            if key:
                return key, password_store_name()
        except Exception:
            pass
    key = _read_json(AI_KEYS_FILE).get(provider, "")
    return key, (AI_KEYS_FILE.name if key else "")


def save_ai_api_key(provider, key):
    """Save a provider's API key in the system keychain, or a private file when there is none."""
    store = _keyring()
    if store:
        try:
            store.set_password(AI_KEYRING_SERVICE, provider, key)
            return password_store_name()
        except Exception:
            pass
    keys = _read_json(AI_KEYS_FILE)
    keys[provider] = key
    AI_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    AI_KEYS_FILE.write_text(json.dumps(keys, indent=2))
    try:
        os.chmod(AI_KEYS_FILE, 0o600)
    except OSError:
        pass
    return AI_KEYS_FILE.name


def remove_ai_api_key(provider):
    store = _keyring()
    if store:
        try:
            store.delete_password(AI_KEYRING_SERVICE, provider)
        except Exception:
            pass
        try:
            store.delete_password(LEGACY_AI_KEYRING_SERVICE, provider)
        except Exception:
            pass
    keys = _read_json(AI_KEYS_FILE)
    if keys.pop(provider, None) is not None:
        AI_KEYS_FILE.write_text(json.dumps(keys, indent=2))


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except OSError:
        pass


BAD_GEOSPATIAL_ISSUES = []
INSPECTION_ISSUES = [
    "COUNTRY_MISMATCH",
    "RECORDED_DATE_MISMATCH",
    "RECORDED_DATE_INVALID",
    "RECORDED_DATE_UNLIKELY",
    "OCCURRENCE_STATUS_UNPARSABLE",
    "COORDINATE_ROUNDED",
    "GEODETIC_DATUM_INVALID",
    "GEODETIC_DATUM_ASSUMED_WGS84",
    "COORDINATE_PRECISION_INVALID",
    "COORDINATE_UNCERTAINTY_METERS_INVALID",
    "FOOTPRINT_INVALID",
    "FOOTPRINT_WKT_MISMATCH",
    "FOOTPRINT_SRS_INVALID",
    "CONTINENT_COORDINATE_MISMATCH",
    "COUNTRY_COORDINATE_MISMATCH",
    "CONTINENT_COUNTRY_MISMATCH",
    "MULTIMEDIA_DATE_INVALID",
]

MEDIA_COLUMNS = ["identifier", "references_multimedia", "references_occurrence", "references", "occurrenceID"]
LINK_COLUMNS = ["identifier", "references_multimedia", "references", "occurrenceID",
                "bibliographicCitation", "associatedReferences"]
MEDIA_EVIDENCE_COLUMNS = ["mediaType", "type_multimedia", "identifier", "format", "references_multimedia"]
MEASUREMENT_COLUMNS = ["Panicle length (cm)", "Leaf width (cm)", "Seed length (cm)"]
MULTIMEDIA_RENAMES = {
    "references": "references_multimedia",
    "publisher": "publisher_multimedia",
    "license": "license_multimedia",
    "rightsHolder": "rightsHolder_multimedia",
}

PRECISION_NONE = "none"
PRECISION_RELAXED = "relaxed"
PRECISION_STRICT = "strict"

BASE_FILTERS = (("BASIS_OF_RECORD", "PRESERVED_SPECIMEN"),)
PRESETS = {
    "G064 (Default)": {
        "species": "Megathyrsus maximus",
        "precision": PRECISION_NONE,
        "exclude_taxa": [],
        "bad_issues": [],
        "inspect_issues": INSPECTION_ISSUES,
        "filters": BASE_FILTERS,
    },
    "G024": {
        "species": "Megathyrsus maximus",
        "precision": PRECISION_NONE,
        "exclude_taxa": [],
        "bad_issues": [],
        "inspect_issues": INSPECTION_ISSUES,
        "filters": BASE_FILTERS + (("OCCURRENCE_STATUS", "PRESENT"),),
    },
    "Custom": {
        "species": "",
        "precision": PRECISION_NONE,
        "exclude_taxa": [],
        "bad_issues": [],
        "inspect_issues": INSPECTION_ISSUES,
        "filters": BASE_FILTERS,
    },
}
