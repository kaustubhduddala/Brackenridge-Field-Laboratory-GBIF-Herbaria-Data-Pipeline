import base64
import json
import re
from pathlib import Path
import pandas as pd
from openai import OpenAI


MEDIA_DIR = Path(__file__).resolve().parent / "data" / "media"
OUTPUT_CSV = Path(__file__).resolve().parent / "data" / "measurements.csv"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
FIELDS = [
    "panicle_length_1_cm", "panicle_length_1_confidence",
    "panicle_length_2_cm", "panicle_length_2_confidence",
    "leaf_width_1_cm", "leaf_width_1_confidence",
    "leaf_width_2_cm", "leaf_width_2_confidence",
    "seed_length_1_cm", "seed_length_1_confidence",
    "seed_length_2_cm", "seed_length_2_confidence",
]
MODEL_NAME = "Qwen3-VL-Thinking"

client = OpenAI(
    base_url="http://localhost:1234/v1",
    api_key="lm-studio",
)

SYSTEM_PROMPT = """
You are a meticulous botanical measurement assistant analyzing one herbarium
specimen image of Guinea grass (Megathyrsus maximus).

Use the ruler or scale bar if visible. If there is no scale bar but the full
herbarium sheet is visible, use 29 cm as the sheet width only as a fallback.
If neither calibration method is available, return null measurements and say
why in notes. Do not invent measurements. The ruler may not look like a standard 
ruler, but if it has visible tick marks, it can be used for calibration. If the 
ruler is present but not visible enough to calibrate, return null measurements and say why in notes. 

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
is absent, obscured, or cannot be calibrated, do your best to measure faithful 
measurements but if that is not possible, use null for both its value and
confidence. If the specimen has flowers but no visible seeds, seed length is
null. Confidence must reflect visibility, calibration quality, and endpoint
clarity, not certainty created by guessing.

In addition, many will include vouchers that are written out information about the specimen. 
If you can read any of this information, please extract the text transcription and add it to 
the agent_instructions field. If you cannot read any of the text, leave agent_instructions 
as an empty string and note such in the notes column. agent_instructions should instruct 
another agent to take your transcribed text and add it to the original data row in case of 
missing information. the information may be pertinent to any of these columns ("gbifID","media","accessRights","bibliographicCitation","language","license","modified","references","rightsHolder","type_occurrence","institutionID","collectionID","datasetID","institutionCode","collectionCode","datasetName","ownerInstitutionCode","basisOfRecord","informationWithheld","dataGeneralizations","dynamicProperties","occurrenceID","catalogNumber","recordNumber","recordedBy","recordedByID","individualCount","organismQuantity","organismQuantityType","sex","lifeStage","reproductiveCondition","establishmentMeans","degreeOfEstablishment","georeferenceVerificationStatus","occurrenceStatus","preparations","disposition","associatedReferences","associatedTaxa","otherCatalogNumbers","occurrenceRemarks","organismID","previousIdentifications","organismRemarks","materialSampleID","eventID","parentEventID","fieldNumber","eventDate","startDayOfYear","endDayOfYear","year","month","day","verbatimEventDate","habitat","samplingProtocol","fieldNotes","eventRemarks","locationID","higherGeography","continent","islandGroup","island","countryCode","stateProvince","county","municipality","locality","verbatimLocality","verbatimElevation","locationAccordingTo","locationRemarks","decimalLatitude","decimalLongitude","coordinateUncertaintyInMeters","coordinatePrecision","verbatimCoordinateSystem","verbatimSRS","georeferencedBy","georeferencedDate","georeferenceProtocol","georeferenceSources","georeferenceRemarks","identificationID","verbatimIdentification","identificationQualifier","typeStatus","identifiedBy","identifiedByID","dateIdentified","identificationReferences","identificationVerificationStatus","identificationRemarks","taxonID","scientificNameID","acceptedNameUsageID","parentNameUsageID","nameAccordingToID","namePublishedInID","taxonConceptID","scientificName","acceptedNameUsage","parentNameUsage","originalNameUsage","namePublishedIn","higherClassification","kingdom","phylum","class","order","family","genus","genericName","specificEpithet","infraspecificEpithet","taxonRank","verbatimTaxonRank","vernacularName","nomenclaturalCode","taxonomicStatus","nomenclaturalStatus","taxonRemarks","datasetKey","publishingCountry","lastInterpreted","elevation","elevationAccuracy","distanceFromCentroidInMeters","issue","mediaType","hasCoordinate","hasGeospatialIssues","taxonKey","acceptedTaxonKey","kingdomKey","phylumKey","classKey","orderKey","familyKey","genusKey","speciesKey","species","acceptedScientificName","verbatimScientificName","typifiedName","protocol","lastParsed","lastCrawled","repatriated","projectId","isSequenced","gbifRegion","publishedByGbifRegion","level0Gid","level0Name","level1Gid","level1Name","level2Gid","level2Name","level3Gid","level3Name","type_multimedia","format","identifier","references_multimedia","title","description","source","audience","created","creator","contributor","publisher_multimedia","license_multimedia","rightsHolder_multimedia","inspect_flag","Panicle length (cm)","Leaf width (cm)","Seed length (cm)","media_path"). Make sure to include which piece of information should be added to which column 

Respond with ONLY one valid JSON object. Do not use markdown or extra text.
Required shape:
{
  "panicle_length_1_cm": number|null,
  "panicle_length_1_confidence": number|null,
  "panicle_length_2_cm": number|null,
  "panicle_length_2_confidence": number|null,
  "leaf_width_1_cm": number|null,
  "leaf_width_1_confidence": number|null,
  "leaf_width_2_cm": number|null,
  "leaf_width_2_confidence": number|null,
  "seed_length_1_cm": number|null,
  "seed_length_1_confidence": number|null,
  "seed_length_2_cm": number|null,
  "seed_length_2_confidence": number|null,
  "notes": "brief explanation",
  "agent_instructions": "instructions"
}
"""


