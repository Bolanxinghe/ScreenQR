"""Actual multi-scale QR decoding and instance isolation, without network/UI."""
import hashlib
from concurrent.futures import CancelledError
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageOps
import qrcode

from screenqr.core import decode_image
from screenqr.decoding import (
    MAX_INSTANCES, MAX_PYRAMID_LEVELS, MAX_TILE_COUNT, TILE_EDGE,
    pyramid_sizes, read_qr_instances, tile_boxes,
)
from screenqr.image_limits import MAX_IMAGE_EDGE, MAX_IMAGE_PIXELS, validate_image_size


def make_qr(text, box_size=1):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=box_size, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("L")


class MultiscaleDecodeTests(unittest.TestCase):
    def test_single_pixel_modules_and_source_unchanged(self):
        image = make_qr("SCREENQR-ONE-PIXEL-MODULES")
        before = hashlib.sha256(image.tobytes()).digest()
        self.assertEqual([r.text for r in decode_image(image)], ["SCREENQR-ONE-PIXEL-MODULES"])
        self.assertEqual(hashlib.sha256(image.tobytes()).digest(), before)
        self.assertEqual(image.mode, "L")

    def test_8k_large_and_tiny_codes_both_survive(self):
        image = Image.new("L", (7680, 4320), 255)
        giant = make_qr("SCREENQR-GIANT-8K", box_size=132)
        tiny = make_qr("SCREENQR-TINY-8K", box_size=1)
        image.paste(giant, (40, 40))
        image.paste(tiny, (7600, 4200))
        self.assertEqual({r.text for r in decode_image(image)}, {"SCREENQR-GIANT-8K", "SCREENQR-TINY-8K"})

    def test_16k_panorama_tiny_code_near_far_edge(self):
        image = Image.new("L", (16384, 1024), 255)
        tiny = make_qr("SCREENQR-16K-EDGE", box_size=1)
        image.paste(tiny, (16384 - tiny.width - 1, 1024 - tiny.height - 1))
        self.assertEqual([r.text for r in decode_image(image)], ["SCREENQR-16K-EDGE"])

    def test_dense_medium_symbol_across_large_image_tile_seams(self):
        payload = "SCREENQR-DENSE-GAP-" + "0123456789ABCDEF" * 100
        qr = qrcode.QRCode(version=40, error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=4, border=4)
        qr.add_data(payload)
        qr.make(fit=False)
        symbol = qr.make_image().convert("L")
        self.assertEqual(symbol.size, (740, 740))
        with Image.new("L", (12000, 4000), 255) as image:
            image.paste(symbol, (1000, 1000))
            # Original + overview only demonstrably misses this placement.
            # Keep the negative control to prove the intermediate layers matter.
            with patch("screenqr.decoding.pyramid_sizes", return_value=()):
                self.assertEqual(read_qr_instances(image), [])
            found = read_qr_instances(image)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].text, payload)
            self.assertAlmostEqual(found[0].bbox[0], 1016, delta=4)
            self.assertAlmostEqual(found[0].bbox[1], 1016, delta=4)

    def test_pyramid_levels_are_finite_and_skip_overview_duplication(self):
        self.assertEqual(pyramid_sizes(12000, 4000), ((6000, 2000), (3000, 1000)))
        self.assertEqual(len(pyramid_sizes(65536, 2441)), MAX_PYRAMID_LEVELS)
        self.assertEqual(pyramid_sizes(2560, 1440), ())

    def test_same_payload_in_distinct_positions_remains_two_instances(self):
        image = Image.new("L", (3200, 2000), 255)
        qr = make_qr("SCREENQR-SAME-TWO", box_size=6)
        image.paste(qr, (1300, 200))
        image.paste(qr, (2500, 1300))
        instances = read_qr_instances(image)
        self.assertEqual(len(instances), 2)
        self.assertEqual({r.text for r in instances}, {"SCREENQR-SAME-TWO"})
        self.assertLess(min(r.bbox[0] for r in instances), 1500)
        self.assertGreater(max(r.bbox[0] for r in instances), 2500)
        self.assertEqual(len(decode_image(image)), 1)

    def test_overlap_does_not_duplicate_one_instance(self):
        image = Image.new("L", (3600, 2600), 255)
        qr = make_qr("SCREENQR-OVERLAP", box_size=8)
        image.paste(qr, (1300, 1300))
        instances = read_qr_instances(image)
        self.assertEqual(len(instances), 1)
        left, top, right, bottom = instances[0].bbox
        self.assertAlmostEqual(left, 1332, delta=3)
        self.assertAlmostEqual(top, 1332, delta=3)
        self.assertGreater(right, left)
        self.assertGreater(bottom, top)

    def test_tiny_rotated_inverted_on_large_canvas(self):
        image = Image.new("L", (4200, 2300), 0)
        qr = ImageOps.invert(make_qr("SCREENQR-INVERTED-TINY", box_size=1)).rotate(90, expand=True)
        image.paste(qr, (3200, 1700))
        self.assertEqual([r.text for r in decode_image(image)], ["SCREENQR-INVERTED-TINY"])

    def test_size_rejection_precedes_any_decode(self):
        image = Image.new("L", (1, 1), 255)
        with patch.object(image, "_size", (MAX_IMAGE_EDGE + 1, 1)):
            with patch("screenqr.decoding.zxingcpp.read_barcodes") as decoder:
                with self.assertRaisesRegex(ValueError, "图片太大"):
                    decode_image(image)
                decoder.assert_not_called()

    def test_pixel_budget_and_edge_budget_are_distinct(self):
        validate_image_size((16384, 8192))
        validate_image_size((MAX_IMAGE_EDGE, 1))
        with self.assertRaises(ValueError):
            validate_image_size((16000, 10001))
        self.assertEqual(MAX_IMAGE_PIXELS, 160_000_000)

    def test_too_many_instances_raises_instead_of_returning_partial_results(self):
        image = Image.new("L", (8, 8), 255)
        image.putpixel((1, 1), 0)
        point = SimpleNamespace(x=2, y=2)
        position = SimpleNamespace(top_left=point, top_right=SimpleNamespace(x=6, y=2),
                                   bottom_right=SimpleNamespace(x=6, y=6), bottom_left=SimpleNamespace(x=2, y=6))
        symbols = [SimpleNamespace(valid=True, text=f"SCREENQR-LIMIT-{i}", format="QR Code", position=position)
                   for i in range(MAX_INSTANCES + 1)]
        with patch("screenqr.decoding.zxingcpp.read_barcodes", return_value=symbols):
            with self.assertRaisesRegex(ValueError, "超过512"):
                read_qr_instances(image)

    def test_expired_deadline_raises_before_decoder(self):
        with patch("screenqr.decoding.zxingcpp.read_barcodes") as decoder:
            with self.assertRaises(TimeoutError):
                read_qr_instances(make_qr("SCREENQR-DEADLINE"), deadline_ns=1)
            decoder.assert_not_called()

    def test_public_decoder_passes_deadline_without_partial_result(self):
        with self.assertRaises(TimeoutError):
            decode_image(make_qr("SCREENQR-DEADLINE-PUBLIC"), deadline_ns=1)

    def test_default_decode_has_no_elapsed_time_budget(self):
        # No fixed clock is consulted for a normal decode. Long images finish
        # their finite pass plan instead of receiving the former 15 s cutoff.
        with patch("screenqr.decoding.time.monotonic_ns", side_effect=AssertionError("unexpected clock limit")):
            found = decode_image(make_qr("SCREENQR-NO-TIME-LIMIT"))
        self.assertEqual([r.text for r in found], ["SCREENQR-NO-TIME-LIMIT"])

    def test_cancel_before_or_during_native_decode_returns_no_partial_list(self):
        import screenqr.decoding as decoding
        image = make_qr("SCREENQR-CANCELLED")
        stop = threading.Event()
        stop.set()
        with patch("screenqr.decoding.zxingcpp.read_barcodes") as decoder:
            with self.assertRaises(CancelledError):
                decode_image(image, cancelled=stop.is_set)
            decoder.assert_not_called()

        native_read = decoding.zxingcpp.read_barcodes
        stop.clear()

        def cancelled_after_success(*args, **kwargs):
            symbols = native_read(*args, **kwargs)
            self.assertEqual(len(symbols), 1)
            stop.set()
            return symbols

        with patch("screenqr.decoding.zxingcpp.read_barcodes", side_effect=cancelled_after_success) as decoder:
            with self.assertRaises(CancelledError):
                decode_image(image, cancelled=stop.is_set)
            self.assertEqual(decoder.call_count, 1)

    def test_cancel_after_native_list_before_publication_returns_no_result(self):
        from screenqr.core import classify_payload
        stop = threading.Event()

        def classify_then_cancel(*args):
            result = classify_payload(*args)
            stop.set()
            return result

        with patch("screenqr.core.classify_payload", side_effect=classify_then_cancel):
            with self.assertRaises(CancelledError):
                decode_image(make_qr("SCREENQR-CANCEL-PUBLICATION"), cancelled=stop.is_set)

    def test_tiles_cover_corners_edges_and_overlap_with_finite_count(self):
        for width, height in ((7680, 4320), (16384, 8192), (65536, 2441), (1, 65536)):
            with self.subTest(size=(width, height)):
                boxes = tile_boxes(width, height)
                self.assertLessEqual(len(boxes), MAX_TILE_COUNT)
                for box in boxes:
                    self.assertLessEqual(box[2] - box[0], TILE_EDGE)
                    self.assertLessEqual(box[3] - box[1], TILE_EDGE)
                    self.assertGreater(box[2], box[0])
                    self.assertGreater(box[3], box[1])
                for x, y in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1), (width // 2, height // 2)):
                    self.assertTrue(any(l <= x < r and t <= y < b for l, t, r, b in boxes))


if __name__ == "__main__":
    unittest.main()
