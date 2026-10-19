"""Edit any CSV: choose columns, join two files, filter rows, tidy up, combine and undo."""
import re
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

from .utils import read_csv, save_csv

BACKUP_DIR_NAME = "backups"
CONFLICT_CHOICES = {
    "Keep both (add _B to the second file's column)": "both",
    "Prefer file A, use B only where A is blank": "prefer_a",
    "Prefer file B, use A only where B is blank": "prefer_b",
}
JOIN_TYPES = {
    "Keep every row of A (left join)": "left",
    "Only rows found in both (inner join)": "inner",
    "Keep every row of B (right join)": "right",
    "Keep every row of both (outer join)": "outer",
}
DUPLICATE_CHOICES = {
    "Use the first matching row of B": "first",
    "Use the last matching row of B": "last",
    "Use every matching row (A rows are repeated)": "all",
}
FILTER_OPS = ("equals", "does not equal", "contains", "does not contain", "starts with", "ends with",
              "is blank", "is not blank", "is greater than", "is less than", "is at least", "is at most",
              "matches regex", "is one of (comma separated)")
NO_VALUE_OPS = ("is blank", "is not blank")
STAMP = "%Y%m%d-%H%M%S"


def load_table(path, log=print):
    df = read_csv(path, log)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def blank_mask(df):
    """True where a cell is empty or only spaces."""
    return pd.DataFrame({c: df[c].isna() | df[c].astype(str).str.strip().eq("") for c in df.columns},
                        index=df.index, columns=df.columns)


def filled_counts(df):
    blanks = blank_mask(df)
    return {c: int(len(df) - blanks[c].sum()) for c in df.columns}


def default_output(path, suffix):
    path = Path(path)
    return path.with_name(f"{path.stem}_{suffix}.csv")


def unique_output(path, suffix):
    """<name>_<suffix>.csv next to `path`, numbered if that name is already taken."""
    candidate = default_output(path, suffix)
    number = 2
    while candidate.exists():
        candidate = default_output(path, f"{suffix}_{number}")
        number += 1
    return candidate


def backup_dir(path):
    return Path(path).parent / BACKUP_DIR_NAME


def list_backups(path):
    path = Path(path)
    pattern = re.compile(rf"^{re.escape(path.stem)}_\d{{8}}-\d{{6}}\.csv$")
    folder = backup_dir(path)
    return sorted(p for p in folder.glob("*.csv") if pattern.match(p.name)) if folder.is_dir() else []


def write_result(df, source, output=None):
    """Save `df`. Overwriting `source` first keeps the old version in backups/ so it can be undone."""
    source = Path(source)
    output = Path(output) if output else source
    output.parent.mkdir(parents=True, exist_ok=True)
    note = ""
    if output.exists() and output.resolve() == source.resolve():
        folder = backup_dir(source)
        folder.mkdir(exist_ok=True)
        backup = folder / f"{source.stem}_{datetime.now().strftime(STAMP)}.csv"
        shutil.copy2(source, backup)
        note = f"previous version kept as {BACKUP_DIR_NAME}/{backup.name}"
    save_csv(df, output)
    return output, note


def restore_latest(path):
    backups = list_backups(path)
    if not backups:
        raise FileNotFoundError(f"There is no earlier version of {Path(path).name} to go back to")
    latest = backups[-1]
    shutil.copy2(latest, path)
    latest.unlink()
    return latest.name


def select_columns(df, columns, renames=None):
    """Keep `columns` in the given order, optionally renaming some of them."""
    columns = list(columns)
    if not columns:
        raise ValueError("Keep at least one column")
    out = df[columns].copy()
    renames = {k: v.strip() for k, v in (renames or {}).items() if k in columns and v.strip() and v.strip() != k}
    out = out.rename(columns=renames)
    repeated = sorted({c for c in out.columns if list(out.columns).count(c) > 1})
    if repeated:
        raise ValueError("Two columns would have the same name: " + ", ".join(repeated))
    return out


