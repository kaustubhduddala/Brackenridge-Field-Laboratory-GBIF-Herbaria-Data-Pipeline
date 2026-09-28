import base64
import io
import json
import os
import re
from pathlib import Path

import pandas as pd
from openai import APIConnectionError, NotFoundError, OpenAI

DATA_DIR = Path(__file__).resolve().parent / "data"
MEDIA_DIR = DATA_DIR / "media"
OUTPUT_CSV = DATA_DIR / "measurements.csv"
DEFAULT_BASE_URL = os.environ.get("LMSTUDIO_URL", "http://localhost:1234/v1")
DEFAULT_MODEL = os.environ.get("LMSTUDIO_MODEL", "Qwen3-VL-Thinking")
MAX_IMAGE_SIDE = 2560
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
TRAITS = ("panicle_length", "leaf_width", "seed_length")
FIELDS = [f"{trait}_{n}_{kind}" for trait in TRAITS for n in (1, 2) for kind in ("cm", "confidence")]
COLUMNS = ["gbifID", "image", *FIELDS, "notes", "agent_instructions", "error"]

JSON_SHAPE = (
    "{\n"
    + "".join(f'  "{field}": number|null,\n' for field in FIELDS)
    + '  "notes": "brief explanation",\n'
    + '  "agent_instructions": "instructions"\n}'
)