def encode_image(image_path):
    return base64.b64encode(Path(image_path).read_bytes()).decode("utf-8")


def _parse_json(content):
    text = str(content or "").strip()
    if not text:
        raise ValueError("Model returned an empty response")

    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    for candidate in re.findall(r"\{.*?\}", text, flags=re.DOTALL) + [text[text.find("{") : text.rfind("}") + 1]]:
        if candidate.count("{") == 0:
            continue
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON object found in model response")


def _normalise_result(result):
    output = {}
    for field in FIELDS:
        value = result.get(field)
        output[field] = None if value in (None, "") else round(float(value), 2)
    output["notes"] = str(result.get("notes", "") or "").strip()
    output["agent_instructions"] = str(result.get("agent_instructions", "") or "").strip()
    return output


def analyze_image(image_path):
    suffix = Path(image_path).suffix.lower()
    media_type = "jpeg" if suffix in {".jpg", ".jpeg"} else suffix[1:]
    response = client.chat.completions.create(
        model=MODEL_NAME,
        temperature=0.1,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Measure this specimen using the required two-pass format.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/{media_type};base64,{encode_image(image_path)}"
                        },
                    },
                ],
            },
        ],
    )
    return _normalise_result(_parse_json(response.choices[0].message.content))


def analyze_media(media_dir=MEDIA_DIR, csv_filename=OUTPUT_CSV):
    image_paths = sorted(
        path for path in Path(media_dir).iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )
    if not image_paths:
        print(f"No supported images found in {media_dir}")
        return

    csv_filename = Path(csv_filename)
    existing = pd.read_csv(csv_filename) if csv_filename.exists() else pd.DataFrame()
    rows = existing.to_dict("records")
    processed = {str(x) for x in existing.get("gbifID", pd.Series(dtype=str)).astype(str)}

    for index, image_path in enumerate(image_paths, start=1):
        gbif_id = image_path.stem
        if gbif_id in processed:
            print(f"[{index}/{len(image_paths)}] Skipping {image_path.name}")
            continue

        print(f"[{index}/{len(image_paths)}] Measuring {image_path.name}...")
        try:
            row = {"gbifID": gbif_id, **analyze_image(image_path)}
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            row = {"gbifID": gbif_id, "notes": f"Invalid model response: {exc}"}
        except Exception as exc:
            row = {"gbifID": gbif_id, "notes": f"Error: {exc}"}

        rows.append(row)
        pd.DataFrame(rows).to_csv(csv_filename, index=False)
        processed.add(gbif_id)

    print(f"Saved measurements to {csv_filename}")


if __name__ == "__main__":
    analyze_media()