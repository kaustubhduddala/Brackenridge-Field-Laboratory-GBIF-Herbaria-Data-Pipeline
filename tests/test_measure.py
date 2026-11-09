"""End-to-end checks of the measuring pipeline on drawn sheets with known measurements.

Run with:  python -m unittest discover tests
"""
import csv
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from program.measure import decision, geometry, labels, occlusion, records, training
from program.measure.benchmark import load_reference, score
from program.measure.calibration import manual_calibration
from program.measure.pipeline import load_result, measure_images, measure_sheet, remeasure
from program.measure.taxa import DEFAULT_PROFILE, TRAITS
from tests import synthetic as syn


class Geometry(unittest.TestCase):
    def ribbon(self):
        mask = np.zeros((700, 700), np.uint8)
        points = np.array([[100 + 300 * math.cos(t), 400 - 300 * math.sin(t)]
                           for t in np.linspace(0, math.pi / 2, 200)], np.int32)
        cv2.polylines(mask, [points], False, 1, thickness=12)
        return mask

    def test_bent_leaf_is_measured_along_its_length(self):
        mask = self.ribbon()
        path, _ = geometry.skeleton_path(mask)
        arc = geometry.arc_length(path)
        self.assertAlmostEqual(arc, 300 * math.pi / 2, delta=8)            # the arc, 471 px
        self.assertLess(math.hypot(*(path[-1] - path[0])), arc - 40)       # not the chord, 424 px

    def test_width_is_the_inscribed_width(self):
        width, _, a, b = geometry.widest_point(self.ribbon())
        self.assertAlmostEqual(width, 12.5, delta=1.0)
        self.assertAlmostEqual(math.hypot(a[0] - b[0], a[1] - b[1]), width, delta=1.5)

    def test_fold_is_flagged(self):
        mask = self.ribbon()
        self.assertEqual(geometry.blade_checks(mask, 100)[1], [])
        cv2.line(mask, (320, 230), (360, 130), 1, thickness=26)
        self.assertIn("width_jump", geometry.blade_checks(mask, 100)[1])

    def test_polygon_round_trip(self):
        mask = self.ribbon()
        polygon = geometry.mask_polygon(mask, offset=(10, 20), scale=0.5)
        back = geometry.polygon_mask(polygon, mask.shape, offset=(10, 20), scale=0.5)
        self.assertGreater((back & mask).sum() / (back | mask).sum(), 0.9)


class Decisions(unittest.TestCase):
    def entry(self, value, *codes, score=0.9):
        entry = {"trait": "panicle_length", "value_cm": value, "score": score, "quality": 1.0, "flags": []}
        for code in codes:
            decision.add_flag(entry, code)
        return entry

    def decide(self, *entries, calibration=None):
        calibration = calibration or {"px_per_cm": 100.0, "source": "ruler", "uncertain": []}
        return decision.decide_trait("panicle_length", list(entries), DEFAULT_PROFILE, calibration)

    def test_statuses(self):
        self.assertEqual(self.decide(self.entry(25.0))["status"], decision.ACCEPT)
        self.assertEqual(self.decide(self.entry(25.0, "winding"))["status"], decision.NEEDS_REVIEW)
        self.assertEqual(self.decide(self.entry(25.0, "folded"))["status"], decision.NOT_MEASURABLE)
        self.assertEqual(self.decide(self.entry(55.0))["status"], decision.NEEDS_REVIEW)      # unusual for the taxon

    def test_lower_bound_stays_out_of_the_value(self):
        result = self.decide(self.entry(21.0, "occluded_tip"))
        self.assertEqual(result["status"], decision.LOWER_BOUND)
        self.assertIsNone(result["value_cm"])
        self.assertEqual(result["lower_bound_cm"], 21.0)

    def test_clean_objects_are_preferred(self):
        result = self.decide(self.entry(20.0), self.entry(30.0, "winding"))
        self.assertEqual((result["value_cm"], result["n"], result["status"]), (20.0, 1, decision.ACCEPT))

    def test_uncertain_scale_sends_to_review(self):
        calibration = {"px_per_cm": 100.0, "source": "sheet", "uncertain": ["sheet edge touches the image border"]}
        self.assertEqual(self.decide(self.entry(25.0), calibration=calibration)["status"], decision.NEEDS_REVIEW)

    def test_seeds_use_the_median_and_need_three(self):
        def seeds(*values):
            entries = [{"trait": "seed_length", "value_cm": v, "score": 0.9, "flags": []} for v in values]
            return decision.decide_trait("seed_length", entries, DEFAULT_PROFILE, {"px_per_cm": 150, "source": "ruler"})
        self.assertEqual(seeds(0.40, 0.42, 0.60)["value_cm"], 0.42)
        self.assertEqual(seeds(0.40, 0.42)["status"], decision.NEEDS_REVIEW)
        self.assertEqual(seeds(0.40, 0.41, 0.42, 0.43)["status"], decision.ACCEPT)


