"""Download the specimen images listed in a dataset.

Includes per-site handling: saved sign-ins, browser-like retries and site-specific downloaders.
"""
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests

from config import BROWSER_USER_AGENT, HEADERS, IMAGE_EXTENSIONS, SITE_LOGINS_FILE
from .utils import (ItemSkipped, check, find_media_url, gbif_ids, index_images, is_blank, read_csv,
                   request_with_backoff, save_csv)

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
    from . import browser_sign_in  # imported here: browser_sign_in imports this module
    return browser_sign_in.browser_fetch(url, control, log)


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
    from . import browser_sign_in  # imported here: browser_sign_in imports this module
    if host in _gave_up:
        raise AccessDenied(url, reason="the browser could not get past this site earlier in the run")
    if not browser_sign_in.available():
        raise AccessDenied(url, reason="a browser is needed; " + browser_sign_in.INSTALL_HINT)
    if fallback:
        log(f"    {host} refuses plain requests; trying through the browser")
    throttle(host, MODE_BROWSER, control)
    try:
        return browser_sign_in.browser_fetch(url, control, log)
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


# --------------------------------------------------------------------------- downloading the images in a dataset

BLOCKED_AFTER = 3  # skip a site for the rest of the run after this many refusals in a row
FAILING_AFTER = 5  # ...or after this many other failures in a row
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff")


def _has_image_suffix(url):
    return urlparse(url).path.lower().endswith(IMAGE_SUFFIXES)


def _extract_image_from_json(json_text, log=print):
    try:
        data = json.loads(json_text)
    except (json.JSONDecodeError, TypeError) as exc:
        log(f"    IIIF JSON parse failed: {exc}")
        return None
    if not isinstance(data, dict):
        return None

    candidates = []
    for canvas in data.get("items", []):
        for page in canvas.get("items", []):
            for annotation in page.get("items", []):
                body = annotation.get("body", {})
                body = body[0] if isinstance(body, list) and body else body
                if isinstance(body, dict):
                    candidates.append(body.get("id") or body.get("@id"))
    for sequence in data.get("sequences", []):
        for canvas in sequence.get("canvases", []):
            for image in canvas.get("images", []):
                resource = image.get("resource", {})
                candidates.append(resource.get("@id") or resource.get("id"))
    candidates += [data.get(key) for key in ("imageUri", "imageUrl", "identifier")]
    return next((c for c in candidates if isinstance(c, str) and c.startswith("http")), None)


def _resolve_image_from_html(resp, page_url, log=print):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise ValueError("HTML page returned; install beautifulsoup4 to find images in web pages")
    soup = BeautifulSoup(resp.text, "html.parser")

    meta = (soup.find("meta", property="og:image")
            or soup.find("meta", attrs={"name": "twitter:image"})
            or soup.find("link", rel="image_src"))
    href = meta and (meta.get("content") or meta.get("href"))
    if href and href.strip():
        return urljoin(page_url, href.strip())

    manifest = soup.find("link", rel="alternate", type="application/ld+json")
    if manifest and manifest.get("href"):
        try:
            manifest_resp = request_with_backoff(urljoin(page_url, manifest["href"].strip()), log=log)
            image = _extract_image_from_json(manifest_resp.text, log)
            if image:
                return image
        except Exception as exc:
            log(f"    IIIF manifest failed: {exc}")

    for tag in soup.find_all("img", src=True):
        src = urljoin(page_url, tag["src"].strip())
        if not _has_image_suffix(src):
            continue
        try:
            if int(tag.get("width", 999)) < 100 or int(tag.get("height", 999)) < 100:
                continue
        except (TypeError, ValueError):
            pass
        return src

    for tag in soup.find_all("a", href=True):
        link = urljoin(page_url, tag["href"].strip())
        if _has_image_suffix(link):
            return link
    return None


def _detect_extension(content_type, url=""):
    content_type = content_type.lower()
    for key, ext in (("jpeg", "jpg"), ("jpg", "jpg"), ("png", "png"), ("tif", "tif"), ("gif", "gif"), ("webp", "webp")):
        if key in content_type:
            return ext
    path = urlparse(url).path.lower()
    for ext in ("jpg", "jpeg", "png", "tif", "tiff", "gif", "webp"):
        if path.endswith(f".{ext}"):
            return {"jpeg": "jpg", "tiff": "tif"}.get(ext, ext)
    return "jpg"


