"""Pure declared-guard geometry and metadata; no Win32/UI/business test."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_native_capture_extent as fixtures
from screenqr import native_capture as capture
from screenqr.stage import SUPPORTED_STAGE_PADDING_BY_GUARD, even_transport_bbox


class NativeCapturePaddingTests(unittest.TestCase):
    def mapping(self, guard=16, pad=32, *, odd=False):
        frame, mapping, probe = fixtures.fixture()
        if odd:
            mapping["payload_bbox_physical"] = (1201, 701, 1361, 861)
        left, top, right, bottom = mapping["payload_bbox_physical"]
        white = (left-pad, top-pad, right+pad, bottom+pad)
        mapping.update(guard_physical_px=guard, white_padding_physical_px=pad,
            white_padding_bbox_physical=white,
            transport_bbox_physical=even_transport_bbox(mapping["payload_bbox_physical"], white, guard_physical_px=guard))
        return frame, mapping, probe

    def test_legacy_plan_default_and_declared_metadata_remain_eight_twentyfour(self):
        frame, mapping, _ = fixtures.fixture()
        plan = capture._plan(frame, mapping, frame.generation)
        self.assertEqual((plan.guard_physical_px, plan.white_padding_physical_px), (8, 24))
        old_constructor = capture._Plan(plan.frame_id, plan.generation, plan.source_capture_sha256,
            plan.topology_sha256, plan.stage_id, plan.stage_epoch, plan.payload, plan.transport_requested, plan.white_container)
        self.assertEqual(old_constructor.metadata_only(), plan.metadata_only())
        self.assertIs(capture.SUPPORTED_STAGE_PADDING_BY_GUARD, SUPPORTED_STAGE_PADDING_BY_GUARD)

    def test_live_pair_carries_actual_declaration_without_changing_payload_or_strip_profile(self):
        for odd in (False, True):
            with self.subTest(odd=odd):
                frame, mapping, _ = self.mapping(odd=odd)
                original = frame.rgb8
                plan = capture._plan(frame, mapping, frame.generation)
                metadata = plan.metadata_only()
                self.assertEqual((metadata["guard_physical_px"], metadata["white_padding_physical_px"]), (16, 32))
                self.assertEqual(plan.transport_requested, mapping["transport_bbox_physical"])
                self.assertTrue(all(edge % 2 == 0 for edge in plan.transport_requested))
                offsets = (plan.payload[0]-plan.transport_requested[0], plan.payload[1]-plan.transport_requested[1])
                self.assertTrue(all(offset in (16, 17) for offset in offsets))
                sections = capture._sections(plan)
                self.assertEqual(len(sections), 8)
                for section in sections:
                    box = section.bbox
                    self.assertEqual((box[2]-box[0], box[3]-box[1]),
                        (8, 18) if section.normal_axis == "y" else (18, 8))
                    self.assertTrue(capture._contains(plan.white_container, box))
                self.assertEqual((capture.OUTER_NORMAL_PX, capture.INNER_NORMAL_PX), (12, 6))
                self.assertIs(frame.rgb8, original)
                self.assertEqual(metadata["source_capture_sha256"], frame.capture_sha256)

    def test_mixed_unknown_or_nonnumeric_pair_is_rejected(self):
        frame, mapping, _ = fixtures.fixture()
        for guard, pad in ((8, 32), (16, 24), (12, 28), (True, 24), (8, 24.0)):
            with self.subTest(pair=(guard, pad)), self.assertRaises(capture._SelectionObservationError):
                capture._plan(frame, dict(mapping, guard_physical_px=guard, white_padding_physical_px=pad), frame.generation)

    def test_declared_live_pair_rejects_old_transport_or_insufficient_physical_white(self):
        frame, mapping, _ = self.mapping()
        _, old, _ = fixtures.fixture()
        for changes in ({"transport_bbox_physical": old["transport_bbox_physical"]},
                        {"white_padding_bbox_physical": old["white_padding_bbox_physical"]}):
            with self.subTest(changes=changes), self.assertRaises(capture._SelectionObservationError):
                capture._plan(frame, dict(mapping, **changes), frame.generation)

    def test_full_numeric_observation_preserves_live_pair_in_stage_metadata(self):
        frame, mapping, probe = self.mapping()
        acquire, _ = fixtures.fake_source(frame, mapping)
        clock = fixtures.Clock()
        api = SimpleNamespace(fixed_version=lambda path: (4, 1, 15, 13))
        with patch.object(capture.sys, "platform", "win32"), \
                patch.object(capture.time, "monotonic_ns", clock), \
                patch.object(capture, "probe_screenshot_window", return_value=probe), \
                patch.object(capture, "_Gdi", return_value=api), \
                patch.object(capture, "_capture_rect", side_effect=acquire):
            observation = capture.observe_native_selection_pixels(frame, stage_mapping=mapping,
                session_id=str(uuid4()), generation=frame.generation,
                deadline_monotonic_ns=clock.value+2_000_000_000)
        self.assertEqual(observation["status"], "PAYLOAD_MISMATCH")
        observed_stage = observation["stage_mapping"]
        self.assertEqual((observed_stage["guard_physical_px"], observed_stage["white_padding_physical_px"]), (16, 32))
        self.assertEqual(observed_stage["transport_requested_bbox_physical"], mapping["transport_bbox_physical"])
        self.assertEqual(observation["payload_comparison"]["captured_bbox_physical"], mapping["payload_bbox_physical"])
        self.assertEqual(observation["payload_comparison"]["expected_rgb_sha256"], frame.capture_sha256)
        self.assertEqual(len(observation["edge_observations"]), 8)
        self.assertFalse(observation["native_input_sent"])
        self.assertFalse(observation["raw_pixels_persisted"])


if __name__ == "__main__":
    unittest.main()
