"""Per-site handling for image downloads: saved sign-ins, browser-like retries and site-specific downloaders."""
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

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
BLOCK_MARKERS = re.compile(r"request rejected|just a moment|cf-challenge|captcha|access denied|attention required|"
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


def site_reasons():
    """{host: why it is on the list}, for sites skipped by the user or given up on after repeated failures."""
    return dict(load_logins().get("reasons", {}))


def note_refused(host, reason=None):
    data = load_logins()
    data.setdefault("refused", {})[host] = datetime.now().isoformat(timespec="minutes")
    if reason:
        data.setdefault("reasons", {})[host] = reason
    else:
        data.get("reasons", {}).pop(host, None)
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


SHORT_USER_AGENT = "GBIF-HerbariaPipeline/1.0"
# Some servers refuse by User-Agent text alone: oxalis.br.fgov.be answers 403 to anything but a bare product token
# (not to "Python requests", not even to a Chrome string). Keyed by host suffix.
SITE_USER_AGENTS = {"oxalis.br.fgov.be": SHORT_USER_AGENT}
_working_agent = {}  # host -> the User-Agent that was accepted, tried first from then on


def _plain_fetch(url, host, stream, timeout):
    """Try the usual headers, then a bare User-Agent, then browser-like headers with a Referer."""
    saved = load_logins().get("user_agent") if _has_login(host) else None
    configured = next((ua for suffix, ua in SITE_USER_AGENTS.items() if _domain_matches(host, suffix)), None)
    referer = {"Referer": f"{urlparse(url).scheme}://{host}/", "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    attempts = [dict(HEADERS, **{"User-Agent": _working_agent.get(host) or configured or saved or HEADERS["User-Agent"]}),
                {"User-Agent": SHORT_USER_AGENT},
                {"User-Agent": saved or BROWSER_USER_AGENT, **referer}]
    seen, status = [], None
    for headers in attempts:
        if headers in seen:
            continue
        seen.append(headers)
        resp = session().get(url, timeout=timeout, verify=False, headers=headers, stream=stream)
        if resp.status_code not in DENIED_STATUSES:
            _working_agent[host] = headers["User-Agent"]
            return resp
        status = resp.status_code
        resp.close()
    raise AccessDenied(url, status)


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
    if host.endswith("cria.org.br"):
        return _download_cria(url, stem, log, control)
    return None


IIIF_SIZES = ("max", "full", "!4000,4000", "!3000,3000", "!2000,2000", "!1000,1000")
IIIF_IMAGE_URL = re.compile(r"^(?P<base>.+?)/(?:full|square|\d+,\d+,\d+,\d+|pct:[^/]+)/[^/]+/[^/]+/[^/]+$")


def _try_get(url, control=None):
    """One plain request, no escalation. The response, or None for any refusal or error."""
    host = host_of(url)
    throttle(host, MODE_REQUESTS, control)
    try:
        resp = _plain_fetch(url, host, True, 30)
    except (AccessDenied, requests.RequestException):
        return None
    if resp.status_code >= 400:
        resp.close()
        return None
    return resp


def _iiif_image_base(url, control, log):
    """The IIIF image base URL (the part before /full/max/0/default.jpg) for a manifest or an image URL."""
    match = IIIF_IMAGE_URL.match(url)
    if match:
        return match.group("base")
    resp = _try_get(url, control)
    if resp is None:
        return None
    from .media import _extract_image_from_json  # imported here: media imports this module
    image = _extract_image_from_json(resp.text, log)
    match = image and IIIF_IMAGE_URL.match(image)
    return match.group("base") if match else None


def _download_iiif(url, stem, log, control):
    """Download a IIIF image at the largest size the server allows. A server limits what one request may return
    (info.json maxArea, maxWidth, maxHeight) and answers 403 to anything bigger, so an image over the limit is
    fetched as full-resolution regions and joined. Returns the saved Path or None."""
    base = _iiif_image_base(url, control, log)
    if not base:
        return None
    info, width, height, limit = {}, 0, 0, {}
    response = _try_get(base + "/info.json", control)
    if response is not None:
        try:
            info = response.json()
            width, height = int(info.get("width", 0)), int(info.get("height", 0))
            limit = {k: int(info[k]) for k in ("maxArea", "maxWidth", "maxHeight") if info.get(k)}
        except (ValueError, TypeError):
            info = {}

    if width and height and limit:
        max_w = limit.get("maxWidth", width)
        max_h = limit.get("maxHeight", limit.get("maxWidth", height)) if "maxHeight" in limit or "maxWidth" in limit \
            else height
        area = limit.get("maxArea", max_w * max_h)
        if width * height > area or width > max_w or height > max_h:
            edge = int(min(max_w, max_h, area ** 0.5, 4096))
            pieces = [(x, y, f"{base}/{x},{y},{min(edge, width - x)},{min(edge, height - y)}/"
                             f"{min(edge, width - x)},/0/default.jpg")
                      for y in range(0, height, edge) for x in range(0, width, edge)]
            try:
                path = _stitch(width, height, pieces, stem, log, control)
                log(f"    IIIF image joined from regions ({width} x {height})")
                return path
            except ItemSkipped:
                raise
            except Exception as exc:
                log(f"    Region download failed ({exc}); trying smaller sizes")

    sizes = list(IIIF_SIZES)
    if width and height and limit.get("maxArea"):
        scale = (limit["maxArea"] / (width * height)) ** 0.5
        sizes.insert(0, f"{int(width * scale)},")
    elif width:
        sizes.insert(2, f"{width},")
    for size in sizes:
        resp = _try_get(f"{base}/full/{size}/0/default.jpg", control)
        if resp is None:
            log(f"    {host_of(url)} did not accept size {size}")
            continue
        path = _save_image(resp, stem, control)
        if path:
            log(f"    IIIF image at size {size}")
            return path
    return None  # the general downloader takes over, including its browser fallback


def _download_meise(url, stem, log, control):
    # Meise Botanic Garden (BR): the manifest loads but the image server can refuse the "max" size the manifest
    # points to, so ask the IIIF service for other sizes, largest first.
    return _download_iiif(url, stem, log, control)


def _save_image(resp, stem, control=None):
    """Write an image response to <stem>.<extension>. Returns the Path, or None when it is not an image."""
    from .media import _detect_extension  # imported here: media imports this module
    kind = resp.headers.get("Content-Type", "").lower()
    if not kind.startswith("image/"):
        resp.close()
        return None
    path = Path(f"{stem}.{_detect_extension(kind, resp.url)}")
    partial = path.with_name(path.name + ".part")
    try:
        with resp, open(partial, "wb") as out:
            for chunk in resp.iter_content(1 << 16):
                if control:
                    control.check()
                out.write(chunk)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)
    return path


