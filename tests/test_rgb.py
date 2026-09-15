import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

from qr_transfer.rgb import RGBDecoder, SERVICE, layers_for, schedule
from qr_transfer.player import matrix


def tile(payloads, visual="rgb8", scale=4):
    import numpy as np
    from PIL import Image
    layers = layers_for(visual)
    rgb = np.full((195, 185, 3), 255, dtype=np.uint8)
    for layer, raw in enumerate(payloads):
        rgb[4:181, 4:181, layer] = np.where(np.asarray(matrix(raw)), 0, 255)
    for index in range(1 << layers):
        rgb[187:193, 4+index*22:22+index*22] = [0 if index & (1 << c) else 255 for c in range(3)]
    return Image.fromarray(rgb).resize((185*scale, 195*scale), Image.Resampling.NEAREST)


@unittest.skipUnless(all(importlib.util.find_spec(m) for m in ("numpy", "segno", "zxingcpp")), "Install sender/receiver extras")
class RGBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qr_transfer.transfer import prepare_object, packets
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name)/"source"
        cls.path.write_bytes(hashlib.shake_256(b"rgb-test").digest(8400))
        cls.transfer = prepare_object(cls.path, container="7z-aes256")
        cls.packets = list(packets(cls.transfer))
        cls.picture = tile(cls.packets[1:4])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_all_planes_exact_binary_both_methods(self):
        import numpy as np
        self.assertEqual(len(np.unique(np.asarray(self.picture).reshape(-1,3), axis=0)), 8)
        for method in ("threshold", "palette"):
            decoder = RGBDecoder(method=method)
            result = decoder(self.picture)
            self.assertEqual({r.raw for r in result}, set(self.packets[1:4]))
            self.assertEqual({r.layer for r in result}, {0, 1, 2})

    def test_two_slots_and_geometry_change(self):
        from PIL import Image
        two = Image.new("RGB", (self.picture.width*2+8, self.picture.height), "white")
        two.paste(self.picture, (0, 0))
        two.paste(self.picture, (self.picture.width+8, 0))
        decoder = RGBDecoder()
        result = decoder(two)
        self.assertEqual({(r.slot,r.layer) for r in result}, {(s,c) for s in (0,1) for c in (0,1,2)})
        smaller = self.picture.resize((555,585), Image.Resampling.NEAREST)
        result = decoder(smaller)
        self.assertEqual({r.raw for r in result}, set(self.packets[1:4]))
        self.assertEqual(len(decoder.boxes), 1)

    def test_four_colors_and_service(self):
        decoder = RGBDecoder("rg4")
        self.assertEqual({r.raw for r in decoder(tile(self.packets[1:3], "rg4"))}, set(self.packets[1:3]))
        self.assertEqual(decoder(tile([SERVICE]*2, "rg4")), [])
        self.assertGreater(decoder.stats["service_symbols"], 0)

    def test_weak_layer_independent_and_changed_layer_not_skipped(self):
        import numpy as np
        from PIL import Image
        decoder = RGBDecoder(skip_unchanged=True, search_seconds=100)
        decoder(self.picture)
        self.assertEqual(decoder(self.picture), [])
        changed = tile([self.packets[1], self.packets[2], self.packets[1]])
        result = decoder(changed)
        self.assertEqual([(r.layer, r.raw) for r in result], [(2, self.packets[1])])
        damaged = np.array(self.picture)
        damaged[16:724, 16:724, 2] = 255
        result = RGBDecoder()(Image.fromarray(damaged))
        self.assertEqual({r.layer for r in result}, {0, 1})

    def test_palette_color_shift_and_missing_calibration(self):
        import numpy as np
        from PIL import Image
        # Explicit patches undergo the same cross-channel mixing as data.
        rgb = np.asarray(self.picture).astype(float)
        mixed = rgb @ np.array([[.65,.12,.08],[.15,.65,.12],[.08,.15,.65]]) + 15
        decoder = RGBDecoder()
        self.assertEqual(len(decoder(Image.fromarray(mixed.clip(0,255).astype('uint8')))), 3)
        cropped = self.picture.crop((0, 0, self.picture.width, 185*4))
        self.assertEqual(decoder(cropped), [])
        self.assertGreater(decoder.stats["calibration_missing"], 0)
        self.assertEqual(len(decoder(self.picture)), 3)

    def test_crc_invalid_layer_cannot_advance_capture(self):
        from qr_transfer.capture import receive_frames
        from qr_transfer.protocol import decode
        bad = bytearray(self.packets[1]); bad[-1] ^= 1
        image = tile([self.packets[0], bytes(bad), self.packets[2]])
        decoder = RGBDecoder()
        now = [0.0]
        def grab():
            now[0] += 1
            return image
        with tempfile.TemporaryDirectory() as directory:
            result = receive_frames(Path(directory)/"state", grab, decoder, total_timeout=2,
                                    clock=lambda: now[0], sleep=lambda _: None)
            self.assertFalse(result["transfer"]["content_verified"])
            self.assertGreater(result["layers"]["0:1"]["invalid"], 0)
            self.assertEqual(result["transfer"]["received_bytes"], len(decode(self.packets[2]).payload))

    def test_schedule_profiles_and_padding(self):
        for layers in (1,2,3):
            for slots in (1,2):
                groups = schedule(19, 8, layers, slots)
                self.assertEqual(len(groups) % slots, 0)
                self.assertTrue(all(len(g) == layers for g in groups))
                self.assertEqual(set(range(1,20)), {v for g in groups for v in g if v > 0})
        with self.assertRaises(ValueError):
            layers_for("unknown")

    def test_late_metadata_partial_resume(self):
        from qr_transfer.storage import DurableSession
        decoder = RGBDecoder()
        items = decoder(self.picture)
        metadata = decoder(tile([self.packets[0]]*3))[0].raw
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"state"
            with DurableSession(path) as session:
                session.receiver.feed(items[0].raw)
                session.receiver.feed(metadata)
                self.assertFalse(session.snapshot()["content_verified"])
                self.assertEqual(session.snapshot()["received_chunks"], 1)
            with DurableSession(path, resume=True) as session:
                for item in items[1:]:
                    session.receiver.feed(item.raw)
                self.assertTrue(session.snapshot()["content_verified"])
            self.assertEqual((path/"object.bin").read_bytes(), self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
