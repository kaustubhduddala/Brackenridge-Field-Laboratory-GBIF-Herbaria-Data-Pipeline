import base64
import io
import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from openai import APIConnectionError, BadRequestError, NotFoundError, OpenAI

from program.config import (DEFAULT_TOKEN_LIMIT, LMSTUDIO_MODEL, LMSTUDIO_URL, MASTER_CSV, MAX_IMAGE_SIDE,
                    MEASUREMENTS_CSV, MEDIA_DIR, MIN_CONTEXT_LENGTH, REQUEST_TIMEOUT_S)
from program.utils import ItemSkipped, check, gbif_ids, index_images, is_blank, read_csv, save_csv

TRAITS = ("panicle_length", "leaf_width", "seed_length")
FIELDS = [f"{trait}_{n}_{kind}" for trait in TRAITS for n in (1, 2) for kind in ("cm", "confidence")]
VOUCHER_FIELDS = ["catalogNumber", "recordedBy", "recordNumber", "eventDate", "country", "stateProvince",
                  "county", "locality", "habitat", "verbatimElevation", "identifiedBy", "scientificName"]

RESULT_COLUMNS = (
    ["ai_image"]
    + [f"ai_{field}" for field in FIELDS]
    + [f"ai_{trait}_mean_cm" for trait in TRAITS]
    + ["ai_calibration", "ai_notes", "voucher_text"]
    + [f"voucher_{field}" for field in VOUCHER_FIELDS]
    + ["ai_model", "ai_measured_at", "ai_error"]
)
MEASUREMENT_FILE_COLUMNS = ["gbifID", *RESULT_COLUMNS]

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "calibration": {"type": "string"},
        **{field: {"type": ["number", "null"]} for field in FIELDS},
        "notes": {"type": "string"},
        "voucher_text": {"type": "string"},
        "voucher": {
            "type": "object",
            "properties": {field: {"type": "string"} for field in VOUCHER_FIELDS},
            "required": VOUCHER_FIELDS,
            "additionalProperties": False,
        },
    },
    "required": ["calibration", *FIELDS, "notes", "voucher_text", "voucher"],
    "additionalProperties": False,
}
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "specimen_measurements", "strict": True, "schema": RESPONSE_SCHEMA},
}

JSON_SHAPE = json.dumps({
    "calibration": "string",
    **{field: "number or null" for field in FIELDS},
    "notes": "string",
    "voucher_text": "string",
    "voucher": {field: "string" for field in VOUCHER_FIELDS},
}, indent=2)

SYSTEM_PROMPT = """You are a meticulous botanical measurement assistant analyzing one herbarium
specimen image of Guinea grass (Megathyrsus maximus).

Calibration
- Use the ruler or scale bar if visible. It may not look like a standard ruler; any scale with
  visible tick marks can be used.
- If there is no usable scale but the full herbarium sheet is visible, use 29 cm as the sheet
  width only as a fallback.
- If neither is possible, return null measurements and explain why in notes. Never invent
  measurements.
- Say in one sentence how you calibrated in the calibration field.

Measurements
Return two independent measurements per trait when possible, from different structures: two
panicles, two leaves, two seeds. If only one valid structure is visible, fill measurement 1 and
set measurement 2 to null.
- panicle_length: from the lowest panicle node to the panicle tip.
- leaf_width: the maximum margin-to-margin width of a leaf at its broadest point.
- seed_length: from the branch point of an individual seed to its tip.
Use centimetres rounded to two decimals. If a trait is absent, obscured or cannot be calibrated,
use null for both its value and confidence. If the specimen has flowers but no visible seeds,
seed length is null. Confidence (0 to 1) must reflect visibility, calibration quality and
endpoint clarity, not certainty created by guessing.

Voucher label
Transcribe any readable label or voucher text into voucher_text, and copy matching details into
the voucher fields (Darwin Core terms). Write eventDate as YYYY-MM-DD when the full date is
readable, otherwise as written. Use an empty string for anything you cannot read.

Keep your reasoning brief: estimate each endpoint once against the scale and move on.
Respond with only this JSON object:
""" + JSON_SHAPE

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _image_data_url(image_path):
    path = Path(image_path)
    suffix = path.suffix.lower()
    try:
        from PIL import Image
    except ImportError:
        if suffix in {".tif", ".tiff"}:
            raise ValueError("TIFF images need Pillow to be converted (pip install pillow)")
        mime = "jpeg" if suffix in {".jpg", ".jpeg"} else suffix[1:]
        return f"data:image/{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as image:
        image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def _parse_json(content):
    text = re.sub(r"<think>.*?</think>", "", str(content or ""), flags=re.DOTALL | re.IGNORECASE)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    if not text.strip():
        raise ValueError("Model returned an empty response")

    decoder = json.JSONDecoder()
    found = None
    for match in re.finditer(r"\{", text):
        try:
            payload, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and any(field in payload for field in FIELDS):
            found = payload
    if found is None:
        raise ValueError("No valid JSON object found in model response")
    return found


