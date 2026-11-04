"""One isolated folder per GBIF download: data/<download id>_<Species_name>/."""
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from config import DATA_DIR, HEADERS, RESPONSES_DIR_NAME
from .utils import extract_archive, find_dwca_files

INFO_FILE = "workspace.json"
INSPECT_NAME = "GBIFdownload_inspectFlags.csv"
FULL_MASTER_NAME = "master_cleaned_all_columns.csv"
SPECIES_COLUMNS = ("species", "scientificName")
TAXON_KEYS = {"TAXON_KEY", "SPECIES_KEY", "GENUS_KEY", "ACCEPTED_TAXON_KEY"}


def safe_name(text):
    return re.sub(r"[^\w.+-]+", "_", str(text or "").strip()).strip("_.")


def folder_name(download_id, species=""):
    download_id, species = safe_name(download_id), safe_name(species)
    return f"{download_id}_{species}" if species else download_id


class Workspace:
    def __init__(self, root):
        self.root = Path(root)

    def __eq__(self, other):
        return isinstance(other, Workspace) and self.root == other.root

    def __hash__(self):
        return hash(self.root)

    @property
    def name(self):
        return self.root.name

    raw_dir = property(lambda self: self.root / "raw")
    inspect_csv = property(lambda self: self.root / INSPECT_NAME)
    removed_csv = property(lambda self: self.root / "GBIFdownload_removed.csv")
    missing_csv = property(lambda self: self.root / "Missing_Media.csv")
    master_csv = property(lambda self: self.root / "master_cleaned.csv")
    full_master_csv = property(lambda self: self.root / FULL_MASTER_NAME)
    removed_columns_csv = property(lambda self: self.root / "removed_columns.csv")
    duplicates_csv = property(lambda self: self.root / "removed_duplicates.csv")
    measurements_csv = property(lambda self: self.root / "measurements.csv")
    media_dir = property(lambda self: self.root / "media")
    responses_dir = property(lambda self: self.root / RESPONSES_DIR_NAME)

    @property
    def info(self):
        try:
            data = json.loads((self.root / INFO_FILE).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def update_info(self, **changes):
        info = {**self.info, **{k: v for k, v in changes.items() if v not in (None, "")}}
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / INFO_FILE).write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")

    @property
    def download_id(self):
        return self.info.get("download_id") or self.name

    @property
    def species(self):
        return self.info.get("species", "")

    @property
    def title(self):
        return f"{self.download_id}  ({self.species})" if self.species else self.download_id

    def csv_files(self):
        return sorted(self.root.glob("*.csv"))

    def rename_species(self, species):
        target = self.root.with_name(folder_name(self.download_id, species))
        if target == self.root or target.exists():
            self.update_info(species=species)
            return self
        self.root.rename(target)
        renamed = Workspace(target)
        renamed.update_info(species=species)
        return renamed


def _is_workspace(path):
    return path.is_dir() and (path / INFO_FILE).is_file()


def list_workspaces():
    if not DATA_DIR.is_dir():
        return []
    found = [Workspace(p) for p in DATA_DIR.iterdir() if _is_workspace(p)]
    return sorted(found, key=lambda w: (w.info.get("created", ""), w.name), reverse=True)


def find_workspace(download_id):
    key = safe_name(download_id)
    for ws in list_workspaces():
        if ws.download_id == key or ws.name == key or ws.name.startswith(key + "_"):
            return ws
    return None


def workspace_by_name(name):
    path = DATA_DIR / str(name or "")
    return Workspace(path) if name and _is_workspace(path) else None


def workspace_of(path):
    """The workspace that contains `path`, or None."""
    try:
        relative = Path(path).resolve().relative_to(DATA_DIR.resolve())
    except (ValueError, OSError):
        return None
    if not relative.parts:
        return None
    candidate = DATA_DIR / relative.parts[0]
    return Workspace(candidate) if _is_workspace(candidate) else None


def create_workspace(download_id, species="", **info):
    """The workspace for a download, created on first use and reused afterwards."""
    download_id = safe_name(download_id)
    if not download_id:
        raise ValueError("A download ID is needed to create a data folder")
    ws = find_workspace(download_id)
    if ws is None:
        ws = Workspace(DATA_DIR / folder_name(download_id, species))
        ws.update_info(download_id=download_id, created=datetime.now().isoformat(timespec="seconds"))
    ws.update_info(**info)
    if species and not ws.species:
        ws = ws.rename_species(species)
    return ws


def _taxon_keys(node):
    keys = []
    if isinstance(node, dict):
        if node.get("key") in TAXON_KEYS:
            keys += [str(v) for v in ([node["value"]] if "value" in node else node.get("values", []))]
        for value in node.values():
            keys += _taxon_keys(value)
    elif isinstance(node, list):
        for value in node:
            keys += _taxon_keys(value)
    return list(dict.fromkeys(keys))


def species_for_download(download_key, log=print):
    """Best effort: the taxon names a GBIF download was requested for."""
    try:
        meta = requests.get(f"https://api.gbif.org/v1/occurrence/download/{download_key}", timeout=20,
                            headers=HEADERS)
        meta.raise_for_status()
        names = []
        for key in _taxon_keys(meta.json().get("request", {}))[:2]:
            taxon = requests.get(f"https://api.gbif.org/v1/species/{key}", timeout=20, headers=HEADERS).json()
            name = taxon.get("canonicalName") or taxon.get("scientificName")
            if name:
                names.append(name)
        return " + ".join(names)
    except (requests.RequestException, ValueError) as exc:
        log(f"Could not look up the species for download {download_key}: {exc}")
        return ""


def species_from_occurrences(folder):
    """The most common species name in a downloaded occurrence file, or ''."""
    try:
        occurrence = find_dwca_files(folder)[0]
        df = pd.read_csv(occurrence, sep="\t", usecols=lambda c: c in SPECIES_COLUMNS, quoting=csv.QUOTE_NONE,
                         dtype=str, nrows=20000)
    except (OSError, ValueError, FileNotFoundError, pd.errors.EmptyDataError):
        return ""
    for column in SPECIES_COLUMNS:
        if column in df.columns:
            counts = Counter(df[column].dropna().str.strip())
            counts.pop("", None)
            if counts:
                return counts.most_common(1)[0][0]
    return ""


def resolve_input(path, log=print, create=True):
    """Turn a folder or ZIP chosen by the user into (folder with the data, its workspace).

    A ZIP is extracted into its workspace's raw/ folder. The workspace is None only for an outside
    folder when `create` is False."""
    if not str(path).strip():
        raise ValueError("Select a data folder or ZIP file")
    path = Path(path)
    if path.is_file() and path.suffix.lower() == ".zip":
        ws = workspace_of(path) or create_workspace(path.stem, source=str(path))
        if not any(ws.raw_dir.rglob("occurrence.txt")):
            extract_archive(path, ws.raw_dir, log)
        if not ws.species:
            ws = ws.rename_species(species_from_occurrences(ws.raw_dir))
        return str(ws.raw_dir), ws
    if not path.is_dir():
        raise ValueError(f"Not a folder or ZIP file: {path}")
    ws = workspace_of(path)
    if ws:
        return str(ws.raw_dir if path == ws.root and ws.raw_dir.is_dir() else path), ws
    if not create:
        return str(path), None
    ws = create_workspace(path.name, species_from_occurrences(path), source=str(path))
    return str(path), ws
