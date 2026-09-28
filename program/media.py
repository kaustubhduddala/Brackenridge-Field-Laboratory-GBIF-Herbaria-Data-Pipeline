import json
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd

from program.config import IMAGE_EXTENSIONS, MEDIA_DIR
from program.utils import (ItemSkipped, check, find_media_url, gbif_ids, index_images, is_blank, read_csv,
                   request_with_backoff, save_csv)

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


def media_status(csv_path, media_dir=MEDIA_DIR, log=print):
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


def download_media(csv_path, media_dir=None, log=print, control=None, progress=None, only_ids=None, force=False):
    csv_path = Path(csv_path)
    media_dir = Path(media_dir or MEDIA_DIR)
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
               "cancelled": False}
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
        log(f"[{number}/{len(rows)}] {label}: {url[:80]}")
        try:
            path = _fetch_image(url, media_dir / label, log, control)
            _replace_old_versions(path)
            df.at[idx, "media_path"] = str(path)
            df.at[idx, "media_error"] = ""
            summary["downloaded"] += 1
        except ItemSkipped:
            if control and control.cancelled:
                summary["cancelled"] = True
                break
            log(f"    Skipped {label}")
            summary["skipped_by_user"] += 1
        except Exception as exc:
            log(f"    Failed: {exc}")
            df.at[idx, "media_error"] = str(exc)
            summary["failed"] += 1

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
    log(f"Image download complete: {summary['downloaded']} downloaded, {summary['skipped']} already present, "
        f"{summary['skipped_by_user']} skipped by you, {summary['failed']} failed")
    return summary