def _normalise_key(series, loose):
    keys = series.fillna("").astype(str).str.strip()
    return keys.str.lower().str.replace(r"\s+", " ", regex=True) if loose else keys


def count_matches(left, left_key, right, right_key, loose=True):
    left_keys = _normalise_key(left[left_key], loose)
    right_keys = set(_normalise_key(right[right_key], loose)) - {""}
    return int(left_keys.isin(right_keys).sum()), len(left_keys), len(right_keys)


def join_tables(left, right, left_key, right_key, how="left", left_columns=None, right_columns=None,
                conflict="both", duplicates="first", loose=True):
    """Join two tables on one column each. Returns (table, summary)."""
    for name, df, key in (("A", left, left_key), ("B", right, right_key)):
        if key not in df.columns:
            raise ValueError(f"File {name} has no column named {key}")
    left_columns = list(left.columns) if left_columns is None else [c for c in left.columns if c in set(left_columns)]
    if left_key not in left_columns:
        left_columns.insert(0, left_key)
    right_columns = [c for c in (right.columns if right_columns is None else right_columns)
                     if c in right.columns and c != right_key]
    shared = [c for c in right_columns if c in left_columns]

    a = left[left_columns].copy()
    b = right[[right_key, *right_columns]].copy()
    left_ids = _normalise_key(a[left_key], loose)
    right_ids = _normalise_key(b[right_key], loose)
    # blank keys must never match each other
    a["__key__"] = left_ids.where(left_ids != "", "__blank_a_" + pd.Series(range(len(a)), index=a.index).astype(str))
    b["__key__"] = right_ids.where(right_ids != "", "__blank_b_" + pd.Series(range(len(b)), index=b.index).astype(str))
    b = b.rename(columns={right_key: "__right_key__"})
    if duplicates in ("first", "last"):
        b = b.drop_duplicates("__key__", keep=duplicates)
    b = b.rename(columns={c: f"{c}__b" for c in shared})

    merged = a.merge(b, on="__key__", how=how, indicator=True)
    both = merged["_merge"].eq("both")
    right_only = merged["_merge"].eq("right_only")
    if right_only.any():
        merged.loc[right_only, left_key] = merged.loc[right_only, "__right_key__"]

    for column in shared:
        mine, theirs = merged[column], merged[f"{column}__b"]
        if conflict == "both":
            merged = merged.rename(columns={f"{column}__b": f"{column}_B"})
        elif conflict == "prefer_a":
            blank = mine.fillna("").astype(str).str.strip().eq("")
            merged[column] = mine.where(~blank, theirs)
            merged = merged.drop(columns=f"{column}__b")
        else:
            blank = theirs.fillna("").astype(str).str.strip().eq("")
            merged[column] = theirs.where(~blank, mine)
            merged = merged.drop(columns=f"{column}__b")

    added = [c for c in right_columns if c not in shared] + (
        [f"{c}_B" for c in shared] if conflict == "both" else [])
    result = merged[[*left_columns, *added]].reset_index(drop=True)
    unmatched_right = len(set(b["__key__"]) - set(a["__key__"]))
    matched_rows = int(both.sum())
    return result, {"matched": matched_rows, "left_rows": len(left), "right_rows": len(right),
                    "unmatched_right": unmatched_right, "rows": len(result), "columns": len(result.columns),
                    "shared": shared}