def _fetch_image(url, stem, log=print, control=None):
    special = download_special(url, stem, log, control)
    if special:
        return special
    resp = request_with_backoff(url, log=log, control=control, stream=True)
    content_type = resp.headers.get("Content-Type", "").lower()
    if "json" in content_type:
        image_url = _extract_image_from_json(resp.text, log)
        if not image_url:
            raise ValueError("JSON response: no image URL found")
        log(f"    IIIF image: {image_url[:80]}")
        resp = request_with_backoff(image_url, log=log, control=control, stream=True)
    elif "text/html" in content_type:
        image_url = _resolve_image_from_html(resp, url, log)
        if not image_url:
            if looks_blocked(resp.text):
                raise AccessDenied(url, reason="the site showed a sign-in or verification page")
            raise ValueError("HTML page has no recognizable image (it may need JavaScript)")
        log(f"    Image from page: {image_url[:80]}")
        resp = request_with_backoff(image_url, log=log, control=control, stream=True)

    content_type = resp.headers.get("Content-Type", "").lower()
    if "text/html" in content_type or "json" in content_type:
        resp.close()
        raise ValueError(f"Final response is not an image (Content-Type: {content_type})")
    path = Path(f"{stem}.{_detect_extension(content_type, resp.url)}")
    partial = path.with_name(path.name + ".part")
    try:
        with resp, open(partial, "wb") as out:
            for chunk in resp.iter_content(1 << 16):
                check(control)
                out.write(chunk)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)
    return path


def _replace_old_versions(path):
    for old in path.parent.glob(f"{path.stem}.*"):
        if old != path and old.suffix.lower() in IMAGE_EXTENSIONS:
            old.unlink(missing_ok=True)


def media_status(csv_path, media_dir, log=print):
    df = read_csv(csv_path, log)
    ids = gbif_ids(df)
    images = index_images(media_dir)
    errors = df["media_error"] if "media_error" in df.columns else pd.Series("", index=df.index)
    items = {}
    for idx, gbif_id in ids.items():
        if not gbif_id or gbif_id in items:
            continue
        url = find_media_url(df.loc[idx]) or ""
        if gbif_id in images:
            status, detail = "Downloaded", images[gbif_id].name
        elif not url:
            status, detail = "No link", ""
        elif not is_blank(errors[idx]):
            status, detail = "Failed", str(errors[idx])
        else:
            status, detail = "Not downloaded", url
        items[gbif_id] = {"id": gbif_id, "status": status, "detail": detail, "image": images.get(gbif_id)}
    return list(items.values())


def download_media(csv_path, media_dir, log=print, control=None, progress=None, only_ids=None, force=False):
    from . import browser_sign_in  # imported here: browser_sign_in imports this module

    new_run()
    try:
        return _download_media(csv_path, media_dir, log, control, progress, only_ids, force)
    finally:
        browser_sign_in.close_fetcher()


