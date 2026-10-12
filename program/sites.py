"""Per-site handling for image downloads: saved sign-ins, browser-like retries and site-specific downloaders."""
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

from config import BROWSER_USER_AGENT, HEADERS, SITE_LOGINS_FILE

DENIED_STATUSES = (401, 403)
MODE_REQUESTS, MODE_BROWSER = "requests", "browser"
STRATEGIES = {
    "Automatic: plain request, then browser-like headers, then the browser": "auto",
    "Plain requests only (never open a browser)": "requests",
    "Always use the browser for sites that refuse access": "browser",
    "Ask me: stop and open Site logins": "ask",
}
PREFS = {"strategy": "auto", "challenge_wait": 180, "browser_delay": 2.0, "request_delay": 0.0}
_last_request = {}
_gave_up = set()  # sites the browser could not get past in this run
BLOCK_MARKERS = re.compile(r"just a moment|cf-challenge|captcha|access denied|attention required|"
                           r"verify you are (?:a )?human|please (?:log|sign) ?in", re.IGNORECASE)


class ItemSkipped(Exception):
    pass


class AccessDenied(Exception):
    """A site refused the request in a way a sign-in or a real browser may fix."""

    def __init__(self, url, status=None, reason=""):
        self.url, self.status, self.host = url, status, host_of(url)
        why = f"HTTP {status}" if status else (reason or "blocked")
        super().__init__(f"Access denied by {self.host} ({why}); sign in to this site from Images, Site logins")


def host_of(url):
    return (urlparse(url).hostname or "").lower()


def looks_blocked(html):
    return bool(BLOCK_MARKERS.search(html or ""))


def _domain_matches(host, domain):
    domain = domain.lstrip(".").lower()
    return host == domain or host.endswith("." + domain)


# ---------------------------------------------------------------- saved sign-ins