def _to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER.search(str(value or ""))
    return float(match.group()) if match else None


def _as_text(value):
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalise_result(result):
    output = {}
    for field in FIELDS:
        value = _to_number(result.get(field))
        if value is not None and field.endswith("_confidence"):
            value = min(max(value, 0.0), 1.0)
        elif value is not None and value <= 0:
            value = None
        output[field] = None if value is None else round(value, 2)
    for key in ("calibration", "notes", "voucher_text"):
        output[key] = _as_text(result.get(key))
    voucher = result.get("voucher") if isinstance(result.get("voucher"), dict) else {}
    output["voucher"] = {field: _as_text(voucher.get(field)) for field in VOUCHER_FIELDS}
    return output


def _format_number(value):
    return "" if value is None else f"{value:.2f}"


def _result_columns(result, model, image):
    columns = {"ai_image": image.name, **{f"ai_{field}": _format_number(result[field]) for field in FIELDS}}
    for trait in TRAITS:
        values = [result[f"{trait}_{n}_cm"] for n in (1, 2) if result[f"{trait}_{n}_cm"] is not None]
        columns[f"ai_{trait}_mean_cm"] = _format_number(sum(values) / len(values) if values else None)
    columns.update({
        "ai_calibration": result["calibration"],
        "ai_notes": result["notes"],
        "voucher_text": result["voucher_text"],
        **{f"voucher_{field}": value for field, value in result["voucher"].items()},
        "ai_model": model,
        "ai_measured_at": _now(),
        "ai_error": "",
    })
    return columns


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _extra(obj, name):
    value = getattr(obj, name, None)
    if value is None:
        value = (getattr(obj, "model_extra", None) or {}).get(name)
    return value


def _api_root(base_url):
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def find_loaded_model(base_url=LMSTUDIO_URL):
    if LMSTUDIO_MODEL:
        return {"id": LMSTUDIO_MODEL, "type": "", "context": None}
    try:
        resp = requests.get(f"{_api_root(base_url)}/api/v0/models", timeout=10)
        resp.raise_for_status()
        models = resp.json().get("data", [])
    except (requests.RequestException, ValueError):
        models = None

    if models is not None:
        loaded = [m for m in models if m.get("state") == "loaded" and m.get("type") in ("vlm", "llm")]
        if not loaded:
            raise RuntimeError("LM Studio is running but no model is loaded. Load a vision model in LM Studio first.")
        best = next((m for m in loaded if m.get("type") == "vlm"), loaded[0])
        return {"id": best["id"], "type": best.get("type", ""), "context": best.get("loaded_context_length")}

    try:
        resp = requests.get(f"{base_url.rstrip('/')}/models", timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Could not reach LM Studio at {base_url}: {exc}") from exc
    if not data:
        raise RuntimeError("The server reports no models. Load a vision model in LM Studio first.")
    return {"id": data[0]["id"], "type": "", "context": None}


def model_warnings(model):
    warnings = []
    if model["type"] == "llm":
        warnings.append(f"{model['id']} is a text-only model; load a vision model to measure images.")
    if model["context"] and model["context"] < MIN_CONTEXT_LENGTH:
        warnings.append(f"{model['id']} is loaded with a {model['context']}-token context. Reload it in LM Studio "
                        f"with at least {MIN_CONTEXT_LENGTH} so the image, prompt and answer fit.")
    return warnings


class VisionModel:
    def __init__(self, base_url=LMSTUDIO_URL, log=print, token_limit=DEFAULT_TOKEN_LIMIT, reasoning_effort=None):
        self.client = OpenAI(base_url=base_url, api_key="lm-studio", timeout=REQUEST_TIMEOUT_S)
        self.base_url = base_url
        self.log = log
        self.token_limit = token_limit
        self.reasoning_effort = reasoning_effort
        self.structured = True
        self.model = find_loaded_model(base_url)
        for warning in model_warnings(self.model):
            log(f"Warning: {warning}")

    def _request(self, image_path):
        request = {
            "model": self.model["id"],
            "temperature": 0.1,
            "stream": True,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": "Measure this specimen."},
                    {"type": "image_url", "image_url": {"url": _image_data_url(image_path)}},
                ]},
            ],
        }
        if self.token_limit:
            request["max_tokens"] = int(self.token_limit)
        if self.reasoning_effort:
            request["reasoning_effort"] = self.reasoning_effort
        if self.structured:
            request["response_format"] = RESPONSE_FORMAT
        return request

    def measure(self, image_path, control=None, status=None):
        request = self._request(image_path)
        while True:
            try:
                return self._stream(request, control, status)
            except BadRequestError as exc:
                if "reasoning_effort" in request:
                    request.pop("reasoning_effort")
                    self.reasoning_effort = None
                    self.log(f"    The server rejected the thinking setting ({exc}); continuing without it")
                elif "response_format" in request:
                    request.pop("response_format")
                    self.structured = False
                    self.log(f"    The server rejected structured output ({exc}); continuing with plain JSON prompting")
                else:
                    raise

    def _stream(self, request, control, status):
        content, reasoning = [], []
        finish_reason, model, tokens, reported = None, self.model["id"], 0, 0
        stream = self.client.chat.completions.create(**request)
        try:
            for chunk in stream:
                check(control)
                model = getattr(chunk, "model", None) or model
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta is not None:
                    if delta.content:
                        content.append(delta.content)
                        tokens += 1
                    thought = _extra(delta, "reasoning_content") or _extra(delta, "reasoning")
                    if thought:
                        reasoning.append(thought)
                        tokens += 1
                finish_reason = choice.finish_reason or finish_reason
                if status and tokens - reported >= 20:
                    reported = tokens
                    status(f"{'Writing answer' if content else 'Thinking'}, about {tokens} tokens")
        finally:
            stream.close()

        text = "".join(content)
        try:
            result = _normalise_result(_parse_json(text if text.strip() else "".join(reasoning)))
        except ValueError:
            if finish_reason == "length":
                raise ValueError(
                    f"The model stopped after about {tokens} tokens without finishing its answer. Raise the "
                    "token limit, raise the context length for this model in LM Studio, or set Thinking to Low or Off."
                )
            raise
        return result, model


