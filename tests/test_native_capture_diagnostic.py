"""Synthetic numeric-only diagnostics; no native/UI/network/proof acceptance."""
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
import test_native_capture_extent as fixtures
from screenqr import native_capture as capture
from screenqr.handoff import _PROFILE_A_TABLE

_UNSET = object()


class PayloadDifferenceDiagnosticTests(unittest.TestCase):
    def difference(self, image, expected):
        return capture._payload_numeric_difference(image, expected, hashlib.sha256(image.tobytes()).hexdigest())

    def observe(self, expected=_UNSET):
        frame, mapping, probe = fixtures.fixture()
        acquire, _ = fixtures.fake_source(frame, mapping)
        clock = fixtures.Clock()
        api = SimpleNamespace(fixed_version=lambda path: (4, 1, 15, 13))
        options = {} if expected is _UNSET else {"diagnostic_expected_rgb8": expected}
        with patch.object(capture.sys, "platform", "win32"), \
                patch.object(capture.time, "monotonic_ns", clock), \
                patch.object(capture, "probe_screenshot_window", return_value=probe) as query, \
                patch.object(capture, "_Gdi", return_value=api), \
                patch.object(capture, "_capture_rect", side_effect=acquire):
            result = capture.observe_native_selection_pixels(frame, stage_mapping=mapping,
                session_id=str(uuid4()), generation=1, deadline_monotonic_ns=clock.value+2_000_000_000, **options)
        return result, query.call_count

    def test_identical_pixels_have_zero_difference_and_input_remains_untouched(self):
        with Image.new("RGB", (4, 3), (10, 20, 30)) as image:
            expected = image.tobytes()
            result = self.difference(image, expected)
            self.assertEqual(result["different_pixel_count"], 0)
            self.assertIsNone(result["difference_bbox_payload_relative"])
            self.assertEqual(result["channel_max_abs_delta"], [0, 0, 0])
            self.assertEqual(result["channels_equal"], [True]*3)
            self.assertTrue(result["all_pixels_equal"])
            self.assertTrue(result["diagnostic_not_authorization"])
            self.assertEqual(result["expected_rgb_sha256"], result["observed_rgb_sha256"])
            self.assertEqual(image.tobytes(), expected)

    def test_union_bbox_counts_local_differences_in_distinct_channels(self):
        expected = bytes(4*3*3)
        with Image.frombytes("RGB", (4, 3), expected) as image:
            image.putpixel((1, 2), (5, 0, 0))
            image.putpixel((3, 1), (0, 0, 9))
            result = self.difference(image, expected)
        self.assertEqual(result["different_pixel_count"], 2)
        self.assertEqual(result["difference_bbox_payload_relative"], (1, 1, 4, 3))
        self.assertEqual(result["channel_max_abs_delta"], [5, 0, 9])
        self.assertEqual(result["channels_equal"], [False, True, False])
        self.assertFalse(result["all_pixels_equal"])

    def test_each_single_channel_change_is_detected(self):
        expected = bytes(3*3*3)
        for channel in range(3):
            with self.subTest(channel=channel), Image.frombytes("RGB", (3, 3), expected) as image:
                pixel = [0, 0, 0]
                pixel[channel] = 7
                image.putpixel((2, 1), tuple(pixel))
                result = self.difference(image, expected)
                self.assertEqual(result["different_pixel_count"], 1)
                self.assertEqual(result["difference_bbox_payload_relative"], (2, 1, 3, 2))
                self.assertEqual(result["channel_max_abs_delta"], pixel)

    def test_default_does_not_compute_diagnostic_and_enabled_mode_keeps_capture_status(self):
        with patch.object(capture, "_payload_numeric_difference", side_effect=AssertionError("must not run")) as compare:
            baseline, calls = self.observe()
        compare.assert_not_called()
        self.assertEqual(calls, 2)
        self.assertNotIn("payload_difference_diagnostic", baseline)
        frame, _, _ = fixtures.fixture()
        enabled, calls = self.observe(frame.rgb8.translate(_PROFILE_A_TABLE))
        self.assertEqual(calls, 2)
        self.assertEqual(enabled["status"], baseline["status"])
        self.assertEqual(enabled["payload_comparison"]["captured_rgb_sha256"],
                         baseline["payload_comparison"]["captured_rgb_sha256"])
        self.assertEqual(enabled["payload_difference_diagnostic"]["different_pixel_count"], 0)
        self.assertTrue(enabled["payload_difference_diagnostic"]["diagnostic_not_authorization"])
        self.assertFalse(enabled["native_input_sent"])
        self.assertFalse(enabled["raw_pixels_persisted"])

    def test_invalid_expected_type_or_length_rejects_before_any_native_query(self):
        frame, _, _ = fixtures.fixture()
        for expected in ("invalid", bytearray(frame.rgb8), b"too short"):
            with self.subTest(kind=type(expected).__name__):
                result, calls = self.observe(expected)
                self.assertEqual(calls, 0)
                self.assertEqual(result["status"], "REJECTED")
                self.assertIn("diagnostic_expected_rgb8_requires_exact_payload_bytes", result["errors"])
                self.assertNotIn("payload_difference_diagnostic", result)


if __name__ == "__main__":
    unittest.main()