SYSTEM_PROMPT = """
You are a meticulous botanical measurement assistant analyzing one herbarium
specimen image of Guinea grass (Megathyrsus maximus).

Use the ruler or scale bar if visible. If there is no scale bar but the full
herbarium sheet is visible, use 29 cm as the sheet width only as a fallback.
If neither calibration method is available, return null measurements and say
why in notes. Do not invent measurements. The ruler may not look like a standard
ruler, but if it has visible tick marks, it can be used for calibration. If the
ruler is present but not visible enough to calibrate, return null measurements
and say why in notes.

Return TWO independent measurements for each trait whenever possible. Use
different visible structures: two panicles for panicle length, two different
leaves for leaf width, and two different seeds for seed length. If only one
valid structure is visible, fill measurement_1 and leave measurement_2 null.
For every measurement, provide a confidence score from 0 to 1.

Measurement definitions:
- panicle_length: from the lowest panicle node to the panicle tip.
- leaf_width: the maximum margin-to-margin width of a leaf at its broadest
  point.
- seed_length: from the branch point of an individual seed to its tip.

Use centimetres. Round measurements to exactly two decimal places. If a trait
is absent, obscured, or cannot be calibrated, do your best to take faithful
measurements, but if that is not possible, use null for both its value and
confidence. If the specimen has flowers but no visible seeds, seed length is
null. Confidence must reflect visibility, calibration quality, and endpoint
clarity, not certainty created by guessing.

Many images include vouchers with written information about the specimen.
If you can read any of this information, transcribe it into the
agent_instructions field. If you cannot read any of the text, leave
agent_instructions as an empty string and say so in notes. agent_instructions
should instruct another agent to take your transcribed text and add it to the
original data row where information is missing. The information may be
pertinent to any of these columns ("gbifID","media","accessRights","bibliographicCitation","language","license","modified","references","rightsHolder","type_occurrence","institutionID","collectionID","datasetID","institutionCode","collectionCode","datasetName","ownerInstitutionCode","basisOfRecord","informationWithheld","dataGeneralizations","dynamicProperties","occurrenceID","catalogNumber","recordNumber","recordedBy","recordedByID","individualCount","organismQuantity","organismQuantityType","sex","lifeStage","reproductiveCondition","establishmentMeans","degreeOfEstablishment","georeferenceVerificationStatus","occurrenceStatus","preparations","disposition","associatedReferences","associatedTaxa","otherCatalogNumbers","occurrenceRemarks","organismID","previousIdentifications","organismRemarks","materialSampleID","eventID","parentEventID","fieldNumber","eventDate","startDayOfYear","endDayOfYear","year","month","day","verbatimEventDate","habitat","samplingProtocol","fieldNotes","eventRemarks","locationID","higherGeography","continent","islandGroup","island","countryCode","stateProvince","county","municipality","locality","verbatimLocality","verbatimElevation","locationAccordingTo","locationRemarks","decimalLatitude","decimalLongitude","coordinateUncertaintyInMeters","coordinatePrecision","verbatimCoordinateSystem","verbatimSRS","georeferencedBy","georeferencedDate","georeferenceProtocol","georeferenceSources","georeferenceRemarks","identificationID","verbatimIdentification","identificationQualifier","typeStatus","identifiedBy","identifiedByID","dateIdentified","identificationReferences","identificationVerificationStatus","identificationRemarks","taxonID","scientificNameID","acceptedNameUsageID","parentNameUsageID","nameAccordingToID","namePublishedInID","taxonConceptID","scientificName","acceptedNameUsage","parentNameUsage","originalNameUsage","namePublishedIn","higherClassification","kingdom","phylum","class","order","family","genus","genericName","specificEpithet","infraspecificEpithet","taxonRank","verbatimTaxonRank","vernacularName","nomenclaturalCode","taxonomicStatus","nomenclaturalStatus","taxonRemarks","datasetKey","publishingCountry","lastInterpreted","elevation","elevationAccuracy","distanceFromCentroidInMeters","issue","mediaType","hasCoordinate","hasGeospatialIssues","taxonKey","acceptedTaxonKey","kingdomKey","phylumKey","classKey","orderKey","familyKey","genusKey","speciesKey","species","acceptedScientificName","verbatimScientificName","typifiedName","protocol","lastParsed","lastCrawled","repatriated","projectId","isSequenced","gbifRegion","publishedByGbifRegion","level0Gid","level0Name","level1Gid","level1Name","level2Gid","level2Name","level3Gid","level3Name","type_multimedia","format","identifier","references_multimedia","title","description","source","audience","created","creator","contributor","publisher_multimedia","license_multimedia","rightsHolder_multimedia","inspect_flag","Panicle length (cm)","Leaf width (cm)","Seed length (cm)","media_path").
Make sure to say which piece of information should be added to which column.

Respond with ONLY one valid JSON object. Do not use markdown or extra text.
Required shape:
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
    output["notes"] = _as_text(result.get("notes"))
    output["agent_instructions"] = _as_text(result.get("agent_instructions"))
    return output


def analyze_image(image_path, client=None, model=DEFAULT_MODEL):
    client = client or OpenAI(base_url=DEFAULT_BASE_URL, api_key="lm-studio")
    response = client.chat.completions.create(
        model=model,
        temperature=0.1,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Measure this specimen using the required two-pass format."},
                    {"type": "image_url", "image_url": {"url": _image_data_url(image_path)}},
                ],
            },
        ],
    )
    return _normalise_result(_parse_json(response.choices[0].message.content))


def _needs_retry(row):
    notes = str(row.get("notes") or "")
    return bool(str(row.get("error") or "").strip()) or notes.startswith(("Error:", "Invalid model response:"))


def _save(rows, csv_filename):
    df = pd.DataFrame(list(rows.values()))
    extra = [c for c in df.columns if c not in COLUMNS]
    df.reindex(columns=COLUMNS + extra).to_csv(csv_filename, index=False)


def analyze_media(media_dir=MEDIA_DIR, csv_filename=OUTPUT_CSV, log=print, cancel_flag=None,
                  progress=None, base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL):
    summary = {"total": 0, "measured": 0, "skipped": 0, "failed": 0, "cancelled": False}
    media_dir = Path(media_dir)
    if not media_dir.is_dir():
        log(f"Error: media folder not found: {media_dir}")
        return summary

    images = sorted(p for p in media_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
    summary["total"] = len(images)
    if not images:
        log(f"No supported images found in {media_dir}")
        return summary

    csv_filename = Path(csv_filename)
    csv_filename.parent.mkdir(parents=True, exist_ok=True)
    rows = {}
    if csv_filename.exists():
        for record in pd.read_csv(csv_filename, dtype=str, keep_default_na=False).to_dict("records"):
            rows[str(record.get("gbifID", "")).strip()] = record

    client = OpenAI(base_url=base_url, api_key="lm-studio", timeout=600)
    log(f"Measuring {len(images)} images with {model} at {base_url}")

    for index, image_path in enumerate(images):
        if cancel_flag and cancel_flag():
            summary["cancelled"] = True
            log("Measurement cancelled")
            break
        if progress:
            progress(index, len(images))

        gbif_id = image_path.stem
        previous = rows.get(gbif_id)
        if previous and not _needs_retry(previous):
            summary["skipped"] += 1
            continue

        log(f"[{index + 1}/{len(images)}] Measuring {image_path.name}")
        try:
            row = {**analyze_image(image_path, client, model), "error": ""}
            summary["measured"] += 1
        except (APIConnectionError, NotFoundError) as exc:
            raise RuntimeError(
                f"Could not use model '{model}' at {base_url}: {exc}. "
                "Check that LM Studio is running and the model is loaded."
            ) from exc
        except Exception as exc:
            row = {"error": str(exc)}
            summary["failed"] += 1
            log(f"    Failed: {exc}")

        rows[gbif_id] = {"gbifID": gbif_id, "image": image_path.name, **row}
        _save(rows, csv_filename)

    if progress and not summary["cancelled"]:
        progress(len(images), len(images))
    log(f"Measurement complete: {summary['measured']} measured, {summary['skipped']} skipped, "
        f"{summary['failed']} failed. Saved to {csv_filename}")
    return summary


if __name__ == "__main__":
    analyze_media()