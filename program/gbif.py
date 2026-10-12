import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from pygbif import occurrences

from config import BASE_FILTERS, HEADERS, PRESETS, gbif_credentials
from .utils import extract_archive
from .workspace import create_workspace, species_for_download, species_from_occurrences


def resolve_species(scientific_name, log=print):
    log(f"Looking up GBIF taxon key for '{scientific_name}'")
    resp = requests.get("https://api.gbif.org/v1/species/match", params={"name": scientific_name}, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if result.get("matchType") in ("EXACT", "FUZZY") and "usageKey" in result:
        log(f"Matched '{result.get('scientificName', scientific_name)}' ({result['matchType'].lower()}), "
            f"taxon key {result['usageKey']}")
        return result["usageKey"]
    raise ValueError(f"No reliable GBIF match for '{scientific_name}'. GBIF response: {result}")


def check_gbif_login(user, password):
    resp = requests.get(f"https://api.gbif.org/v1/occurrence/download/user/{user}", params={"limit": 1},
                        auth=(user, password), timeout=20, headers=HEADERS)
    if resp.status_code in (401, 403):
        return False, "GBIF did not accept this username and password."
    resp.raise_for_status()
    return True, f"Signed in to GBIF as {user}."


def build_predicate(taxon_key, preset_name=None):
    filters = PRESETS.get(preset_name, {}).get("filters", BASE_FILTERS)
    clauses = [("TAXON_KEY", str(taxon_key)), *filters]
    return {"type": "and", "predicates": [{"type": "equals", "key": k, "value": v} for k, v in clauses]}


def wait_and_download(download_key, output_dir, log=print):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    meta = occurrences.download_meta(download_key)
    while meta["status"] in ("RUNNING", "PREPARING"):
        log(f"GBIF is preparing the file ({meta['status'].lower()}); checking again in 30 s")
        time.sleep(30)
        meta = occurrences.download_meta(download_key)
    if meta["status"] != "SUCCEEDED":
        raise RuntimeError(f"GBIF download failed with status {meta['status']}")
    log("File is ready; downloading ZIP")
    occurrences.download_get(download_key, path=str(output_dir))
    return output_dir / f"{download_key}.zip"


def open_download_folder(download_key, species="", log=print, **info):
    """The data folder for a download: data/<download id>_<Species_name>/."""
    species = species or species_for_download(download_key, log)
    ws = create_workspace(download_key, species, **info)
    log(f"Data folder for this download: {ws.root}")
    return ws


def download_from_doi_link(doi_text, log=print):
    doi_text = doi_text.strip()
    if doi_text.lower().startswith("doi:"):
        doi_text = doi_text[4:].strip()
    if not doi_text:
        raise ValueError("No DOI provided")
    url = doi_text if doi_text.startswith(("http://", "https://")) else "https://doi.org/" + doi_text

    log(f"Resolving {url}")
    with requests.get(url, allow_redirects=True, stream=True, timeout=30, headers=HEADERS) as resp:
        final_url = resp.url
        parsed = urlparse(final_url)
        if "gbif.org" not in parsed.netloc.lower():
            raise ValueError(f"DOI did not resolve to a GBIF download: {final_url}")
        content_type = resp.headers.get("content-type", "").lower()
        if final_url.lower().endswith(".zip") or content_type.startswith(("application/zip", "application/octet-stream")):
            name = Path(parsed.path).name or "gbif_download.zip"
            ws = open_download_folder(Path(name).stem, log=log, doi=doi_text, source="DOI")
            out_path = ws.root / name
            with open(out_path, "wb") as out_file:
                for chunk in resp.iter_content(1 << 16):
                    out_file.write(chunk)
            return out_path, ws

    match = re.search(r"/download/([^/?#&]+)", final_url)
    if match:
        ws = open_download_folder(match.group(1), log=log, doi=doi_text, source="DOI")
        return wait_and_download(match.group(1), ws.root, log), ws
    raise ValueError("DOI did not resolve to a downloadable ZIP or a GBIF download key")


def download_dataset(species="", doi_text="", preset_name=None, log=print):
    """Download into its own folder and return the folder holding the extracted data."""
    if doi_text:
        archive, ws = download_from_doi_link(doi_text, log=log)
    else:
        user, password, email, _ = gbif_credentials()
        if not (user and password and email):
            raise RuntimeError("GBIF credentials are missing. Add your account in the app under File, GBIF account, "
                               "or set GBIF_USER, GBIF_PASSWORD and GBIF_EMAIL.")
        taxon_key = resolve_species(species, log)
        log("Submitting download request to GBIF")
        result = occurrences.download(build_predicate(taxon_key, preset_name), format="DWCA",
                                      user=user, pwd=password, email=email)
        download_key = result[0] if isinstance(result, (tuple, list)) else result
        log(f"Download key {download_key}. GBIF usually takes 5 to 30 minutes.")
        ws = open_download_folder(download_key, species, log, source="GBIF request", preset=preset_name)
        archive = wait_and_download(download_key, ws.root, log)
    extract_archive(archive, ws.raw_dir, log)
    if not ws.species:
        ws = ws.rename_species(species_from_occurrences(ws.raw_dir))
    return str(ws.raw_dir)
