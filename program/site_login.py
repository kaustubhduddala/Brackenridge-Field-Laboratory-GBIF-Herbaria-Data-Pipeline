"""Sign in to a site in a real browser window and keep its cookies for the image downloader.

Needs Playwright (pip install playwright). It uses Google Chrome when installed, otherwise Playwright's own
Chromium (playwright install chromium). The browser profile is kept in BROWSER_PROFILE_DIR so a site that
remembers the browser, or a verification check that was already passed, is not asked again."""
import os
import subprocess
import time
from urllib.parse import urlparse

from config import BROWSER_PROFILE_DIR, BROWSERS_DIR
from .sites import host_of, save_login

INSTALL_HINT = "Install it with:  pip install playwright"


class BrowserMissing(RuntimeError):
    """Neither Google Chrome nor Playwright's Chromium is available."""


def _use_app_browser_folder():
    # Chromium is kept next to the app's other data, so it is found again after updates
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)


def available():
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def driver_ready():
    """True when Playwright's bundled Node.js driver can run. Used by the self-test of the packaged app."""
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        result = subprocess.run([*compute_driver_executable(), "--version"], env=get_driver_env(),
                                capture_output=True, text=True, timeout=60)
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def normalise_url(text):
    text = text.strip()
    return text if urlparse(text).scheme else "https://" + text


def _launch(playwright):
    options = dict(user_data_dir=str(BROWSER_PROFILE_DIR), headless=bool(os.environ.get("GBIF_BROWSER_HEADLESS")),
                   no_viewport=True)
    try:
        return playwright.chromium.launch_persistent_context(channel="chrome", **options)
    except Exception:
        pass
    try:
        return playwright.chromium.launch_persistent_context(**options)
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            raise BrowserMissing("No browser is available. Install Google Chrome, or let the app download "
                                 "Chromium (about 150 MB).") from exc
        raise


def install_chromium(log=print):
    """Download Playwright's Chromium into BROWSERS_DIR using Playwright's own driver, which is bundled with the
    app, so this works in the packaged app too."""
    if not available():
        raise RuntimeError("Browser sign-in needs Playwright. " + INSTALL_HINT)
    from playwright._impl._driver import compute_driver_executable, get_driver_env
    _use_app_browser_folder()
    BROWSERS_DIR.mkdir(parents=True, exist_ok=True)
    command = [*compute_driver_executable(), "install", "chromium"]
    log("Downloading Chromium for browser sign-in. This can take a few minutes.")
    result = subprocess.run(command, env=get_driver_env(), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("Could not download Chromium: " + (result.stderr or result.stdout).strip()[-400:])
    log("Chromium is ready.")


def sign_in(url, done, log=print):
    """Open `url` and wait until `done` is set or the window is closed, then save the cookies.
    Blocks, so run it in a worker thread. Returns the host the cookies were saved for."""
    if not available():
        raise RuntimeError("Browser sign-in needs Playwright. " + INSTALL_HINT)
    from playwright.sync_api import sync_playwright
    _use_app_browser_folder()
    url = normalise_url(url)
    cookies, agent = [], ""
    with sync_playwright() as playwright:
        context = _launch(playwright)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:
                log(f"  The page did not finish loading ({exc}); you can still sign in in the window")
            while not done.is_set():
                try:
                    if not context.pages:
                        break
                    cookies = context.cookies()
                    agent = context.pages[0].evaluate("navigator.userAgent")
                except Exception:
                    break
                time.sleep(0.5)
            try:
                cookies = context.cookies()
            except Exception:
                pass
        finally:
            try:
                context.close()
            except Exception:
                pass
    if not cookies:
        raise RuntimeError("No cookies were captured. Sign in, then click Done before closing the window.")
    host = host_of(url)
    save_login(host, cookies, agent)
    log(f"Saved the sign-in for {host} ({len(cookies)} cookies)")
    return host