def load_measurements(csv_path=MEASUREMENTS_CSV):
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    if "gbifID" not in df.columns:
        raise ValueError(f"{csv_path} has no gbifID column")
    return {str(r["gbifID"]).strip(): r for r in df.to_dict("records")}


def _save_measurements(rows, csv_path):
    df = pd.DataFrame(list(rows.values()))
    extra = [c for c in df.columns if c not in MEASUREMENT_FILE_COLUMNS]
    df.reindex(columns=MEASUREMENT_FILE_COLUMNS + extra).to_csv(csv_path, index=False)


def _is_done(row):
    return bool(row) and not is_blank(row.get("ai_measured_at")) and is_blank(row.get("ai_error"))


def _describe(row):
    parts = []
    for trait, label in zip(TRAITS, ("panicle", "leaf", "seed")):
        value = row.get(f"ai_{trait}_mean_cm")
        parts.append(f"{label} {value} cm" if not is_blank(value) else f"{label} -")
    return ", ".join(parts)


def measurement_status(media_dir=MEDIA_DIR, csv_path=MEASUREMENTS_CSV):
    rows = load_measurements(csv_path)
    items = []
    for gbif_id, image in index_images(media_dir).items():
        row = rows.get(gbif_id)
        if _is_done(row):
            status, detail = "Measured", _describe(row)
        elif row and not is_blank(row.get("ai_error")):
            status, detail = "Failed", str(row["ai_error"])
        else:
            status, detail = "Not measured", ""
        notes = "" if not row else "\n".join(
            str(row.get(k)) for k in ("ai_calibration", "ai_notes", "voucher_text") if not is_blank(row.get(k)))
        items.append({"id": gbif_id, "status": status, "detail": detail, "image": image, "notes": notes})
    return items


def check_connection(base_url=LMSTUDIO_URL):
    model = find_loaded_model(base_url)
    context = f", {model['context']}-token context" if model["context"] else ""
    lines = [f"Connected. Measurements will use {model['id']}{context}."]
    lines += [f"Warning: {w}" for w in model_warnings(model)]
    return "\n".join(lines)


