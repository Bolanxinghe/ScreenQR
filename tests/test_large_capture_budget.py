"""Large-frame regression with synthetic capture only; no native/UI acceptance.

The payload contains an actually encoded public test QR, but Win32 capture and
native probes are replaced by the existing synthetic read-only fixture source.
No user pixels, Windows input, network or business operation is exercised.
"""
from dataclasses import replace
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from PIL import Image
import qrcode

from screenqr import native_capture as capture, native_extent as extent
from screenqr.core import decode_image
from screenqr.native_job import NativeJob
import test_native_capture_extent as capture_helpers
import test_native_port as port_helpers


class LargeCaptureBudgetTests(unittest.TestCase):
    def test_1293_by_969_encoded_payload_and_all_eight_strips_complete(self):
        width, height = 1293, 969
        payload = 'https://example.invalid/screenqr-public-large-capture-test'
        with Image.new('RGB', (width, height), 'white') as image:
            with qrcode.make(payload).get_image().convert('RGB') as qr:
                image.paste(qr, ((width-qr.width)//2, (height-qr.height)//2))
            decoded, = decode_image(image)
            self.assertEqual(decoded.text, payload)
            rgb8 = image.tobytes()
        original, mapping, probe = capture_helpers.fixture()
        frame = replace(original, requested_bbox=(100, 100, 1393, 1069),
            actual_bbox=(100, 100, 1393, 1069), width=width, height=height,
            rgb8=rgb8)
        mapping.update(source_capture_sha256=frame.capture_sha256,
            payload_bbox_physical=(900, 420, 2193, 1389),
            transport_bbox_physical=(892, 412, 2202, 1398),
            white_padding_bbox_physical=(876, 396, 2217, 1413))
        expected_pixels = width*height + 8*18*8
        self.assertEqual(expected_pixels, 1_254_069)
        self.assertGreater(expected_pixels, 1_000_000)
        self.assertLessEqual(capture.planned_capture_pixels(frame), capture.MAX_CAPTURE_PIXELS)
        self.assertEqual(capture.MAX_CAPTURE_PIXELS, 6_400_000)
        source, _ = capture_helpers.fake_source(frame, mapping, captured=frame.rgb8)
        clock = capture_helpers.Clock()
        api = SimpleNamespace(fixed_version=lambda _path: (4, 1, 15, 13))
        with patch.object(capture.sys, 'platform', 'win32'), \
                patch.object(capture.time, 'monotonic_ns', clock), \
                patch.object(capture, 'probe_screenshot_window', return_value=probe) as probes, \
                patch.object(capture, '_Gdi', return_value=api), \
                patch.object(capture, '_capture_rect', side_effect=source) as pixels:
            observation = capture.observe_native_selection_pixels(frame,
                stage_mapping=mapping, session_id=str(uuid4()),
                generation=frame.generation,
                deadline_monotonic_ns=clock.value+2_000_000_000)
            boundary = extent.derive_native_extent(observation)
        self.assertEqual(observation['status'], 'PAYLOAD_MATCH_EXTENT_UNVERIFIED')
        self.assertEqual(observation['errors'], [])
        self.assertEqual((probes.call_count, pixels.call_count), (2, 9))
        self.assertEqual(observation['captured_pixels'], expected_pixels)
        self.assertEqual(observation['planned_capture_pixels'], expected_pixels)
        self.assertEqual(len(observation['edge_observations']), 8)
        self.assertTrue(observation['payload_comparison']['exact_rgb_bytes_equal'])
        self.assertEqual(observation['payload_comparison']['captured_rgb_sha256'], frame.capture_sha256)
        self.assertEqual(boundary['status'], 'VERIFIED')
        self.assertEqual(boundary['observed_selection_bbox_physical'], mapping['transport_bbox_physical'])
        self.assertTrue(boundary['requested_transport_matches'])
        self.assertFalse(boundary['raw_payload_verified'])
        self.assertFalse(boundary['commit_authorization'])
        self.assertFalse(observation['native_input_sent'])
        self.assertFalse(observation['raw_pixels_persisted'])
        self.assertEqual(frame.rgb8, rgb8)
        self.assertEqual(frame.capture_sha256, hashlib.sha256(rgb8).hexdigest())

    def test_frame_at_screen_area_plus_border_budget_stops_before_counter_or_native_setup(self):
        original, original_mapping, _, _ = port_helpers.fixture()
        # A full-screen payload alone fits the cap; its mandatory border reads
        # exceed it. The typed preview deliberately extends off-screen, which
        # admission must reject before any platform, decoder or input adapter.
        width, height = 3200, 2000
        frame = replace(original, requested_bbox=(0, 0, width, height),
            actual_bbox=(0, 0, width, height), width=width, height=height,
            rgb8=b'\x00'*(width*height*3))
        own = replace(original_mapping.own_stage,
            top_window_bbox_physical=(0, 0, 3330, 2120),
            top_client_bbox_physical=(8, 24, 3320, 2110),
            image_bbox_physical=(64, 64, 3264, 2064),
            padding_bbox_physical=(40, 40, 3288, 2088))
        mapping = replace(original_mapping, source_capture_sha256=frame.capture_sha256,
            payload_width=width, payload_height=height,
            payload_bbox_physical=own.image_bbox_physical,
            transport_bbox_physical=(56, 56, 3272, 2072),
            payload_offset=(8, 8), white_padding_bbox_physical=own.padding_bbox_physical,
            own_stage=own)
        self.assertGreater(capture.planned_capture_pixels(frame), capture.MAX_CAPTURE_PIXELS)
        counter, port, observer, controller = Mock(), Mock(), Mock(), Mock()
        job = NativeJob(frame, mapping, entered_ns=1_000_000_000,
            single_target_confirmed=True, clock=lambda:1_100_000_000,
            counter=counter, port_factory=port, observer_factory=observer,
            controller_factory=controller)
        input_deadline, result_deadline = job.input_deadline_ns, job.result_deadline_ns
        job.run()
        self.assertTrue(job.done.is_set())
        self.assertEqual(job.result.status, 'FRAME_TOO_LARGE')
        self.assertEqual(job.result.reason, 'total_capture_exceeds_pixel_budget')
        self.assertFalse(job.result.dispatch_attempted)
        self.assertEqual(job.result.dispatch_count, 0)
        self.assertEqual(job.result.cleanup_status, 'NOT_NEEDED')
        self.assertTrue(job.result.retry_safe_before_input)
        for unused in (counter, port, observer, controller):
            unused.assert_not_called()
        self.assertEqual((job.input_deadline_ns, job.result_deadline_ns),
            (input_deadline, result_deadline))


if __name__ == '__main__':
    unittest.main()
