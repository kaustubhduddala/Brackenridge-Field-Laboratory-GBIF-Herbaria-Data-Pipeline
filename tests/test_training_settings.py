import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from program.measure import training  # noqa: E402


class RecommendTests(unittest.TestCase):
    def test_small_memory_gets_a_smaller_image_size(self):
        small, large = training.recommend("mps", 8), training.recommend("mps", 64)
        self.assertLess(small["imgsz"], large["imgsz"])
        self.assertLess(small["batch"], large["batch"])
        self.assertGreaterEqual(small["batch"], 1)

    def test_batch_is_a_power_of_two_and_fits(self):
        for device, memory in (("mps", 24), ("cuda", 12), ("cpu", 16), ("mps", None)):
            settings = training.recommend(device, memory)
            self.assertIn(settings["batch"], (1, 2, 4, 8, 16))
            self.assertIn(settings["imgsz"], training.IMGSZ_STEPS + (training.MIN_IMGSZ,))

    def test_fewer_sheets_get_more_epochs(self):
        few, many = training.recommend("mps", 24, 20), training.recommend("mps", 24, 400)
        self.assertGreater(few["epochs"], many["epochs"])
        self.assertTrue(50 <= many["epochs"] <= few["epochs"] <= 300)


class SavedSettingsTests(unittest.TestCase):
    def test_missing_or_bad_values_fall_back_to_defaults(self):
        with mock.patch.object(training, "load_settings", return_value={"training": {"epochs": "x", "imgsz": 960,
                                                                                    "device": "tpu"}}):
            saved = training.saved_settings()
        self.assertEqual((saved["epochs"], saved["imgsz"], saved["batch"], saved["device"]),
                         (training.DEFAULTS["epochs"], 960, None, "auto"))

    def test_out_of_memory_message_is_recognised(self):
        self.assertTrue(training.is_out_of_memory(RuntimeError("MPS ... kIOGPUCommandBufferCallbackErrorOutOfMemory")))
        self.assertFalse(training.is_out_of_memory(ValueError("bad label")))


if __name__ == "__main__":
    unittest.main()
