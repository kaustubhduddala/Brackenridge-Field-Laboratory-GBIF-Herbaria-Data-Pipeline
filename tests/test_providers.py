"""The vision-model client against a fake OpenAI-compatible server that behaves the way LM Studio does:
silent while it reads an image, repeating itself, refusing images, running out of memory.

Run with:  python -m unittest discover tests
"""
import http.server
import json
import socketserver
import threading
import time
import unittest
from unittest import mock

from PIL import Image

from program.measure import providers
from program.measure.providers import ModelOutputError, ModelSettings, OpenAICompatibleClient, ProviderError
from program.utils import ItemSkipped, TaskControl

MODEL_MESSAGE = ("The provided messages contain images, but {model} does not support image inputs.")


def chunk(text="", finish=None, model="fake"):
    return {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": model,
            "choices": [{"index": 0, "delta": {"content": text} if text else {}, "finish_reason": finish}]}


class FakeLmStudio(http.server.BaseHTTPRequestHandler):
    calls = {}

    def log_message(self, *args):
        pass

    def _send(self, status, body, stream=False):
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _events(self, items):
        for item in items:
            self.wfile.write(b"data: " + (item.encode() if isinstance(item, str) else json.dumps(item).encode())
                             + b"\n\n")
            self.wfile.flush()

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model = request["model"]
        self.calls[model] = self.calls.get(model, 0) + 1
        try:
            if model == "textonly":
                self._send(400, json.dumps({"error": {"message": MODEL_MESSAGE.format(model=model),
                                                      "type": "invalid_request_error"}}).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if model == "slow":                       # reading the image: nothing comes back for a long time
                for _ in range(80):
                    time.sleep(0.1)
                    self.wfile.write(b": still reading\n\n")
                    self.wfile.flush()
            elif model == "loop":
                self._events(chunk("I should look at the leaf blade again and check the margins of it. " * 4)
                             for _ in range(400))
                time.sleep(0.01)
            elif model == "oom" and self.calls[model] == 1:
                self._events([chunk('{"a"'), {"error": {"message": "Insufficient Memory (kIOGPU...OutOfMemory)"}}])
                return
            self._events([chunk('{"answer": 1}'), chunk(finish="stop"), "[DONE]"])
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class Client(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server(("127.0.0.1", 0), FakeLmStudio)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.image = Image.new("RGB", (3000, 2000), (200, 200, 190))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def client(self, model, **options):
        FakeLmStudio.calls.pop(model, None)
        return OpenAICompatibleClient(ModelSettings(provider="custom", base_url=self.url, model=model,
                                                    token_limit=0, **options), log=lambda _: None)

    def ask(self, client, control=None, status=None):
        return client.ask_json([self.image], "system", "prompt", None, "t", control, status, "Testing")

    def test_a_good_answer(self):
        answer, raw = self.ask(self.client("ok"))
        self.assertEqual(answer, {"answer": 1})
        self.assertIn("seconds", raw)
        self.assertIsNotNone(raw["first_token_s"])

    def test_cancel_works_while_the_model_is_still_reading(self):
        control = TaskControl()
        client = self.client("slow")
        threading.Timer(0.6, control.cancel_event.set).start()
        shown, started = [], time.monotonic()
        with self.assertRaises(ItemSkipped):
            self.ask(client, control, shown.append)
        self.assertLess(time.monotonic() - started, 3.0)              # not the 8 s the model takes

    def test_waiting_shows_how_long(self):
        control = TaskControl()
        client = self.client("slow")
        shown = []
        threading.Timer(4.2, control.cancel_event.set).start()
        with self.assertRaises(ItemSkipped):
            self.ask(client, control, shown.append)
        self.assertTrue(any("reading the image" in text for text in shown), shown)

    def test_skip_gives_up_on_one_item_only(self):
        control = TaskControl()
        client = self.client("slow")
        threading.Timer(0.4, control.request_skip).start()
        with self.assertRaises(ItemSkipped):
            self.ask(client, control)
        control.new_item()
        answer, _ = self.ask(self.client("ok"), control)              # the next item runs normally
        self.assertEqual(answer, {"answer": 1})

    def test_a_model_that_repeats_itself_is_stopped(self):
        started = time.monotonic()
        with self.assertRaises(ModelOutputError) as caught:
            self.ask(self.client("loop"))
        self.assertIn("repeating itself", str(caught.exception))
        self.assertLess(time.monotonic() - started, 6.0)

    def test_a_text_only_model_stops_the_run_with_a_clear_message(self):
        with self.assertRaises(ProviderError) as caught:
            self.ask(self.client("textonly"))
        self.assertIn("text-only", str(caught.exception))
        self.assertEqual(FakeLmStudio.calls["textonly"], 1)           # asked once, not once per sheet

    def test_out_of_memory_retries_with_smaller_images(self):
        client = self.client("oom")
        before = client.max_image_side
        answer, _ = self.ask(client)
        self.assertEqual(answer, {"answer": 1})
        self.assertLess(client.max_image_side, before)

    def test_image_size_setting(self):
        self.assertEqual(self.client("ok", image_side=1200).max_image_side, 1200)
        self.assertEqual(self.client("ok").max_image_side, providers.PROVIDERS["custom"]["max_image_side"])
        from program.measure.providers import settings_from_app
        self.assertEqual(settings_from_app({"image_side": "1500"}).image_side, 1500)
        self.assertEqual(settings_from_app({"image_side": "oops"}).image_side, 0)


class Preflight(unittest.TestCase):
    def test_lm_studio_text_only_model_is_refused_before_any_sheet(self):
        models = [{"id": "text-model", "type": "llm"}, {"id": "vision-model", "type": "vlm"}]
        with mock.patch.object(providers, "_lmstudio_models", return_value=models):
            with self.assertRaises(ProviderError) as caught:
                OpenAICompatibleClient(ModelSettings(provider="lmstudio", model="text-model"), log=lambda _: None)
            self.assertIn("vision-model", str(caught.exception))
            OpenAICompatibleClient(ModelSettings(provider="lmstudio", model="vision-model"), log=lambda _: None)

    def test_repetition_detector(self):
        self.assertFalse(providers.is_repeating("a short answer"))
        self.assertTrue(providers.is_repeating("think about the margin of the leaf blade carefully. " * 20))
        self.assertFalse(providers.is_repeating(json.dumps({"points": [[i, i * 2] for i in range(300)]})))


if __name__ == "__main__":
    unittest.main()
