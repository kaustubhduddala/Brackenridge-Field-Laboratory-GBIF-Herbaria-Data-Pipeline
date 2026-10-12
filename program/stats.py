"""Summaries of any CSV for the Analysis tab. Every function takes and returns plain pandas objects."""
import numpy as np
import pandas as pd

from .modify import blank_mask

NUMERIC_SHARE = 0.9
PROFILE_COLUMNS = ["column", "type", "filled", "filled %", "blank", "unique", "min", "max", "mean", "median"]
GROUP_STATS = ("count", "mean", "median", "min", "max", "std", "sum")


def numbers(series):
    """The numeric values in a text column; anything that is not a number becomes NaN."""
    return pd.to_numeric(series.fillna("").astype(str).str.strip().replace("", np.nan), errors="coerce")


def is_numeric(series, share=NUMERIC_SHARE):
    filled = series.fillna("").astype(str).str.strip().ne("")
    return bool(filled.any()) and numbers(series).notna().sum() >= share * filled.sum()


def _short(value):
    return "" if pd.isna(value) else f"{value:.6g}"


def column_profile(df):
    blanks = blank_mask(df)
    rows = []
    for column in df.columns:
        filled = int(len(df) - blanks[column].sum())
        series = df[column][~blanks[column]]
        row = {"column": column, "type": "number" if is_numeric(df[column]) else "text", "filled": filled,
               "filled %": round(100 * filled / len(df), 1) if len(df) else 0.0,
               "blank": int(blanks[column].sum()), "unique": int(series.nunique()),
               "min": "", "max": "", "mean": "", "median": ""}
        if row["type"] == "number":
            values = numbers(df[column]).dropna()
            row.update({"min": _short(values.min()), "max": _short(values.max()),
                        "mean": _short(values.mean()), "median": _short(values.median())})
        rows.append(row)
    return pd.DataFrame(rows, columns=PROFILE_COLUMNS)


def overview(df, key=None):
    blanks = blank_mask(df)
    cells = df.size
    info = {"rows": len(df), "columns": len(df.columns),
            "blank cells %": round(100 * blanks.to_numpy().sum() / cells, 1) if cells else 0.0,
            "empty columns": int(blanks.all(axis=0).sum()) if len(df) else 0,
            "duplicate rows": int(df.duplicated().sum())}
    if key and key in df.columns:
        ids = df[key].fillna("").astype(str).str.strip()
        info[f"duplicate {key} values"] = int(ids[ids != ""].duplicated().sum())
        info[f"blank {key} values"] = int((ids == "").sum())
    return info


def value_counts(df, column, top=50, include_blank=True):
    text = df[column].fillna("").astype(str).str.strip()
    if not include_blank:
        text = text[text != ""]
    counts = text.replace("", "(blank)").value_counts()
    table = counts.rename_axis("value").reset_index(name="count")
    table["percent"] = (100 * table["count"] / max(len(text), 1)).round(2)
    return table.head(top) if top else table


def number_summary(series):
    values = numbers(series).dropna()
    if values.empty:
        return {}
    quartiles = values.quantile([0.25, 0.5, 0.75])
    return {"count": int(values.size), "min": values.min(), "25%": quartiles[0.25], "median": quartiles[0.5],
            "75%": quartiles[0.75], "max": values.max(), "mean": values.mean(),
            "std": values.std() if values.size > 1 else float("nan")}


def histogram(series, bins=20):
    values = numbers(series).dropna()
    if values.empty:
        return pd.DataFrame(columns=["range", "count"])
    counts, edges = np.histogram(values, bins=bins)
    labels = [f"{_short(lo)} to {_short(hi)}" for lo, hi in zip(edges[:-1], edges[1:])]
    return pd.DataFrame({"range": labels, "count": counts})


def group_summary(df, group_column, value_column, top=0):
    """Count, mean, median, min, max, std and sum of a numeric column for each group."""
    frame = pd.DataFrame({
        "group": df[group_column].fillna("").astype(str).str.strip().replace("", "(blank)"),
        "value": numbers(df[value_column]),
    })
    grouped = frame.groupby("group")["value"]
    table = grouped.agg(list(GROUP_STATS[1:])).join(grouped.size().rename("rows"))
    table.insert(0, "count", grouped.count())
    table = table.drop(columns="rows").sort_values("count", ascending=False).reset_index()
    table = table.rename(columns={"group": group_column})
    for column in GROUP_STATS[1:]:
        table[column] = table[column].round(4)
    return table.head(top) if top else table