class Sheets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="herbaria_test_"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)

    def measure(self, name, detector=None, **sheet):
        rgb, facts = syn.make_sheet(**sheet)
        path = self.folder / f"{name}.jpg"
        Image.fromarray(rgb).save(path, quality=95)
        result, _ = measure_sheet(path, syn.fake_detector(facts, **(detector or {})))
        return result, facts

    def test_plain_sheet(self):
        result, facts = self.measure("plain")
        self.assertEqual(result["calibration"]["source"], "ruler")
        self.assertAlmostEqual(result["calibration"]["px_per_cm"], facts["px_per_cm"], delta=0.5)
        decisions = result["decisions"]
        self.assertEqual([decisions[t]["status"] for t in TRAITS], [decision.ACCEPT] * 3)
        self.assertAlmostEqual(decisions["panicle_length"]["value_cm"], facts["panicle_cm"], delta=0.02 * facts["panicle_cm"])
        self.assertAlmostEqual(decisions["leaf_width"]["value_cm"], facts["leaf_cm"], delta=0.03 * facts["leaf_cm"])
        self.assertAlmostEqual(decisions["seed_length"]["value_cm"], facts["seed_cm"], delta=0.05 * facts["seed_cm"])
        self.assertGreaterEqual(decisions["seed_length"]["n"], 5)

    def test_hidden_tip_is_a_lower_bound(self):
        result, facts = self.measure("label_over_tip", label_over_tip=True)
        panicle = result["decisions"]["panicle_length"]
        self.assertEqual(panicle["status"], decision.LOWER_BOUND)
        self.assertIsNone(panicle["value_cm"])
        self.assertLess(panicle["lower_bound_cm"], facts["panicle_cm"])
        self.assertGreater(panicle["lower_bound_cm"], 0.6 * facts["panicle_cm"])
        row = records.result_row("1", "x.jpg", result)
        self.assertEqual(row["ai_panicle_length_cm"], "")
        self.assertNotEqual(row["ai_panicle_length_lower_bound_cm"], "")

    def test_tip_placed_mid_panicle_goes_to_review(self):
        result, facts = self.measure("short_tip", {"tip_error_cm": 6.0})
        panicle = result["decisions"]["panicle_length"]
        self.assertEqual(panicle["status"], decision.NEEDS_REVIEW)
        self.assertIn("tip_not_at_end", panicle["flags"])

    def test_folded_leaf(self):
        result, _ = self.measure("folded", {"outlines": True}, folded_leaf=True)
        self.assertEqual(result["decisions"]["leaf_width"]["status"], decision.NEEDS_REVIEW)
        result, _ = self.measure("folded_checked", {"outlines": True, "second_opinion": True,
                                                    "locator": syn.FakeLocator(flat=False)}, folded_leaf=True)
        self.assertEqual(result["decisions"]["leaf_width"]["status"], decision.NOT_MEASURABLE)

    def test_second_opinion_is_one_vote(self):
        result, _ = self.measure("hidden_vote", {"second_opinion": True, "locator": syn.FakeLocator(tip="hidden")})
        panicle = result["decisions"]["panicle_length"]
        self.assertEqual(panicle["status"], decision.NEEDS_REVIEW)        # value kept, a person decides
        self.assertIsNotNone(panicle["value_cm"])

    def test_low_resolution_is_not_measurable(self):
        rgb, facts = syn.make_sheet()
        small = cv2.resize(rgb, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)       # 30 px/cm
        path = self.folder / "small.jpg"
        Image.fromarray(small).save(path, quality=95)
        facts = {**facts, "label": [v * 0.25 for v in facts["label"]]}
        result, _ = measure_sheet(path, syn.fake_detector(facts))
        self.assertEqual(result["decisions"]["seed_length"]["status"], decision.NOT_MEASURABLE)
        self.assertIn("low_resolution", result["decisions"]["seed_length"]["flags"])