def _download_media(csv_path, media_dir, log, control, progress, only_ids, force):
    csv_path = Path(csv_path)
    media_dir = Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    failed_csv = csv_path.with_name(csv_path.stem + "_failed_media.csv")

    df = read_csv(csv_path, log).reset_index(drop=True)
    df.columns = df.columns.str.strip()
    for column in ("media_path", "media_error"):
        df[column] = df[column].astype("object").fillna("") if column in df.columns else ""
        df[column] = df[column].astype("object")
    ids = gbif_ids(df)
    repeat = ids.groupby(ids).cumcount()
    rows = [idx for idx in df.index if ids[idx] and (only_ids is None or ids[idx] in only_ids)]
    images = index_images(media_dir)

    summary = {"total": len(rows), "downloaded": 0, "skipped": 0, "skipped_by_user": 0, "failed": 0,
               "cancelled": False, "blocked": {}, "skipped_sites": {}, "failing": {}}
    denied_in_a_row = {}
    failed_in_a_row = {}
    scope = f"{len(rows)} selected records" if only_ids is not None else f"{len(rows)} records"
    log(f"Downloading images for {scope} to {media_dir}")

    for number, idx in enumerate(rows, start=1):
        if control and control.cancelled:
            summary["cancelled"] = True
            break
        if control:
            control.new_item()
        if progress:
            progress(number - 1, len(rows))
        if number % 25 == 0:
            save_csv(df, csv_path)

        label = ids[idx] if repeat[idx] == 0 else f"{ids[idx]}_{repeat[idx]}"
        existing = images.get(label)
        if existing and not force:
            df.at[idx, "media_path"] = str(existing)
            df.at[idx, "media_error"] = ""
            summary["skipped"] += 1
            continue

        url = find_media_url(df.loc[idx])
        if not url:
            df.at[idx, "media_error"] = "No media link found in row"
            summary["failed"] += 1
            continue
        host = host_of(url)
        if denied_in_a_row.get(host, 0) >= BLOCKED_AFTER:
            df.at[idx, "media_error"] = f"Access denied by {host}; skipped after repeated refusals"
            summary["blocked"].setdefault(host, []).append(ids[idx])
            summary["failed"] += 1
            continue
        if control and host in control.skipped_hosts:
            df.at[idx, "media_error"] = f"Site skipped by you: {host}"
            summary["skipped_sites"].setdefault(host, []).append(ids[idx])
            summary["failed"] += 1
            continue
        if failed_in_a_row.get(host, 0) >= FAILING_AFTER:
            df.at[idx, "media_error"] = f"Site {host} skipped after {FAILING_AFTER} failures in a row"
            summary["failing"].setdefault(host, []).append(ids[idx])
            summary["failed"] += 1
            continue
        log(f"[{number}/{len(rows)}] {label}: {url[:80]}")
        try:
            path = _fetch_image(url, media_dir / label, log, control)
            _replace_old_versions(path)
            df.at[idx, "media_path"] = str(path)
            df.at[idx, "media_error"] = ""
            summary["downloaded"] += 1
            denied_in_a_row.pop(host, None)
            failed_in_a_row.pop(host, None)
        except ItemSkipped:
            if control and control.cancelled:
                summary["cancelled"] = True
                break
            if control and control.skip_site:
                control.skipped_hosts.add(host)
                log(f"    Skipped {label} and the rest of {host}")
                df.at[idx, "media_error"] = f"Site skipped by you: {host}"
                summary["skipped_sites"].setdefault(host, []).append(ids[idx])
                summary["failed"] += 1
            else:
                log(f"    Skipped {label}")
                summary["skipped_by_user"] += 1
        except AccessDenied as exc:
            log(f"    Failed: {exc}")
            df.at[idx, "media_error"] = str(exc)
            if host not in denied_in_a_row:
                note_refused(host)
            denied_in_a_row[host] = denied_in_a_row.get(host, 0) + 1
            summary["blocked"].setdefault(host, []).append(ids[idx])
            summary["failed"] += 1
        except Exception as exc:
            log(f"    Failed: {exc}")
            df.at[idx, "media_error"] = str(exc)
            summary["failed"] += 1
            failed_in_a_row[host] = failed_in_a_row.get(host, 0) + 1
            if failed_in_a_row[host] == FAILING_AFTER:
                log(f"    {host} failed {FAILING_AFTER} times in a row; skipping it for the rest of this run")
                summary["failing"].setdefault(host, []).append(ids[idx])

    save_csv(df, csv_path)
    failed = df[df["media_error"].astype(str).str.strip() != ""]
    if len(failed):
        save_csv(failed, failed_csv)
        log(f"{len(failed)} records without an image are listed in {failed_csv}")
    elif failed_csv.exists():
        failed_csv.unlink()

    if summary["cancelled"]:
        log("Image download cancelled")
    elif progress:
        progress(len(rows), len(rows))
    for host, skipped_ids in summary["skipped_sites"].items():
        note_refused(host, "skipped by you")
        log(f"{host} was skipped by you for {len(skipped_ids)} images. It is listed under Images, Site logins.")
    for host, failing_ids in summary["failing"].items():
        note_refused(host, f"failed {FAILING_AFTER} times in a row")
        log(f"{host} kept failing and was skipped ({len(failing_ids)} images). It is listed under Images, Site logins.")
    for host, blocked_ids in summary["blocked"].items():
        log(f"{host} refused access for {len(blocked_ids)} images. Use Images, Site logins to sign in, then retry.")
    log(f"Image download complete: {summary['downloaded']} downloaded, {summary['skipped']} already present, "
        f"{summary['skipped_by_user']} skipped by you, {summary['failed']} failed")
    return summary
