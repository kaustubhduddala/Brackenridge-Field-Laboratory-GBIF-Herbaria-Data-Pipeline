import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from pygbif import occurrences

from program.config import BASE_FILTERS, DATA_DIR, GBIF_EMAIL, GBIF_PASSWORD, GBIF_USER, HEADERS, PRESETS, has_gbif_credentials
from program.utils import extract_archive


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


def build_predicate(taxon_key, preset_name=None):
    filters = PRESETS.get(preset_name, {}).get("filters", BASE_FILTERS)
    clauses = [("TAXON_KEY", str(taxon_key)), *filters]
    return {"type": "and", "predicates": [{"type": "equals", "key": k, "value": v} for k, v in clauses]}


def wait_and_download(download_key, output_dir=DATA_DIR, log=print):
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


def download_from_doi_link(doi_text, output_dir=DATA_DIR, log=print):
    doi_text = doi_text.strip()
    if doi_text.lower().startswith("doi:"):
        doi_text = doi_text[4:].strip()
    if not doi_text:
        raise ValueError("No DOI provided")
    url = doi_text if doi_text.startswith(("http://", "https://")) else "https://doi.org/" + doi_text
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    log(f"Resolving {url}")
    with requests.get(url, allow_redirects=True, stream=True, timeout=30, headers=HEADERS) as resp:
        final_url = resp.url
        parsed = urlparse(final_url)
        if "gbif.org" not in parsed.netloc.lower():
            raise ValueError(f"DOI did not resolve to a GBIF download: {final_url}")
        content_type = resp.headers.get("content-type", "").lower()
        if final_url.lower().endswith(".zip") or content_type.startswith(("application/zip", "application/octet-stream")):
            out_path = output_dir / (Path(parsed.path).name or "gbif_download.zip")
            with open(out_path, "wb") as out_file:
                for chunk in resp.iter_content(1 << 16):
                    out_file.write(chunk)
            return out_path

    match = re.search(r"/download/([^/?#&]+)", final_url)
    if match:
        return wait_and_download(match.group(1), output_dir, log)
    raise ValueError("DOI did not resolve to a downloadable ZIP or a GBIF download key")


def download_dataset(species="", doi_text="", preset_name=None, log=print):
    if doi_text:
        archive = download_from_doi_link(doi_text, log=log)
    else:
        if not has_gbif_credentials():
            raise RuntimeError("GBIF credentials are missing. Set GBIF_USER, GBIF_PASSWORD and GBIF_EMAIL, "
                               "or add user, password and email to ~/credentials.json.")
        taxon_key = resolve_species(species, log)
        log("Submitting download request to GBIF")
        result = occurrences.download(build_predicate(taxon_key, preset_name), format="DWCA",
                                      user=GBIF_USER, pwd=GBIF_PASSWORD, email=GBIF_EMAIL)
        download_key = result[0] if isinstance(result, (tuple, list)) else result
        log(f"Download key {download_key}. GBIF usually takes 5 to 30 minutes.")
        archive = wait_and_download(download_key, log=log)
    return extract_archive(archive, DATA_DIR / Path(archive).stem, log)