def measure_images(media_dir=MEDIA_DIR, csv_path=MEASUREMENTS_CSV, log=print, control=None, progress=None,
                   status=None, base_url=LMSTUDIO_URL, token_limit=DEFAULT_TOKEN_LIMIT, reasoning_effort=None,
                   only_ids=None, force=False):
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    images = index_images(media_dir)
    rows = load_measurements(csv_path)
    targets = [(gbif_id, path) for gbif_id, path in images.items() if only_ids is None or gbif_id in only_ids]
    summary = {"total": len(targets), "measured": 0, "skipped": 0, "skipped_by_user": 0, "failed": 0,
               "cancelled": False}
    if not images:
        log(f"No downloaded images found in {media_dir}. Download images first.")
        return summary

    vision = VisionModel(base_url, log, token_limit, reasoning_effort)
    scope = "selected " if only_ids is not None else ""
    log(f"Measuring {len(targets)} {scope}images from {media_dir} with {vision.model['id']}")

    for number, (gbif_id, image) in enumerate(targets, start=1):
        if control and control.cancelled:
            summary["cancelled"] = True
            break
        if control:
            control.new_item()
        if progress:
            progress(number - 1, len(targets))
        if _is_done(rows.get(gbif_id)) and not force:
            summary["skipped"] += 1
            continue

        log(f"[{number}/{len(targets)}] Measuring {image.name}")
        started = datetime.now()
        try:
            result, model = vision.measure(image, control, status)
            rows[gbif_id] = {"gbifID": gbif_id, **_result_columns(result, model, image)}
            summary["measured"] += 1
            log(f"    Done in {(datetime.now() - started).seconds} s: {_describe(rows[gbif_id])}")
        except ItemSkipped:
            if control and control.cancelled:
                summary["cancelled"] = True
                break
            log(f"    Skipped {image.name}")
            summary["skipped_by_user"] += 1
            continue
        except (APIConnectionError, NotFoundError) as exc:
            raise RuntimeError(f"Could not reach LM Studio at {base_url}: {exc}. "
                               "Check that the server is running with a vision model loaded.") from exc
        except Exception as exc:
            previous = rows.get(gbif_id, {})
            rows[gbif_id] = {**previous, "gbifID": gbif_id, "ai_image": image.name, "ai_error": str(exc),
                             "ai_measured_at": _now()}
            summary["failed"] += 1
            log(f"    Failed: {exc}")
        _save_measurements(rows, csv_path)

    if summary["cancelled"]:
        log("Measurement cancelled")
    elif progress:
        progress(len(targets), len(targets))
    log(f"Measurement complete: {summary['measured']} measured, {summary['skipped']} already done, "
        f"{summary['skipped_by_user']} skipped by you, {summary['failed']} failed. Saved to {csv_path}")
    return summary


def join_measurements(master_csv=MASTER_CSV, measurements_csv=MEASUREMENTS_CSV, log=print, fill_blanks=False):
    master_csv, measurements_csv = Path(master_csv), Path(measurements_csv)
    if not measurements_csv.exists():
        raise FileNotFoundError(f"{measurements_csv} does not exist yet. Measure some images first.")
    master = read_csv(master_csv, log)
    ids = gbif_ids(master)
    measurements = read_csv(measurements_csv, log)
    measurements["gbifID"] = gbif_ids(measurements)
    measurements = measurements.drop_duplicates("gbifID", keep="last").set_index("gbifID")
    result_columns = [c for c in measurements.columns if c in RESULT_COLUMNS]

    previous_fills = (master["voucher_filled_columns"].fillna("") if "voucher_filled_columns" in master.columns
                      else pd.Series("", index=master.index))
    master = master.drop(columns=[c for c in RESULT_COLUMNS + ["voucher_filled_columns"] if c in master.columns])
    joined = measurements.reindex(ids.values)[result_columns]
    joined.index = master.index
    master = pd.concat([master, joined.astype("object")], axis=1)

    matched = ids.isin(measurements.index)
    fills = previous_fills.astype("object").copy()
    if fill_blanks:
        for idx in master.index[matched]:
            filled = [f for f in str(fills[idx]).split(", ") if f]
            for field in VOUCHER_FIELDS:
                value = master.at[idx, f"voucher_{field}"] if f"voucher_{field}" in master.columns else ""
                if field in master.columns and not is_blank(value) and is_blank(master.at[idx, field]):
                    master.at[idx, field] = value
                    filled.append(field)
            fills[idx] = ", ".join(dict.fromkeys(filled))
    master["voucher_filled_columns"] = fills

    backup = master_csv.with_name(master_csv.stem + "_before_join.csv")
    backup.write_bytes(master_csv.read_bytes())
    save_csv(master, master_csv)
    unmatched = sorted(set(measurements.index) - set(ids))
    if unmatched:
        preview = ", ".join(unmatched[:5]) + (" and more" if len(unmatched) > 5 else "")
        log(f"{len(unmatched)} measured gbifIDs are not in the dataset and were not joined: {preview}")
    log(f"Join complete: {int(matched.sum())} of {len(master)} records have measurements. "
        f"Saved to {master_csv} (previous version kept as {backup.name})")
    return {"matched": int(matched.sum()), "total": len(master), "unmatched": len(unmatched)}
