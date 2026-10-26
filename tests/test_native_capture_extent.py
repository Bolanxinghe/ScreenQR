"""Synthetic read-only acquisition boundaries, never native/business acceptance.

No window query, Win32 capture, user asset, file fixture, UI action or network is
used. The fake capture source tests rejection logic and independent geometry.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

from screenqr import native_capture as capture, native_extent as extent
from screenqr.frozen_frame import FrozenFrame, MonitorDescriptor, TopologyDescriptor, TOPOLOGY_KIND
from screenqr.handoff import FrameBinding, NativeSnapshot, SelectionExtentReceipt, StageReceipt
from screenqr.native_identity import NativeWindowIdentity, WindowProbeResult


class Clock:
    def __init__(self):
        self.value = 1_000_000_000

    def __call__(self):
        self.value += 1_000
        return self.value


def fixture():
    topology = TopologyDescriptor(TOPOLOGY_KIND, (0, 0, 3200, 2000),
        (MonitorDescriptor("DISPLAY1", (0, 0, 3200, 2000), 192, 192, True, 0),))
    frame = FrozenFrame(str(uuid4()), 1, (100, 100, 260, 260), (100, 100, 260, 260),
        160, 160, bytes((0, 128, 255)) * (160 * 160), 0.0, 0.001, topology)
    mapping = dict(frame_id=frame.frame_id, generation=1,
        source_capture_sha256=frame.capture_sha256, topology_sha256=frame.topology_sha256,
        stage_id=str(uuid4()), stage_epoch=3, payload_bbox_physical=(1200, 700, 1360, 860),
        transport_bbox_physical=(1192, 692, 1368, 868),
        white_padding_bbox_physical=(1176, 676, 1384, 884),
        guard_physical_px=8, white_padding_physical_px=24)
    native = NativeWindowIdentity(901, 700, 123456,
        r"C:\Program Files\Tencent\Weixin\Weixin.exe", "Weixin", "Qt51514QWindowIcon",
        (0, 0, 3200, 2000), (0, 0, 3200, 2000), 192, True)
    probe = WindowProbeResult("UNIQUE", "synthetic", (native,), (0, 0, 3200, 2000),
        1, 2, 901, 901, {}, ())
    return frame, mapping, probe


def fake_source(frame, mapping, *, outward=0, captured=None):
    plan = capture._plan(frame, mapping, frame.generation)
    sections = {section.bbox: section for section in capture._sections(plan)}
    left, top, right, bottom = plan.transport_requested
    actual = dict(left=left - outward, top=top - outward,
                  right=right + outward, bottom=bottom + outward)
    payload = bytes(max(value - 1, 0) for value in frame.rgb8) if captured is None else captured

    def acquire(api, identity, rect, approved_container):
        if rect == plan.payload:
            return Image.frombytes("RGB", (frame.width, frame.height), payload)
        section = sections[rect]
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        image = Image.new("RGB", (width, height))
        edge = actual[section.side]
        for y in range(height):
            for x in range(width):
                coordinate = rect[1] + y if section.normal_axis == "y" else rect[0] + x
                if section.side in ("top", "left"):
                    colour = extent.GRAY if coordinate < edge - 5 else extent.GREEN if coordinate <= edge else extent.WHITE
                else:
                    colour = extent.WHITE if coordinate < edge - 1 else extent.GREEN if coordinate <= edge + 4 else extent.GRAY
                image.putpixel((x, y), colour)
        return image

    return acquire, payload


def acquire_observation(*, outward=0, after_change=False, version_change=False, raw_exact=False):
    frame, mapping, before = fixture()
    after = replace(before, candidates=(replace(before.candidates[0], pid=701),)) if after_change else before
    source, payload = fake_source(frame, mapping, outward=outward,
        captured=frame.rgb8 if raw_exact else None)
    clock = Clock()
    versions = [(4, 1, 15, 13), (4, 1, 15, 14) if version_change else (4, 1, 15, 13)]
    api = SimpleNamespace(fixed_version=lambda path: versions.pop(0))
    with patch.object(capture.sys, "platform", "win32"), \
         patch.object(capture.time, "monotonic_ns", clock), \
         patch.object(capture, "probe_screenshot_window", side_effect=[before, after]) as probe, \
         patch.object(capture, "_Gdi", return_value=api), \
         patch.object(capture, "_capture_rect", side_effect=source) as pixels:
        observation = capture.observe_native_selection_pixels(frame, stage_mapping=mapping,
            session_id=str(uuid4()), generation=1, deadline_monotonic_ns=clock.value + 2_000_000_000)
    return frame, mapping, observation, clock, payload, probe.call_count, pixels.call_count


class NativeCaptureTests(unittest.TestCase):
    def test_payload_and_eight_strips_are_numeric_and_raw_mismatch_is_preserved(self):
        frame, mapping, observation, clock, payload, probes, captures = acquire_observation()
        self.assertEqual(observation["status"], "PAYLOAD_MISMATCH")
        self.assertEqual((probes, captures), (2, 9))
        comparison = observation["payload_comparison"]
        self.assertFalse(comparison["exact_rgb_bytes_equal"])
        self.assertEqual(comparison["captured_rgb_sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(comparison["expected_rgb_sha256"], frame.capture_sha256)
        self.assertEqual(comparison["captured_bbox_physical"], mapping["payload_bbox_physical"])
        self.assertEqual(len(observation["edge_observations"]), 8)
        self.assertEqual(observation["version_metadata"]["fixed_file_version_after"], [4, 1, 15, 13])
        self.assertEqual(observation["captured_pixels"], 160 * 160 + 8 * 18 * 8)
        self.assertEqual(observation["selection_extent_status"], "UNVERIFIED")
        self.assertIsNone(observation["observed_selection_bbox_physical"])
        self.assertNotIn("content_diagnostics", observation)

    def test_exact_raw_payload_still_does_not_claim_extent(self):
        observation = acquire_observation(raw_exact=True)[2]
        self.assertEqual(observation["status"], "PAYLOAD_MATCH_EXTENT_UNVERIFIED")
        self.assertTrue(observation["payload_comparison"]["exact_rgb_bytes_equal"])
        self.assertIsNone(observation["observed_selection_bbox_physical"])

    def test_native_instance_change_after_pixels_rejects_result(self):
        observation = acquire_observation(after_change=True)[2]
        self.assertEqual(observation["status"], "REJECTED")
        self.assertIn("native_identity_changed_across_observation", observation["errors"])

    def test_file_version_change_after_pixels_rejects_result(self):
        observation = acquire_observation(version_change=True)[2]
        self.assertEqual(observation["status"], "REJECTED")
        self.assertIn("fixed_file_version_changed_across_observation", observation["errors"])

    def test_expired_deadline_or_generation_mismatch_never_queries_native_ui(self):
        frame, mapping, probe = fixture()
        for generation, deadline, expected in ((1, 999, "STALE"), (2, 2_000_000_000, "REJECTED")):
            with self.subTest(generation=generation), \
                 patch.object(capture.sys, "platform", "win32"), \
                 patch.object(capture.time, "monotonic_ns", return_value=1_000), \
                 patch.object(capture, "probe_screenshot_window") as query, \
                 patch.object(capture, "_Gdi") as api:
                result = capture.observe_native_selection_pixels(frame, stage_mapping=mapping,
                    session_id=str(uuid4()), generation=generation, deadline_monotonic_ns=deadline)
                self.assertEqual(result["status"], expected)
                query.assert_not_called()
                api.assert_not_called()

    def test_unknown_profile_foreground_or_incomplete_probe_fails_closed(self):
        frame, mapping, probe = fixture()
        for bad in (
            replace(probe, errors=("incomplete",)),
            replace(probe, candidates=(replace(probe.candidates[0], dpi=144),)),
            replace(probe, foreground_hwnd_at_end=777),
            replace(probe, candidates=(replace(probe.candidates[0], executable_path=r"C:\other.exe"),)),
        ):
            with self.subTest(probe=bad):
                with self.assertRaises(capture._ObserverError):
                    capture._require_eligible_probe(bad)


class NativeExtentTests(unittest.TestCase):
    def derive(self, observation, clock):
        with patch.object(extent.time, "monotonic_ns", clock):
            return extent.derive_native_extent(observation)

    def test_independent_edges_verify_requested_geometry(self):
        frame, mapping, observation, clock, *_ = acquire_observation()
        result = self.derive(observation, clock)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["observed_selection_bbox_physical"], mapping["transport_bbox_physical"])
        self.assertTrue(result["requested_transport_matches"])
        self.assertEqual(result["source_evidence_id"], observation["evidence_id"])
        self.assertFalse(result["raw_payload_verified"])
        self.assertFalse(result["commit_authorization"])

    def test_outward_negative_does_not_copy_requested_coordinates(self):
        frame, mapping, observation, clock, *_ = acquire_observation(outward=4)
        result = self.derive(observation, clock)
        left, top, right, bottom = mapping["transport_bbox_physical"]
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["observed_selection_bbox_physical"], (left - 4, top - 4, right + 4, bottom + 4))
        self.assertFalse(result["requested_transport_matches"])

    def test_requested_reference_cannot_determine_actual_extent(self):
        frame, mapping, observation, clock, *_ = acquire_observation()
        before = self.derive(observation, clock)
        observation["stage_mapping"]["transport_requested_bbox_physical"] = (1190, 690, 1370, 870)
        for section in observation["edge_observations"]:
            section["requested_edge_coordinate"] = 42
        after = self.derive(observation, clock)
        self.assertEqual(after["status"], "VERIFIED")
        self.assertEqual(before["observed_selection_bbox_physical"], after["observed_selection_bbox_physical"])
        self.assertFalse(after["requested_transport_matches"])

    def test_converter_preserves_original_source_fields_for_both_receipt_factories(self):
        frame, mapping, observation, clock, *_ = acquire_observation()
        before = deepcopy(observation)
        with patch.object(extent.time, "monotonic_ns", clock):
            merged = extent.extent_evidence_from_observation(observation)
        for name in ("evidence_id", "session_id", "status", "frame_id", "stage_id", "generation",
                     "payload_comparison", "native_identity_before", "native_identity_after",
                     "started_monotonic_ns", "finished_monotonic_ns", "capture_completed_monotonic_ns"):
            self.assertEqual(merged[name], before[name])
        self.assertEqual(merged["source_evidence_id"], before["evidence_id"])
        self.assertEqual(observation, before)
        self.assertIsNot(merged["payload_comparison"], observation["payload_comparison"])
        stage = StageReceipt(FrameBinding.from_frame(frame), mapping["stage_id"], 3,
            mapping["payload_bbox_physical"], mapping["transport_bbox_physical"], "0" * 64, 900)
        native = NativeSnapshot.from_probe(observation["native_identity_before"],
            started_ns=observation["started_monotonic_ns"], finished_ns=observation["finished_monotonic_ns"],
            fixed_file_version=(4, 1, 15, 13)).instance
        receipt = SelectionExtentReceipt.from_native_extent_evidence(stage=stage, native_instance=native, evidence=merged)
        self.assertEqual(receipt.evidence_id, observation["evidence_id"])
        self.assertEqual(receipt.actual_selection_bbox_physical, mapping["transport_bbox_physical"])

    def test_missing_duplicate_nonuniform_disagreeing_or_corrupt_sections_reject(self):
        frame, mapping, observation, clock, *_ = acquire_observation()
        malformed = []
        missing = deepcopy(observation); missing["edge_observations"].pop(); malformed.append(missing)
        duplicate = deepcopy(observation); duplicate["edge_observations"][1] = deepcopy(duplicate["edge_observations"][0]); malformed.append(duplicate)
        nonuniform = deepcopy(observation); nonuniform["edge_observations"][0]["normal_profiles"][0]["rgb_max"][0] += 1; malformed.append(nonuniform)
        corrupt = deepcopy(observation); corrupt["edge_observations"][0]["normal_profiles"][7]["rgb_mean"] = [0, 0, 0]; malformed.append(corrupt)
        disagree = deepcopy(observation)
        rows = disagree["edge_observations"][4]["normal_profiles"]
        colours = [(row["rgb_min"], row["rgb_max"], row["rgb_mean"], row["exact_source_white_pixel_count"]) for row in rows]
        for index, row in enumerate(rows):
            row["rgb_min"], row["rgb_max"], row["rgb_mean"], row["exact_source_white_pixel_count"] = colours[max(0, index - 1)]
        malformed.append(disagree)
        wrong_time = deepcopy(observation); wrong_time["edge_observations"][0]["capture_started_monotonic_ns"] = 1; malformed.append(wrong_time)
        for value in malformed:
            with self.subTest(value=value):
                self.assertEqual(self.derive(value, clock)["status"], "REJECTED")
                with patch.object(extent.time, "monotonic_ns", clock), self.assertRaises(ValueError):
                    extent.extent_evidence_from_observation(value)

    def test_stale_mixed_source_or_unknown_version_cannot_become_extent(self):
        frame, mapping, observation, clock, *_ = acquire_observation()
        for mutate in (
            lambda value: value.update(generation=2),
            lambda value: value["version_metadata"].update(fixed_file_version=[4, 1, 15, 14]),
            lambda value: value["native_identity_after"]["candidates"][0].update(pid=701),
            lambda value: value.update(finished_monotonic_ns=value["deadline_monotonic_ns"]),
        ):
            value = deepcopy(observation); mutate(value)
            self.assertEqual(self.derive(value, clock)["status"], "REJECTED")
        clock.value = observation["deadline_monotonic_ns"]
        self.assertEqual(self.derive(observation, clock)["status"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