class Workflow(unittest.TestCase):
    """A folder of sheets: measuring, the CSV, review, labels, COCO, training sets and the benchmark."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="herbaria_flow_"))
        cls.media, cls.csv = cls.root / "media", cls.root / "measurements.csv"
        cls.media.mkdir()
        rgb, cls.facts = syn.make_sheet()
        cls.ids = [str(1000 + n) for n in range(12)]
        for gbif_id in cls.ids:
            Image.fromarray(rgb).save(cls.media / f"{gbif_id}.jpg", quality=92)
        # an old-format measurements file and its answers must be set aside, not overwritten
        cls.csv.write_text('"gbifID","ai_image","ai_panicle_length_mean_cm"\n"1000","1000.jpg","9.09"\n')
        (cls.root / "responses").mkdir()
        (cls.root / "responses" / "1000.json").write_text("{}")
        cls.logs = []
        cls.summary = measure_images(cls.media, cls.csv, log=cls.logs.append,
                                     detector=syn.fake_detector(cls.facts, outlines=True))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_1_fresh_file(self):
        self.assertTrue((self.root / "measurements_legacy.csv").is_file())
        self.assertTrue((self.root / "responses_legacy" / "1000.json").is_file())
        self.assertEqual(self.summary["measured"], len(self.ids))
        rows = records.load_measurements(self.csv)
        self.assertEqual(set(rows), set(self.ids))
        row = rows["1000"]
        self.assertEqual(row["ai_panicle_length_status"], decision.ACCEPT)
        self.assertEqual(row["ai_scale_source"], "ruler")
        self.assertEqual(row["ai_pipeline"], "2.0")
        self.assertTrue(row["ai_panicle_length_method"].startswith("geodesic path"))
        with open(self.csv, newline="", encoding="utf-8-sig") as handle:
            self.assertEqual(next(csv.reader(handle)), records.MEASUREMENT_FILE_COLUMNS)
        self.assertTrue((self.root / "responses" / "1000_overlay.jpg").is_file())

    def test_2_measurement_or_fact(self):
        table = records.measurement_or_fact(records.load_measurements(self.csv))
        self.assertEqual(len(table), 3 * len(self.ids))
        self.assertEqual(set(table["measurementType"]), {"panicle length", "leaf width", "seed length"})
        self.assertTrue((self.root / records.MOF_FILE_NAME).is_file())

    def test_3_review_updates_row_and_writes_label(self):
        record = load_result(self.root / "responses", "1001")
        result = record["result"]
        leaf = result["traits"]["leaf_width"][0]
        a, b = leaf["anchors"]["a"], leaf["anchors"]["b"]
        leaf["anchors"]["b"] = [a[0] + 2 * (b[0] - a[0]), a[1] + 2 * (b[1] - a[1])]       # the reviewer doubles it
        leaf["edited"] = True
        result["traits"]["panicle_length"][0]["attrs"] = {"cut_off": True}
        row = labels.save_review(self.csv, "1001", self.media / "1001.jpg", result)
        self.assertEqual(row["ai_review"], "reviewed")
        self.assertAlmostEqual(float(row["ai_leaf_width_cm"]), 2 * self.facts["leaf_cm"], delta=0.15)
        self.assertEqual(row["ai_panicle_length_status"], decision.LOWER_BOUND)
        self.assertEqual(row["ai_panicle_length_cm"], "")
        label = labels.load_labels(labels.labels_dir(self.csv))["1001"]
        self.assertEqual({o["cls"] for o in label["objects"]}, {"panicle", "leaf", "seed"})

    def test_4_manual_scale_rescales_everything(self):
        record = load_result(self.root / "responses", "1002")
        result = record["result"]
        before = result["decisions"]["panicle_length"]["value_cm"]
        ppc = result["calibration"]["px_per_cm"]
        result["calibration"] = manual_calibration(result["calibration"], [0, 0], [2 * ppc * 10, 0], 10.0).as_dict()
        remeasure(result, image_path=self.media / "1002.jpg")
        self.assertAlmostEqual(result["decisions"]["panicle_length"]["value_cm"], before / 2, delta=0.02)

    def test_5_coco_round_trip(self):
        for gbif_id in self.ids:
            record = load_result(self.root / "responses", gbif_id)
            labels.save_review(self.csv, gbif_id, self.media / f"{gbif_id}.jpg", record["result"])
        stored = labels.load_labels(labels.labels_dir(self.csv))
        self.assertEqual(len(stored), len(self.ids))
        path, images, annotations = labels.export_coco(self.csv, log=lambda _: None)
        coco = json.loads(path.read_text())
        self.assertEqual(images, len(self.ids))
        self.assertEqual({c["name"] for c in coco["categories"]}, set(labels.CLASSES))
        back = labels.from_coco(coco)
        for gbif_id, label in stored.items():
            mine = [(o["cls"], o["keypoints"]) for o in label["objects"]]
            theirs = [(o["cls"], o["keypoints"]) for o in back[gbif_id]["objects"]]
            self.assertEqual(mine, theirs)
        self.assertEqual(labels.import_coco(path, self.csv, self.media, log=lambda _: None), len(self.ids))
        self.assertEqual(records.load_measurements(self.csv)["1003"]["ai_review"], "reviewed")

    def test_6_training_sets(self):
        labelled = training.gather_labels([self.root])
        self.assertEqual(len(labelled), len(self.ids))
        for task in ("pose", "seg"):
            built = training.build_dataset(task, labelled, out_dir=self.root / f"dataset_{task}", log=lambda _: None)
            self.assertTrue(built["yaml"].is_file())
            self.assertEqual(set(built["splits"].values()), {"train", "val", "test"})
            texts = list((built["dir"] / "labels").rglob("*.txt"))
            self.assertEqual(len(texts), len(list((built["dir"] / "images").rglob("*.jpg"))))
            for text in texts:
                for line in text.read_text().splitlines():
                    values = [float(v) for v in line.split()]
                    self.assertTrue(all(0.0 <= v <= 1.0 for v in values[1:] if v != 2.0), line)
                    if task == "pose":
                        self.assertEqual(len(values), 11)
        pose_yaml = (self.root / "dataset_pose" / "data.yaml").read_text()
        self.assertIn("kpt_shape: [2, 3]", pose_yaml)
        self.assertEqual(training.split_of("123"), training.split_of("123"))

    def test_7_benchmark_scores_values_and_flags(self):
        reference = self.root / "hand.csv"
        rows = [["Flag", "gbifID", "Panicle length (cm)", "Leaf width (cm)", "Seed length (cm)"]]
        rows += [["", gbif_id, f"{self.facts['panicle_cm']:.2f}", "1.5", "NA"] for gbif_id in self.ids]
        with open(reference, "w", newline="") as handle:
            csv.writer(handle).writerows(rows)
        per_image, summary = score(load_reference(reference), records.load_measurements(self.csv))
        panicle = summary[summary["trait"] == "Panicle length"].iloc[0]
        self.assertEqual(panicle["hand_measured"], len(self.ids))
        self.assertLess(panicle["median_abs_error_pct"], 3.0)
        seed = summary[summary["trait"] == "Seed length"].iloc[0]
        self.assertEqual(seed["hand_NA"], len(self.ids))
        self.assertEqual(seed["hand_NA_without_ai_value_pct"], 0.0)       # the pipeline did measure seeds here


class ReviewQueue(unittest.TestCase):
    def row(self, **statuses):
        row = {"ai_measured_at": "2026-01-01T00:00:00", "ai_error": ""}
        for trait in TRAITS:
            row[f"ai_{trait}_status"] = statuses.get(trait, decision.ACCEPT)
            row[f"ai_{trait}_flags"] = statuses.get(trait + "_flags", "")
            row[f"ai_{trait}_confidence"] = "0.80"
        return row

    def test_what_needs_a_person(self):
        self.assertFalse(records.needs_review(self.row()))
        self.assertTrue(records.needs_review(self.row(seed_length=decision.NEEDS_REVIEW)))
        self.assertTrue(records.needs_review(self.row(panicle_length=decision.LOWER_BOUND)))
        # nothing found: a person can click the structure in; too low a resolution cannot be fixed
        found_nothing = self.row(panicle_length=decision.NOT_MEASURABLE, panicle_length_flags="not_found")
        self.assertTrue(records.needs_review(found_nothing))
        self.assertEqual(records.lowest_confidence(found_nothing), 0.0)
        self.assertFalse(records.needs_review(self.row(seed_length=decision.NOT_MEASURABLE,
                                                       seed_length_flags="low_resolution")))
        reviewed = self.row(seed_length=decision.NEEDS_REVIEW)
        reviewed["ai_review"] = "reviewed"
        self.assertFalse(records.needs_review(reviewed))


class Occlusion(unittest.TestCase):
    def test_touching(self):
        occluders = [{"kind": "label", "box": [300, 200, 700, 500]}]
        self.assertEqual(occlusion.touching([500, 505], occluders, None, (1000, 1500), 50.0)["kind"], "label")
        self.assertEqual(occlusion.touching([500, 8], occluders, None, (1000, 1500), 50.0)["kind"], "image edge")
        self.assertIsNone(occlusion.touching([500, 900], occluders, None, (1000, 1500), 50.0))


if __name__ == "__main__":
    unittest.main()


class VisionModelPath(unittest.TestCase):
    """The real locator and detector, driven by a model client that answers its actual prompts."""

    def test_vision_detector_end_to_end(self):
        from program.measure.detectors import Detector, DetectorOptions
        from program.measure.locator import Locator

        folder = Path(tempfile.mkdtemp(prefix="herbaria_vision_"))
        try:
            rgb, facts = syn.make_sheet()
            path = folder / "sheet.jpg"
            Image.fromarray(rgb).save(path, quality=95)
            client = syn.FakeVisionClient(facts)
            detector = Detector(client.attach(Locator(client)), None, None,
                                DetectorOptions(mode="vision", second_opinion=True, read_labels=True,
                                                label_reader="vision"))
            result, raw = measure_sheet(path, detector)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        for name in ("sheet_layout", "panicles", "panicle_ends", "leaf_positions", "leaf_width", "spikelet_boxes",
                     "panicle_check", "leaf_check", "spikelet_check", "specimen_voucher"):
            self.assertIn(name, client.asked)
        decisions = result["decisions"]
        self.assertEqual(result["calibration"]["source"], "ruler")              # found without the model's help
        self.assertAlmostEqual(decisions["panicle_length"]["value_cm"], facts["panicle_cm"], delta=0.4)
        self.assertAlmostEqual(decisions["leaf_width"]["value_cm"], facts["leaf_cm"], delta=0.05)
        self.assertAlmostEqual(decisions["seed_length"]["value_cm"], facts["seed_cm"], delta=0.03)
        if decisions["seed_length"]["n"] < 3:              # the close-up held few seeds: a person must look
            self.assertEqual(decisions["seed_length"]["status"], decision.NEEDS_REVIEW)
            self.assertIn("few_seeds", decisions["seed_length"]["flags"])
        self.assertEqual(result["voucher"]["scientificName"], "Panicum maximum")
        self.assertEqual(result["detector"], {"vision": "anthropic:fake-vision"})
        self.assertEqual(records.result_row("1", "sheet.jpg", result)["ai_model"], "anthropic:fake-vision")