def _download_google_drive(url, stem, log, control):
    # Share links open a viewer page; the download address of the same file returns the image itself.
    match = re.search(r"/file/d/([\w-]+)|[?&]id=([\w-]+)", url)
    if not match:
        return None
    file_id = match.group(1) or match.group(2)
    for address in (f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
                    f"https://drive.google.com/uc?export=download&id={file_id}"):
        resp = _try_get(address, control)
        path = resp and _save_image(resp, stem, control)
        if path:
            log("    Google Drive file downloaded")
            return path
    log("    Google Drive did not give the file (it may not be shared with everyone)")
    return None


def _download_smithsonian(url, stem, log, control):
    # collections.nmnh.si.edu/media/?i=... redirects to the image on ids.si.edu, but its firewall rejects requests
    # that do not look like a browser's.
    host = host_of(url)
    headers = {"User-Agent": BROWSER_USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9", "Referer": f"https://{host}/"}
    throttle(host, MODE_REQUESTS, control)
    try:
        resp = session().get(url, headers=headers, timeout=30, verify=False, allow_redirects=False, stream=True)
        location = resp.headers.get("Location") if resp.is_redirect else None
        if location is None:
            path = _save_image(resp, stem, control)
            if not path:
                log("    The Smithsonian site rejected the request")
            return path
        resp.close()
        resp = session().get(urljoin(url, location), headers=headers, timeout=60, verify=False, stream=True)
    except requests.RequestException as exc:
        log(f"    Smithsonian download failed: {exc}")
        return None
    return _save_image(resp, stem, control)


STITCH_WORKERS = 6


def _zoomify_levels(width, height, tile):
    """(columns, rows, first tile number) for each level of a Zoomify pyramid, smallest first."""
    sizes = [(width, height)]
    while sizes[0][0] > tile or sizes[0][1] > tile:
        sizes.insert(0, ((sizes[0][0] + 1) // 2, (sizes[0][1] + 1) // 2))
    levels, first = [], 0
    for w, h in sizes:
        columns, rows = -(-w // tile), -(-h // tile)
        levels.append((columns, rows, first))
        first += columns * rows
    return levels, sizes


def _stitch(width, height, pieces, stem, log, control):
    """Fetch every piece, paste it into one image and save it as <stem>.jpg. `pieces` are (x, y, url)."""
    from concurrent.futures import ThreadPoolExecutor
    from io import BytesIO
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None

    def get(piece):
        x, y, address = piece
        resp = _try_get(address, control)
        if resp is None:
            raise ValueError(f"the piece at {x},{y} could not be fetched")
        with resp:
            return x, y, resp.content

    image = Image.new("RGB", (width, height))
    log(f"    Fetching {len(pieces)} pieces")
    with ThreadPoolExecutor(STITCH_WORKERS) as pool:
        futures = [pool.submit(get, piece) for piece in pieces]
        try:
            for future in futures:
                if control:
                    control.check()
                x, y, data = future.result()
                with Image.open(BytesIO(data)) as part:
                    image.paste(part.convert("RGB"), (x, y))
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    path = Path(f"{stem}.jpg")
    partial = path.with_name(path.name + ".part")
    try:
        image.save(partial, format="JPEG", quality=95)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)
    return path


def _download_zoomify(base, width, height, tile, stem, log, control):
    """Fetch every tile of the largest level of a Zoomify image and join them into one JPEG."""
    levels, _ = _zoomify_levels(width, height, tile)
    top = len(levels) - 1
    columns, rows, first = levels[top]
    base = base if base.endswith("/") else base + "/"
    pieces = [(x * tile, y * tile, f"{base}TileGroup{(first + y * columns + x) // 256}/{top}-{x}-{y}.jpg")
              for y in range(rows) for x in range(columns)]
    return _stitch(width, height, pieces, stem, log, control)


def _download_cria(url, stem, log, control):
    # CRIA / speciesLink viewer pages only wrap an OpenSeadragon viewer. The image is a Zoomify tile pyramid whose
    # address, width and height are written into the page.
    page = _try_get(url, control)
    if page is None:
        return None
    html = page.text
    tiles = re.search(r"tilesUrl:\s*[\"']([^\"']+)", html)
    size = re.search(r"width:\s*(\d+),\s*height:\s*(\d+)", html)
    if not (tiles and size):
        log("    CRIA page has no tile information")
        return None
    width, height = int(size.group(1)), int(size.group(2))
    properties = _try_get(tiles.group(1).rstrip("/") + "/ImageProperties.xml", control)
    tile = 256
    if properties is not None:
        found = re.search(r'TILESIZE="(\d+)"', properties.text)
        tile = int(found.group(1)) if found else tile
    try:
        path = _download_zoomify(tiles.group(1), width, height, tile, stem, log, control)
        log(f"    CRIA image assembled from tiles ({width} x {height})")
        return path
    except ItemSkipped:
        raise
    except Exception as exc:
        log(f"    Tile download failed ({exc}); trying the speciesLink download")
    code = re.search(r"/viewer/([^/?#]+)", url)
    folder = re.search(r"https?://[^/]+/(?:[^/]+/)*?(herbaria/[^\"']+?)/?$", tiles.group(1))
    if code and folder:
        address = ("https://specieslink.net/search/util/osd-dezoomify?imagecode=" + code.group(1)
                   + f"&path={folder.group(1)}&width={width}&height={height}")
        resp = _try_get(address, control)
        return resp and _save_image(resp, stem, control)
    return None
