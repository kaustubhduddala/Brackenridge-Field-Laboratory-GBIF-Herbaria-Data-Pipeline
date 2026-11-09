"""Tests for measuring untouched sheets by hand, the label reader, Model setup and the downloader.

Run with:  python -m unittest discover tests
"""
import http.server
import os
import shutil
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from program.measure import decision, downloads, labels, model_setup, ocr, records, trained
from program.measure.detectors import Detector, DetectorError, DetectorOptions, build_detector
from program.measure.pipeline import blank_result, load_result, new_entry, remeasure, save_result
from program.measure.taxa import DEFAULT_PROFILE, LANDMARKS, taxon_names
from tests import synthetic as syn


class UntouchedSheet(unittest.TestCase):
    """A sheet nobody measured: the scale comes from the pixels and a person clicks the points."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="herbaria_manual_"))
        cls.media, cls.csv = cls.root / "media", cls.root / "measurements.csv"
        cls.media.mkdir()
        rgb, cls.facts = syn.make_sheet()
        cls.image = cls.media / "7001.jpg"
        Image.fromarray(rgb).save(cls.image, quality=93)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def click(self, result, trait, first, second):
        entry = new_entry(trait, {"source": "review"}, len(result["traits"][trait]) + 1)
        names = LANDMARKS[trait]
        entry.update(anchors={names[0]: list(first), names[1]: list(second)}, edited=True, reviewed=True)
        result["traits"][trait].append(entry)

    def test_blank_sheet_has_a_scale_and_nothing_else(self):
        result = blank_result(self.image)
        self.assertEqual(result["calibration"]["source"], "ruler")
        self.assertAlmostEqual(result["calibration"]["px_per_cm"], syn.PPC, delta=1.0)
        self.assertEqual(result["detector"], {"manual": "reviewer"})
        for trait in result["traits"]:
            self.assertEqual(result["traits"][trait], [])
            self.assertEqual(result["decisions"][trait]["status"], decision.NOT_MEASURABLE)

    def test_clicked_measurements_become_a_reviewed_row_and_labels(self):
        facts = self.facts
        result = blank_result(self.image)
        self.click(result, "panicle_length", facts["base"], facts["tip"])
        cx, cy = facts["leaf_centre"]
        self.click(result, "leaf_width", (cx - 0.75 * syn.PPC, cy), (cx + 0.75 * syn.PPC, cy))
        for x, y in facts["seeds"][:3]:
            self.click(result, "seed_length", (x, y + 0.21 * syn.PPC), (x, y - 0.21 * syn.PPC))
        remeasure(result, image_path=self.image)
        decisions = result["decisions"]
        self.assertAlmostEqual(decisions["panicle_length"]["value_cm"], facts["panicle_cm"], delta=0.4)
        self.assertAlmostEqual(decisions["leaf_width"]["value_cm"], 1.5, delta=0.05)
        self.assertAlmostEqual(decisions["seed_length"]["value_cm"], 0.42, delta=0.01)
        self.assertEqual(decisions["seed_length"]["n"], 3)

        row = labels.save_review(self.csv, "7001", self.image, result)
        self.assertEqual(row["ai_review"], "reviewed")
        self.assertEqual(row["ai_detector"], "manual=reviewer")
        self.assertFalse(records.needs_review(row))
        self.assertEqual(records.load_measurements(self.csv)["7001"]["ai_panicle_length_status"], decision.ACCEPT)
        label = labels.load_labels(labels.labels_dir(self.csv))["7001"]
        self.assertEqual(sorted(o["cls"] for o in label["objects"]), ["leaf", "panicle", "seed", "seed", "seed"])
        # the saved result opens again in the Review tab (current format), with the clicked points
        record = load_result(self.csv.parent / "responses", "7001")
        self.assertIsNotNone(record)
        self.assertEqual(len(record["result"]["traits"]["seed_length"]), 3)

    def test_a_hand_value_file_is_read_for_comparison(self):
        from program.measure.benchmark import load_reference

        path = self.root / "hand.csv"
        path.write_text(' Flag , gbifID , Panicle length (cm), Leaf width (cm), Seed length (cm)\n'
                        '      , 7001   , 18.9, NA, 0.41\n')
        reference = load_reference(path)
        self.assertEqual(reference["7001"]["panicle_length"], 18.9)
        self.assertIsNone(reference["7001"]["leaf_width"])


class LabelReader(unittest.TestCase):
    class FakeReader:
        """Returns what an engine would for a region: lines with a position and a confidence."""

        def __init__(self, lines):
            self.lines_by_call, self.calls = list(lines), []

        def lines(self, rgb):
            self.calls.append(rgb.shape)
            return self.lines_by_call.pop(0) if self.lines_by_call else []

    @staticmethod
    def line(text, y, x=10, score=0.9, h=20):
        return {"text": text, "score": score, "x": x, "y": y, "h": h}

    def test_dates(self):
        for text, want in {"Date_8-17-1936": "1936-08-17", "collected 3 Mar 1954": "1954-03-03",
                           "March 2008": "2008-03", "Date 3-4-1936": "1936", "12/05/1998": "1998",
                           "25/05/1998": "1998-05-25", "1999-07-02": "1999-07-02", "no digits here": "",
                           "Date: 1921 | det. 2008": "1921", "TAES0071293 | 1936": "1936",
                           "S. L. Hatch March 2008 | Date_8-17-1936": "1936-08-17",
                           "det. March 2008 | 12 Aug 1936": "1936-08-12"}.items():
            self.assertEqual(ocr.find_date(text), want, text)

    def test_fields(self):
        text = ("GRAMINEAE S. M. TRACY HERBARIUM | A. & M. College of Texas | COLLECTOR H.B. Parks No. 19124 | "
                "LOCALITY Kingsville | COUNTY Kleberg | Urochloa maxima (Jacq.) R.D. Webster | NSF-ADBC1902064 | "
                "TAES0071293")
        fields = ocr.parse_fields(text, taxon_names(DEFAULT_PROFILE))
        self.assertEqual(fields["catalogNumber"], "TAES0071293")          # not the grant number
        self.assertEqual(fields["recordedBy"], "H.B. Parks")               # not "College of Texas"
        self.assertEqual(fields["recordNumber"], "19124")
        self.assertEqual((fields["locality"], fields["county"]), ("Kingsville", "Kleberg"))
        self.assertEqual(fields["scientificName"], "Urochloa maxima")
        self.assertEqual(fields["country"], "")                            # never guessed
        self.assertEqual(ocr.parse_fields("Panicum maximum", ())["scientificName"], "")

    def test_text_is_assembled_in_reading_order(self):
        lines = [self.line("Kingsville", 100, x=300), self.line("LOCALITY", 102, x=10), self.line("TAES0071293", 300),
                 self.line("noise", 200, score=0.2), self.line("x", 210)]
        self.assertEqual(ocr.assemble(lines), "LOCALITY Kingsville | TAES0071293")

    def test_regions(self):
        shape = (8000, 5000, 3)
        layout = {"non_plant_boxes": [[100, 6000, 2500, 7900], [10, 10, 200, 60], [4000, 50, 4900, 400]],
                  "ruler": {"box": [4000, 50, 4900, 400]}}
        names = [n for n, _ in ocr.regions_for(shape, layout, px_per_cm=150)]
        self.assertEqual(names, ["label 1"])                   # the ruler and the tiny box are left out
        fallback = ocr.regions_for(shape, {}, px_per_cm=150)
        self.assertTrue(all(box[1] >= 0.6 * 8000 for _, box in fallback))     # the bottom of the sheet

    def test_read_label_with_a_fake_engine(self):
        import numpy as np

        reader = self.FakeReader([[self.line("COLLECTOR H.B. Parks", 50), self.line("Date 3 Mar 1936", 120),
                                   self.line("TAES0071293", 200)]])
        voucher, raw = ocr.read_label(np.zeros((3000, 2000, 3), np.uint8), {}, 150, taxon_names(DEFAULT_PROFILE),
                                      reader=reader)
        self.assertEqual(voucher["voucher"]["catalogNumber"], "TAES0071293")
        self.assertEqual(voucher["voucher"]["eventDate"], "1936-03-03")
        self.assertIn(" | ", voucher["voucher_text"])
        self.assertEqual(raw["engine"], "rapidocr")

    @unittest.skipUnless(ocr.available(), "the label reader is not installed")
    def test_real_engine_reads_printed_text(self):
        import numpy as np
        from PIL import ImageDraw, ImageFont

        image = Image.new("RGB", (1500, 500), (250, 250, 245))
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=54)
        draw.text((60, 40), "Locality: KINGSVILLE, Texas", fill=(30, 30, 30), font=font)
        draw.text((60, 160), "TAES0071293", fill=(30, 30, 30), font=font)
        draw.text((60, 280), "Date: 17 August 1936", fill=(30, 30, 30), font=font)
        voucher, _ = ocr.read_label(np.asarray(image), {"non_plant_boxes": [[0, 0, 1500, 500]]}, None, ())
        self.assertEqual(voucher["voucher"]["catalogNumber"], "TAES0071293")
        self.assertTrue(voucher["voucher"]["eventDate"].startswith("1936-08"))   # engines slip on single digits


class DetectorLabels(unittest.TestCase):
    def detector(self, **options):
        locator = mock.Mock()
        locator.client.provider, locator.client.model_id = "fake", "fake"
        return Detector(locator, None, None, DetectorOptions(**options))

    def test_which_reader_is_used(self):
        with mock.patch.object(ocr, "available", return_value=True):
            self.assertEqual(self.detector().label_source, "ocr")                           # auto: OCR first
            self.assertEqual(self.detector(label_reader="vision").label_source, "vision")
            self.assertEqual(self.detector(read_labels=False).label_source, "off")
        with mock.patch.object(ocr, "available", return_value=False):
            self.assertEqual(self.detector().label_source, "vision")                         # auto: falls back
            self.assertEqual(self.detector(label_reader="ocr").label_source, "off")
        self.assertEqual(Detector(None, None, None, DetectorOptions(label_reader="vision")).label_source, "off")

    def test_ocr_replaces_the_vision_model_for_labels(self):
        import numpy as np

        detector = self.detector()
        with mock.patch.object(ocr, "available", return_value=True), \
                mock.patch.object(ocr, "read_label", return_value=({"voucher_text": "t", "voucher": {}}, {})) as read:
            voucher, _ = detector.read_voucher(None, np.zeros((10, 10, 3), np.uint8), {}, 100.0)
        self.assertEqual(voucher["voucher_text"], "t")
        read.assert_called_once()
        detector.locator.voucher.assert_not_called()
        with mock.patch.object(ocr, "available", return_value=True):
            self.assertEqual(detector.versions().get("labels"), "rapidocr")

    def test_asking_for_ocr_without_it_installed_says_so(self):
        with mock.patch.object(ocr, "available", return_value=False):
            with self.assertRaises(DetectorError):
                build_detector(None, DetectorOptions(mode="vision", label_reader="ocr"))


class Setup(unittest.TestCase):
    def test_components_report_their_state(self):
        with mock.patch.object(trained, "available", return_value=False), \
                mock.patch.object(ocr, "available", return_value=False), \
                mock.patch.object(trained, "sam_weights", return_value=None), \
                mock.patch.object(trained, "base_weights", return_value=None):
            items = model_setup.components()
            states = {c.key: c.status()[0] for c in items}
            self.assertEqual(states["addon"], model_setup.MISSING)
            self.assertEqual(states["weights"], model_setup.MISSING)
            self.assertEqual({c.key for c in model_setup.to_get(items)}, {"addon", "ocr", "sam", "weights"})
        with mock.patch.object(trained, "available", return_value=True), \
                mock.patch.object(ocr, "available", return_value=True), \
                mock.patch.object(trained, "sam_weights", return_value=Path("x")), \
                mock.patch.object(trained, "base_weights", side_effect=lambda task: Path("x") if task == "pose" else None):
            items = model_setup.components()
            self.assertEqual({c.key: c.status()[0] for c in items}["weights"], model_setup.PARTIAL)
            self.assertEqual([c.key for c in model_setup.to_get(items)], ["weights"])

    def test_get_all_continues_after_a_failure_and_stops_on_cancel(self):
        calls = []

        def make(key, fail=False):
            def install(log, cancel):
                calls.append(key)
                if fail:
                    raise RuntimeError("no network")
            return model_setup.Component(key, key, "", "", lambda: (model_setup.MISSING, ""), install)

        outcome = model_setup.get_all([make("a", fail=True), make("b")], log=lambda _: None)
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual((outcome["a"], outcome["b"]), ("no network", None))

        def cancelled(log, cancel):
            raise downloads.Cancelled()
        stop = model_setup.Component("c", "c", "", "", lambda: (model_setup.MISSING, ""), cancelled)
        with self.assertRaises(downloads.Cancelled):
            model_setup.get_all([stop], log=lambda _: None)

    def test_every_real_installer_accepts_what_the_window_passes_it(self):
        """The window calls component.install(log, cancel). A fake installer hid a real one that took other
        arguments first ("'method' object is not iterable")."""
        import inspect

        for component in model_setup.components():
            if component.install:
                inspect.signature(component.install).bind(lambda text: None, threading.Event())

    def test_the_weights_installer_fetches_both_files_and_honours_cancel(self):
        asked = []
        with mock.patch.object(trained, "fetch_asset", side_effect=lambda name, log, cancel: asked.append(name)):
            trained.download_base_weights(lambda text: None, threading.Event())
        self.assertEqual(asked, [trained.BASE_WEIGHTS["pose"], trained.BASE_WEIGHTS["seg"]])

    def test_install_commands_never_pull_in_a_second_opencv(self):
        for steps in (trained.INSTALL_STEPS, ocr.INSTALL_STEPS):
            self.assertIn("--no-deps", steps[-1])
            self.assertFalse(any("opencv" in part for step in steps for part in step))


class Downloader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = os.urandom(1_200_000)
        payload, flaky = cls.payload, {"left": 2}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/flaky" and flaky["left"] > 0:
                    flaky["left"] -= 1
                    self.send_response(504)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = int(self.headers["Range"].split("=")[1].split("-")[0]) if self.headers.get("Range") else 0
                data = payload[start:]
                self.send_response(206 if start else 200)
                if start:
                    self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    for i in range(0, len(data), 50_000):
                        self.wfile.write(data[i:i + 50_000])
                        self.wfile.flush()
                        if self.path == "/slow":
                            threading.Event().wait(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        cls.server = Server(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.folder = Path(tempfile.mkdtemp(prefix="herbaria_dl_"))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.folder, ignore_errors=True)

    def test_retries_a_flaky_server(self):
        said = []
        path = downloads.fetch(f"{self.base}/flaky", self.folder / "flaky.bin", said.append, backoff=0.05)
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertEqual(sum("HTTP 504" in line for line in said), 2)

    def test_cancel_keeps_the_partial_file_and_the_next_try_resumes(self):
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        with self.assertRaises(downloads.Cancelled):
            downloads.fetch(f"{self.base}/slow", self.folder / "slow.bin", lambda _: None, cancel)
        self.assertFalse((self.folder / "slow.bin").exists())              # never looks finished
        partial = (self.folder / "slow.bin.part").stat().st_size
        self.assertTrue(0 < partial < len(self.payload))
        path = downloads.fetch(f"{self.base}/plain", self.folder / "slow.bin", lambda _: None)
        self.assertEqual(path.read_bytes(), self.payload)

    def test_gives_up_with_a_readable_error(self):
        with self.assertRaises(RuntimeError) as caught:
            downloads.fetch("http://127.0.0.1:9/none", self.folder / "none.bin", lambda _: None, retries=2,
                            backoff=0.05)
        self.assertIn("Could not download none.bin", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class PanicleLine(unittest.TestCase):
    """A panicle drawn by hand is a straight line until it is fitted to the plant."""

    def test_straight_then_fitted_then_through_a_point(self):
        import numpy as np

        folder = Path(tempfile.mkdtemp(prefix="herbaria_fit_"))
        try:
            rgb, facts = syn.make_sheet()
            image = folder / "sheet.jpg"
            Image.fromarray(rgb).save(image, quality=93)
            result = blank_result(image)
            entry = new_entry("panicle_length", {"source": "review"}, 1)
            entry.update(anchors={"base": list(facts["base"]), "tip": list(facts["tip"])}, edited=True,
                         reviewed=True, fit=False)
            result["traits"]["panicle_length"].append(entry)
            remeasure(result, rgb=rgb)
            chord = float(np.hypot(*(np.array(facts["tip"]) - np.array(facts["base"])))) / syn.PPC
            self.assertAlmostEqual(entry["value_cm"], chord, delta=0.05)               # straight: the chord
            self.assertIn("straight", entry["method"])
            entry["fit"], entry["edited"] = True, True                                  # Fit to plant
            remeasure(result, rgb=rgb)
            self.assertAlmostEqual(entry["value_cm"], facts["panicle_cm"], delta=0.06 * facts["panicle_cm"])
            self.assertGreater(entry["value_cm"], chord + 1.0)                          # the arc, not the chord
            self.assertIn("geodesic", entry["method"])
            axis = [(facts["sheet"][0] + syn.px(x), facts["sheet"][1] + syn.px(y)) for x, y in syn.panicle_points()]
            entry["via"] = [list(axis[len(axis) // 2])]                                 # a point on the panicle
            entry["edited"] = True
            remeasure(result, rgb=rgb)                                                  # fitted through the point
            self.assertAlmostEqual(entry["value_cm"], facts["panicle_cm"], delta=0.06 * facts["panicle_cm"])
            entry["fit"], entry["edited"] = False, True                                 # Straighten
            remeasure(result, rgb=rgb)
            self.assertLess(entry["value_cm"], facts["panicle_cm"])
            self.assertIn("straight lines through", entry["method"])
        finally:
            shutil.rmtree(folder, ignore_errors=True)
