"""Injected read-only readiness transitions, never real/UI/business acceptance.

Synthetic image/metadata generation uses no actual Win32, desktop, user assets,
network or input. A later complete verified sample is required; waiting alone
never creates extent evidence or weakens any calibrated colour/border rule.
"""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from test_native_capture_extent import acquire_observation
from screenqr import native_extent, native_port as module
from screenqr.handoff import FrameBinding, NativeSnapshot, StageReceipt
from screenqr.native_identity import WindowProbeResult
from screenqr.stage import FrozenStageMapping, OwnStageSnapshot


class AsyncReadinessTests(unittest.TestCase):
    def setUp(self):
        self.input, self.api, self.own_api = MagicMock(), MagicMock(), MagicMock()
        self.api.fixed_version.return_value = (4, 1, 15, 13)
        for name, replacement in (("_Gdi", lambda: self.api),
                ("_StageApi", lambda: self.own_api),
                ("WeChatInput", lambda **kw: self.input),
                ("stage_mapping_is_current", lambda *args: True)):
            patched = patch.object(module, name, replacement)
            patched.start()
            self.addCleanup(patched.stop)
        self.configure()
        patched = patch.object(native_extent.time, "monotonic_ns", lambda: self.clock())
        patched.start()
        self.addCleanup(patched.stop)

    def configure(self, *, outward=0):
        self.frame, source_mapping, self.base, self.clock, *_ = acquire_observation(outward=outward)
        own = OwnStageSnapshot(777, 778, 779, 123, (1150, 550, 1420, 1000),
            (1160, 560, 1410, 990), source_mapping["payload_bbox_physical"],
            source_mapping["white_padding_bbox_physical"], 192)
        self.mapping = FrozenStageMapping(self.frame.frame_id, self.frame.generation,
            self.frame.capture_sha256, self.frame.topology_sha256, source_mapping["stage_id"],
            source_mapping["stage_epoch"], self.frame.width, self.frame.height,
            source_mapping["payload_bbox_physical"], source_mapping["transport_bbox_physical"],
            (8, 8), source_mapping["white_padding_bbox_physical"], own, (777,), self.clock())
        self.stage = StageReceipt(FrameBinding.from_frame(self.frame), self.mapping.stage_id,
            self.mapping.stage_epoch, self.mapping.payload_bbox_physical,
            self.mapping.transport_bbox_physical, "0"*64, self.clock())
        self.probe = WindowProbeResult("UNIQUE", "synthetic", (), (0, 0, 3200, 2000), 1, 2, 901, 901, {}, ())
        from screenqr.native_identity import NativeWindowIdentity
        candidate = NativeWindowIdentity(901, 700, 123456, r"C:\Program Files\Tencent\Weixin\Weixin.exe",
            "Weixin", "Qt51514QWindowIcon", (0, 0, 3200, 2000), (0, 0, 3200, 2000), 192, True)
        self.probe = replace(self.probe, candidates=(candidate,))
        self.instance = NativeSnapshot.from_probe(self.probe, started_ns=1, finished_ns=2,
            fixed_file_version=(4, 1, 15, 13)).instance
        self.current = True
        self.port = module.WindowsWeChatPort(self.frame, self.mapping, lambda: self.current,
            max_evidence_age_ns=300_000_000, clock=self.clock)
        self.request = self.base["session_id"]
        self.port._request, self.port._bound = self.request, self.instance
        self.port._same_live = MagicMock(return_value=True)
        self.deadline = self.clock.value + 1_500_000_000
        self.ids = []
        self.own_api.kernel.GetCurrentProcessId.return_value = 123
        self.own_api.user.IsWindow.return_value = True
        self.own_api.user.IsWindowVisible.return_value = True
        self.own_api.user.IsIconic.return_value = False
        self.own_api.user.GetAncestor.side_effect = lambda hwnd, flag: hwnd
        def owner_pid(hwnd, pointer):
            pointer._obj.value = 123
            return 1
        self.own_api.user.GetWindowThreadProcessId.side_effect = owner_pid

    def sample(self, mutation=None):
        observation = deepcopy(self.base)
        observation["evidence_id"] = str(uuid4())
        self.ids.append(observation["evidence_id"])
        observation["started_monotonic_ns"] = self.clock()
        payload = observation["payload_comparison"]
        payload["capture_started_monotonic_ns"] = self.clock()
        payload["capture_ended_monotonic_ns"] = self.clock()
        for section in observation["edge_observations"]:
            section["capture_started_monotonic_ns"] = self.clock()
            section["capture_ended_monotonic_ns"] = self.clock()
        observation["capture_completed_monotonic_ns"] = self.clock()
        observation["finished_monotonic_ns"] = self.clock()
        observation["deadline_monotonic_ns"] = self.deadline
        if mutation:
            mutation(observation)
        return observation

    @staticmethod
    def all_white(observation):
        for section in observation["edge_observations"]:
            for row in section["normal_profiles"]:
                row.update(rgb_min=[254]*3, rgb_max=[254]*3, rgb_mean=[254]*3,
                           exact_source_white_pixel_count=0)

    def run_selection(self):
        return self.port.selection_evidence(self.frame, self.stage, self.instance, self.request, self.deadline)

    def absent(self):
        return replace(self.probe, status="NOT_FOUND", candidates=(),
            foreground_hwnd_at_start=777, foreground_hwnd_at_end=777)

    def test_incomplete_paint_waits_for_new_complete_strict_evidence_without_input(self):
        sequence = [self.all_white, None]
        with patch.object(module, "observe_native_selection_pixels", side_effect=lambda *a, **kw: self.sample(sequence.pop(0))) as sample, \
                patch.object(module.time, "sleep", return_value=None) as sleep:
            result = self.run_selection()
        self.assertEqual((sample.call_count, sleep.call_count, self.port.selection_poll_count), (2, 1, 2))
        self.assertEqual(len(set(self.ids)), 2)
        self.assertEqual(result["evidence_id"], self.ids[-1])
        self.assertEqual(self.port._selection_extent.evidence_id, self.ids[-1])
        self.assertEqual(self.port._selection_extent.actual_selection_bbox_physical, self.mapping.transport_bbox_physical)
        self.assertEqual(self.port.last_selection_boundary_reason, "exact_calibrated_profile_two_quarters_agree")
        self.assertEqual(self.input.method_calls, [])

    def test_unbounded_paint_wait_reacquires_fresh_samples_after_slow_wait(self):
        sequence = [self.all_white, None]
        start = self.clock.value
        def acquire(*args, **kwargs):
            self.deadline = kwargs["deadline_monotonic_ns"]
            return self.sample(sequence.pop(0))
        with patch.object(module, "observe_native_selection_pixels", side_effect=acquire) as sample, \
                patch.object(module.time, "sleep", side_effect=lambda seconds: setattr(self.clock, "value", self.clock.value+3_000_000_000)):
            result = self.port.selection_evidence(self.frame, self.stage, self.instance, self.request, None)
        self.assertGreater(self.clock.value-start, 3_000_000_000)
        self.assertEqual(sample.call_count, 2)
        self.assertEqual(result["evidence_id"], self.ids[-1])
        self.assertEqual(self.input.method_calls, [])

    def test_unbounded_native_startup_can_wait_beyond_old_two_second_total(self):
        self.port._bound = None
        start = self.clock.value
        with patch.object(module, "probe_screenshot_window", side_effect=[self.absent(), self.probe]) as probe, \
                patch.object(module.time, "sleep", side_effect=lambda seconds: setattr(self.clock, "value", self.clock.value+3_000_000_000)):
            result = self.port.native_snapshot(self.request, None)
        self.assertGreater(self.clock.value-start, 3_000_000_000)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(result.instance, self.instance)
        self.assertEqual(self.input.method_calls, [])

    def test_settled_native_readiness_ends_unconfirmed_with_no_input(self):
        self.port._bound = None
        with patch.object(module, "probe_screenshot_window", return_value=self.absent()) as probe, \
                patch.object(module.time, "sleep", return_value=None), self.assertRaisesRegex(ValueError, "native_readiness_no_progress_after_complete_samples"):
            self.port.native_snapshot(self.request, None)
        self.assertEqual(probe.call_count, 32)
        self.assertIsNone(self.port._bound)
        self.assertEqual(self.input.method_calls, [])

    def test_later_malformed_strip_cannot_hide_behind_first_paint_error(self):
        def malformed(observation):
            self.all_white(observation)
            observation["edge_observations"][-1]["normal_profiles"].pop()
        with patch.object(module, "observe_native_selection_pixels", side_effect=lambda *a, **kw: self.sample(malformed)) as sample, \
                patch.object(module.time, "sleep") as sleep, self.assertRaises(ValueError):
            self.run_selection()
        self.assertEqual(sample.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(self.port.last_selection_error_code, "selection_wait_sample_incomplete_or_changed")
        self.assertIsNone(self.port._selection_extent)

    def test_derived_wrong_actual_bbox_rejects_immediately_without_wait(self):
        self.configure(outward=4)
        with patch.object(module, "observe_native_selection_pixels", side_effect=lambda *a, **kw: self.sample()) as sample, \
                patch.object(module.time, "sleep") as sleep, self.assertRaises(ValueError):
            self.run_selection()
        self.assertEqual(sample.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(self.port.last_selection_boundary_reason, "actual_selection_bbox_differs_from_requested")
        self.assertIsNone(self.port._selection_extent)

    def test_version_or_binding_error_is_not_a_wait_state(self):
        for mutation in (lambda o: o["version_metadata"].update(fixed_file_version=[4,1,15,14]),
                         lambda o: o.update(stage_epoch=99)):
            with self.subTest(mutation=mutation), \
                    patch.object(module, "observe_native_selection_pixels", side_effect=lambda *a, **kw: self.sample(mutation)) as sample, \
                    patch.object(module.time, "sleep") as sleep, self.assertRaises(ValueError):
                self.run_selection()
            self.assertEqual(sample.call_count, 1)
            sleep.assert_not_called()
        self.assertEqual(self.input.method_calls, [])

    def test_wait_cannot_extend_original_deadline_or_lose_last_boundary_reason(self):
        with patch.object(module, "observe_native_selection_pixels", side_effect=lambda *a, **kw: self.sample(self.all_white)) as sample, \
                patch.object(module.time, "sleep", side_effect=lambda seconds: setattr(self.clock, "value", self.deadline)), \
                self.assertRaises(TimeoutError):
            self.run_selection()
        self.assertEqual(sample.call_count, 1)
        self.assertEqual(self.port.selection_poll_count, 1)
        self.assertEqual(self.port.last_selection_error_code, "selection_evidence_deadline_expired")
        self.assertEqual(self.port.last_selection_boundary_reason, "edge_run_sequence_not_calibrated_exact_three_runs")
        self.assertIsNone(self.port._selection_extent)

    def test_extent_error_after_deadline_stays_timeout_instead_of_generic_rejection(self):
        def expire_during_extent(observation):
            self.clock.value = self.deadline
            raise ValueError('native selection extent unverified: source_observation_not_current')
        with patch.object(module, "observe_native_selection_pixels", side_effect=lambda *a, **kw: self.sample()), \
                patch.object(module, "extent_evidence_from_observation", side_effect=expire_during_extent), \
                self.assertRaises(TimeoutError):
            self.run_selection()
        self.assertEqual(self.port.last_selection_error_code, "selection_evidence_deadline_expired")
        self.assertEqual(self.port.selection_poll_count, 1)
        self.assertIsNone(self.port._selection_extent)
        self.assertEqual(self.input.method_calls, [])

    def test_known_native_to_own_cleanup_transition_waits_but_sends_escape_once(self):
        transition = replace(self.probe, status="REJECTED", candidates=(),
            foreground_hwnd_at_end=777, errors=("foreground_changed_during_query",))
        with patch.object(module, "probe_screenshot_window", side_effect=[self.probe, transition, self.absent()]), \
                patch.object(module.time, "sleep", return_value=None):
            result = self.port.cleanup_once(self.instance, self.deadline)
        self.assertEqual(result, "CONFIRMED_CLOSED")
        self.input.cancel_once.assert_called_once()
        self.assertEqual(self.port.cleanup_probe_count, 3)
        self.assertEqual(self.port.last_cleanup_probe_code, "cleanup_absent_own_foreground")
        self.assertIsNone(self.port.last_cleanup_error_code)
        detached = self.port.last_cleanup_probe
        detached["status"] = "changed"
        self.assertEqual(self.port.last_cleanup_probe["status"], "NOT_FOUND")

    def test_cleanup_foreign_foreground_or_changed_instance_is_unknown(self):
        bad_cases = (
            (replace(self.absent(), foreground_hwnd_at_start=999, foreground_hwnd_at_end=999), "cleanup_foreground_left_allowed_windows"),
            (replace(self.probe, candidates=(replace(self.probe.candidates[0], pid=701),)), "cleanup_bound_instance_changed"),
        )
        for bad, error in bad_cases:
            self.port._cleanup_attempted = False
            self.input.reset_mock()
            with self.subTest(error=error), \
                    patch.object(module, "probe_screenshot_window", side_effect=[self.probe, bad]), \
                    patch.object(module.time, "sleep", return_value=None):
                self.assertEqual(self.port.cleanup_once(self.instance, self.deadline), "UNKNOWN")
            self.input.cancel_once.assert_called_once()
            self.assertEqual(self.port.last_cleanup_error_code, error)

    def test_cleanup_still_native_at_deadline_is_unknown_without_second_escape(self):
        with patch.object(module, "probe_screenshot_window", return_value=self.probe), \
                patch.object(module.time, "sleep", side_effect=lambda seconds: setattr(self.clock, "value", self.deadline)):
            self.assertEqual(self.port.cleanup_once(self.instance, self.deadline), "UNKNOWN")
        self.input.cancel_once.assert_called_once()
        self.assertEqual(self.port.cleanup_probe_count, 1)
        self.assertEqual(self.port.last_cleanup_error_code, "cleanup_deadline_expired")
        self.assertEqual(self.port.last_cleanup_probe_code, "cleanup_bound_native_ready")

    def test_cleanup_already_absent_requires_verified_own_foreground_no_escape(self):
        with patch.object(module, "probe_screenshot_window", return_value=self.absent()):
            self.assertEqual(self.port.cleanup_once(self.instance, self.deadline), "CONFIRMED_ABSENT")
        self.input.cancel_once.assert_not_called()
        self.assertEqual(self.port.cleanup_probe_count, 1)


if __name__ == "__main__":
    unittest.main()