def load_logins():
    path = Path(SITE_LOGINS_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        # never overwrite a file we could not read: keep it aside so the sign-ins in it are not lost
        try:
            path.replace(path.with_name(path.name + ".unreadable"))
        except OSError:
            pass
        return {}


def _write_logins(data):
    """Write atomically and keep the previous version as site_logins.json.bak."""
    path = Path(SITE_LOGINS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    temp.replace(path)
    reload_session()


def saved_hosts():
    """{host: when the sign-in was saved}"""
    return dict(load_logins().get("saved", {}))


def known_sites():
    """{host: when it last refused access}. Kept for good, so the list survives restarts and updates."""
    return dict(load_logins().get("refused", {}))


def note_refused(host):
    data = load_logins()
    data.setdefault("refused", {})[host] = datetime.now().isoformat(timespec="minutes")
    _write_logins(data)


def site_mode(host):
    """How downloads from `host` are made: plain requests, or through the signed-in browser."""
    modes = load_logins().get("modes", {})
    best = max((h for h in modes if _domain_matches(host, h)), key=len, default=None)
    return modes.get(best, MODE_REQUESTS)


def set_site_mode(host, mode):
    data = load_logins()
    data.setdefault("modes", {})[host] = mode
    _write_logins(data)


def configure(**prefs):
    PREFS.update({k: v for k, v in prefs.items() if k in PREFS and v is not None})


def new_run():
    _gave_up.clear()


def throttle(host, mode, control=None):
    """Wait so that requests to one site keep a polite distance."""
    interval = PREFS["browser_delay"] if mode == MODE_BROWSER else PREFS["request_delay"]
    wait = _last_request.get(host, 0) + interval - time.monotonic()
    while wait > 0:
        if control:
            control.check()
        time.sleep(min(wait, 0.25))
        wait -= 0.25
    _last_request[host] = time.monotonic()


def save_login(host, cookies, user_agent=""):
    data = load_logins()
    known = {(c.get("name"), c.get("domain"), c.get("path")): c for c in data.get("cookies", [])}
    for cookie in cookies:
        known[(cookie.get("name"), cookie.get("domain"), cookie.get("path"))] = cookie
    data["cookies"] = list(known.values())
    data.setdefault("saved", {})[host] = datetime.now().isoformat(timespec="minutes")
    if user_agent:
        data["user_agent"] = user_agent
    _write_logins(data)


def forget_login(host):
    data = load_logins()
    data["cookies"] = [c for c in data.get("cookies", []) if not _domain_matches(host, c.get("domain", ""))
                       and not _domain_matches(c.get("domain", "").lstrip("."), host)]
    data.get("saved", {}).pop(host, None)
    _write_logins(data)


# ---------------------------------------------------------------- requests

_session = None


def reload_session():
    global _session
    _session = None


def session():
    global _session
    if _session is None:
        _session = requests.Session()
        for cookie in load_logins().get("cookies", []):
            if cookie.get("name") is not None:
                _session.cookies.set(cookie["name"], cookie.get("value", ""), domain=cookie.get("domain", ""),
                                     path=cookie.get("path", "/"))
    return _session


def _has_login(host):
    return any(_domain_matches(host, h) or _domain_matches(h, host) for h in saved_hosts())


def _browser_fetch(url, control, log):
    from . import site_login  # imported here: site_login imports this module
    return site_login.browser_fetch(url, control, log)


def fetch(url, stream=False, timeout=30, control=None, log=print):
    """GET `url`, escalating as far as the download preference allows:
    1. a plain request with any saved sign-in, retried once with browser-like headers;
    2. the same download through a visible browser, where a person can pass a check;
    3. raise AccessDenied, which lists the site under Site logins for a sign-in or a manual download.
    Other statuses are returned for the caller to judge."""
    host = host_of(url)
    strategy = PREFS["strategy"]
    if strategy == "browser" or site_mode(host) == MODE_BROWSER:
        return _through_browser(url, host, control, log)
    throttle(host, MODE_REQUESTS, control)
    try:
        return _plain_fetch(url, host, stream, timeout)
    except AccessDenied:
        if strategy != "auto":
            raise
        resp = _through_browser(url, host, control, log, fallback=True)
        set_site_mode(host, MODE_BROWSER)
        log(f"    {host} will be downloaded through the browser from now on")
        return resp


def _through_browser(url, host, control, log, fallback=False):
    from . import site_login
    if host in _gave_up:
        raise AccessDenied(url, reason="the browser could not get past this site earlier in the run")
    if not site_login.available():
        raise AccessDenied(url, reason="a browser is needed; " + site_login.INSTALL_HINT)
    if fallback:
        log(f"    {host} refuses plain requests; trying through the browser")
    throttle(host, MODE_BROWSER, control)
    try:
        return site_login.browser_fetch(url, control, log)
    except (AccessDenied, ItemSkipped) as exc:
        if isinstance(exc, AccessDenied):
            _gave_up.add(host)
        raise
    except Exception as exc:  # no browser installed, profile in use, browser closed by the user, ...
        _gave_up.add(host)
        raise AccessDenied(url, reason=f"the browser could not be used: {str(exc).splitlines()[0][:120]}") from exc


def _plain_fetch(url, host, stream, timeout):
    headers = dict(HEADERS)
    agent = load_logins().get("user_agent")
    if agent and _has_login(host):
        headers["User-Agent"] = agent
    resp = session().get(url, timeout=timeout, verify=False, headers=headers, stream=stream)
    if resp.status_code in DENIED_STATUSES:
        resp.close()
        headers.update({"User-Agent": agent or BROWSER_USER_AGENT, "Referer": f"{urlparse(url).scheme}://{host}/",
                        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
        resp = session().get(url, timeout=timeout, verify=False, headers=headers, stream=stream)
        if resp.status_code in DENIED_STATUSES:
            status = resp.status_code
            resp.close()
            raise AccessDenied(url, status)
    return resp


# ---------------------------------------------------------------- site-specific downloaders

def download_special(url, stem, log=print, control=None):
    """Download `url` in a way that is specific to its site. Returns the saved file's Path, or None to let the
    general downloader handle the URL. Add a branch for each site that needs its own method."""
    host = host_of(url)
    if host.endswith("oxalis.br.fgov.be"):
        return _download_meise(url, stem, log, control)
    if host == "drive.google.com":
        return _download_google_drive(url, stem, log, control)
    if host.endswith("nmnh.si.edu"):
        return _download_smithsonian(url, stem, log, control)
    return None


def _download_meise(url, stem, log, control):
    # Meise Botanic Garden (BR): IIIF manifests, the image server refuses plain requests.
    pass


def _download_google_drive(url, stem, log, control):
    # Shared Drive links return a viewer page instead of the file.
    pass


def _download_smithsonian(url, stem, log, control):
    # NMNH media pages build the image with JavaScript.
    pass