def row_mask(df, column, op, value="", case=False):
    if column not in df.columns:
        raise ValueError(f"There is no column named {column}")
    text = df[column].fillna("").astype(str).str.strip()
    needle = str(value).strip()
    if not case:
        text, needle = text.str.lower(), needle.lower()
    if op == "equals":
        return text.eq(needle)
    if op == "does not equal":
        return text.ne(needle)
    if op == "contains":
        return text.str.contains(needle, regex=False)
    if op == "does not contain":
        return ~text.str.contains(needle, regex=False)
    if op == "starts with":
        return text.str.startswith(needle)
    if op == "ends with":
        return text.str.endswith(needle)
    if op == "is blank":
        return text.eq("")
    if op == "is not blank":
        return text.ne("")
    if op == "matches regex":
        try:
            return text.str.contains(str(value), regex=True, flags=0 if case else re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"The pattern is not valid: {exc}") from exc
    if op == "is one of (comma separated)":
        wanted = {w.strip() if case else w.strip().lower() for w in str(value).split(",") if w.strip()}
        return text.isin(wanted)
    if op in ("is greater than", "is less than", "is at least", "is at most"):
        try:
            limit = float(needle)
        except ValueError as exc:
            raise ValueError(f"{value!r} is not a number") from exc
        numbers = pd.to_numeric(text, errors="coerce")
        return {"is greater than": numbers > limit, "is less than": numbers < limit,
                "is at least": numbers >= limit, "is at most": numbers <= limit}[op].fillna(False)
    raise ValueError(f"Unknown condition: {op}")


def filter_rows(df, column, op, value="", keep_matching=True, case=False):
    """Returns (kept rows, removed rows)."""
    mask = row_mask(df, column, op, value, case)
    keep = mask if keep_matching else ~mask
    return df[keep].reset_index(drop=True), df[~keep].reset_index(drop=True)


def drop_duplicates(df, subset=None, keep="first"):
    """Returns (unique rows, removed duplicates). An empty `subset` compares whole rows."""
    subset = [c for c in (subset or []) if c in df.columns] or None
    duplicate = df.duplicated(subset=subset, keep=keep)
    return df[~duplicate].reset_index(drop=True), df[duplicate].reset_index(drop=True)


def sort_rows(df, column, descending=False, numeric=False):
    if column not in df.columns:
        raise ValueError(f"There is no column named {column}")
    key = pd.to_numeric(df[column], errors="coerce") if numeric else df[column].fillna("").str.lower()
    return df.assign(__sort__=key).sort_values("__sort__", ascending=not descending, kind="stable",
                                               na_position="last").drop(columns="__sort__").reset_index(drop=True)


def replace_values(df, columns, find, replace, whole_cell=False, case=False, regex=False):
    """Find and replace in the chosen columns. Returns (table, number of cells changed)."""
    if find == "":
        raise ValueError("Enter the text to find")
    out, changed = df.copy(), 0
    flags = 0 if case else re.IGNORECASE
    pattern = find if regex else re.escape(find)
    if whole_cell:
        pattern = f"^(?:{pattern})$"
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"The pattern is not valid: {exc}") from exc
    for column in columns:
        before = out[column]
        after = before.where(before.isna(), before.fillna("").astype(str).map(lambda v: compiled.sub(replace, v)))
        changed += int((before.fillna("") != after.fillna("")).sum())
        out[column] = after
    return out, changed


def tidy(df, empty_columns=True, empty_rows=True, trim=True):
    """Trim spaces and drop columns and rows that are blank everywhere."""
    out, notes = df.copy(), {}
    if trim:
        out = out.apply(lambda s: s.str.strip() if hasattr(s, "str") else s)
    if empty_columns:
        blank = blank_mask(out).all(axis=0)
        notes["columns_dropped"] = list(out.columns[blank])
        out = out.loc[:, ~blank]
    if empty_rows:
        blank = blank_mask(out).all(axis=1)
        notes["rows_dropped"] = int(blank.sum())
        out = out[~blank].reset_index(drop=True)
    return out, notes


def combine(tables, source_column=None):
    """Stack tables (name, DataFrame) on top of each other, matching columns by name."""
    if len(tables) < 2:
        raise ValueError("Add at least two files to combine")
    frames = []
    for name, df in tables:
        frames.append(df.assign(**{source_column: name}) if source_column else df)
    out = pd.concat(frames, ignore_index=True, sort=False)
    if source_column:
        out = out[[c for c in out.columns if c != source_column] + [source_column]]
    return out
