"""Measuring herbarium sheets.

Models only find things; geometry measures them; simple rules decide what each value is worth.

    taxa.py         the traits and the plausible ranges for each taxon
    calibration.py  pixels per centimetre from the ruler or the sheet
    providers.py    vision-language model providers (LM Studio, OpenAI, Gemini, Claude, custom)
    locator.py      asks a vision-language model where things are, and yes/no quality questions
    trained.py      the optional PyTorch add-on: trained YOLO seg/pose models and SAM 2 masks
    detectors.py    one interface over the vision model and the trained models
    geometry.py     plant mask, skeletons, geodesic lengths, widths, fold checks
    occlusion.py    is the end of a structure real, or hidden by a label or the sheet edge
    decision.py     flags, confidence and the accept / lower bound / not measurable / needs review rules
    pipeline.py     measures one sheet, or a folder of sheets
    overlay.py      the picture of what was measured
    records.py      the measurements CSV, joining it into a dataset, Darwin Core MeasurementOrFact export
    labels.py       reviewed labels, COCO export and import (CVAT, Label Studio)
    training.py     builds training sets from the labels and trains the models
    benchmark.py    scores measurements and flags against hand measurements
    review_tab.py   the Review tab
"""

PIPELINE_VERSION = "2.0"
