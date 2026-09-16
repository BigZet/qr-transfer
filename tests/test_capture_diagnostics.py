import json
from pathlib import Path
import tempfile
import unittest

from qr_transfer.capture import receive_frames


class CaptureDiagnosticsTests(unittest.TestCase):
    def test_worker_cause_saved_without_secrets(self):
        def grab():
            try:
                raise OSError(5, "secret-payload-and-path")
            except OSError as error:
                raise RuntimeError("secret-wrapper") from error
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "receiver"
            result = receive_frames(directory, grab, lambda image: [])
            saved = json.loads((directory / "capture.json").read_text())
            self.assertEqual(result, saved)
            self.assertEqual(saved["termination"], "capture_error")
            self.assertEqual(saved["error"]["stage"], "capture")
            self.assertEqual([x["type"] for x in saved["error"]["chain"]], ["RuntimeError", "OSError"])
            self.assertEqual(saved["error"]["chain"][1]["errno"], 5)
            self.assertTrue(saved["error"]["chain"][1]["frames"])
            self.assertNotIn("secret", json.dumps(saved))
            self.assertNotIn(root, json.dumps(saved))

    def test_decoder_failure_is_distinct(self):
        def decoder(image):
            raise ValueError("private data")
        with tempfile.TemporaryDirectory() as root:
            result = receive_frames(Path(root) / "receiver", lambda: object(), decoder)
            self.assertEqual(result["error"]["stage"], "decode")
            self.assertEqual(result["error"]["chain"][0]["type"], "ValueError")


if __name__ == "__main__":
    unittest.main()
