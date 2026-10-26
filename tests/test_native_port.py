"""Adapter-only fake boundaries; no native API, capture, GUI, input or network.

These checks cover orchestration/provenance and deadlines, not the separately
tested capture algorithm or real Windows/WeChat business acceptance.
"""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from PIL import Image

from screenqr import native_port as port_module
from screenqr.frozen_frame import FrozenFrame, MonitorDescriptor, TopologyDescriptor, TOPOLOGY_KIND
from screenqr.handoff import CommitPermit, NativeSnapshot, SelectionExtentReceipt, StageReceipt
from screenqr.native_identity import NativeWindowIdentity, WindowProbeResult
from screenqr.stage import FrozenStageMapping, OwnStageSnapshot, StagePixelVerification


class Clock:
    def __init__(self):
        self.value = 1_000

    def __call__(self):
        self.value += 1
        return self.value


def fixture():
    topology = TopologyDescriptor(TOPOLOGY_KIND, (0, 0, 3200, 2000),
        (MonitorDescriptor("DISPLAY1", (0, 0, 3200, 2000), 192, 192, True, 0),))
    frame = FrozenFrame(str(uuid4()), 1, (10, 10, 12, 12), (10, 10, 12, 12),
        2, 2, bytes(range(12)), 0.0, 0.001, topology)
    own = OwnStageSnapshot(777, 778, 779, 123, (900, 600, 1100, 800),
        (910, 610, 1090, 790), (1000, 700, 1002, 702), (976, 676, 1026, 726), 192)
    mapping = FrozenStageMapping(frame.frame_id, 1, frame.capture_sha256, frame.topology_sha256,
        str(uuid4()), 3, 2, 2, own.image_bbox_physical, (992, 692, 1010, 710),
        (8, 8), own.padding_bbox_physical, own, (777,), 200)
    digest = hashlib.sha256(b"synthetic transport").hexdigest()
    verification = StagePixelVerification(mapping, frame.capture_sha256, digest, digest,
        digest, digest, str(uuid4()), 0.0, 0.001, 500)
    native = NativeWindowIdentity(901, 700, 123456,
        r"C:\Program Files\Tencent\Weixin\Weixin.exe", "Weixin", "Qt51514QWindowIcon",
        (0, 0, 3200, 2000), (0, 0, 3200, 2000), 192, True)
    probe = WindowProbeResult("UNIQUE", "synthetic", (native,), (0, 0, 3200, 2000),
        1, 2, 901, 901, {}, ())
    return frame, mapping, verification, probe


class NativePortTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.frame, self.mapping, self.verification, self.probe = fixture()
        self.api, self.input = MagicMock(), MagicMock()
        self.api.fixed_version.return_value = (4, 1, 15, 13)
        self.own_api = MagicMock()
        self.own_api.kernel.GetCurrentProcessId.return_value = self.mapping.own_stage.pid
        self.own_api.user.IsWindow.return_value = True
        self.own_api.user.IsWindowVisible.return_value = True
        self.own_api.user.IsIconic.return_value = False
        self.own_api.user.GetAncestor.side_effect = lambda hwnd, flag: hwnd
        def owner_pid(hwnd, pointer):
            pointer._obj.value = self.mapping.own_stage.pid
            return 1
        self.own_api.user.GetWindowThreadProcessId.side_effect = owner_pid
        for target, replacement in (("_Gdi", lambda: self.api),
                                    ("_StageApi", lambda: self.own_api),
                                    ("WeChatInput", lambda **kwargs: self.input),
                                    ("stage_mapping_is_current", lambda *args: True),
                                    ("probe_screenshot_window", lambda: self.probe)):
            patched = patch.object(port_module, target, replacement)
            patched.start()
            self.addCleanup(patched.stop)
        self.port = port_module.WindowsWeChatPort(self.frame, self.mapping, lambda: True,
            max_evidence_age_ns=100_000, clock=self.clock)
        self.stage = StageReceipt.from_stage_verification(self.frame, self.verification)
        self.instance = NativeSnapshot.from_probe(self.probe, started_ns=600, finished_ns=700,
            fixed_file_version=(4, 1, 15, 13)).instance
        self.request = str(uuid4())
        self.port._request, self.port._bound = self.request, self.instance

    def evidence(self):
        return dict(schema_version=1, selection_extent_status="VERIFIED",
            observed_selection_bbox_physical=self.stage.transport_bbox_physical,
            extent_verification_method="native-client-observed-boundaries-v1",
            frame_id=self.frame.frame_id, stage_id=self.stage.stage_id, stage_epoch=self.stage.stage_epoch,
            generation=self.frame.generation, source_capture_sha256=self.frame.capture_sha256,
            topology_sha256=self.frame.topology_sha256, session_id=self.request, evidence_id=str(uuid4()),
            started_monotonic_ns=800, finished_monotonic_ns=900,
            native_identity_before=self.probe.to_dict(), native_identity_after=self.probe.to_dict())

    def extent(self):
        return SelectionExtentReceipt.from_native_extent_evidence(stage=self.stage,
            native_instance=self.instance, evidence=self.evidence())

    def startup_probe(self, *, hwnd=888, found=True):
        if found:
            return replace(self.probe,
                candidates=(replace(self.probe.candidates[0], foreground_match=False),),
                foreground_hwnd_at_start=hwnd, foreground_hwnd_at_end=hwnd)
        return replace(self.probe, status="NOT_FOUND", candidates=(),
            foreground_hwnd_at_start=hwnd, foreground_hwnd_at_end=hwnd)

    def prepare_startup(self):
        self.port._bound = None
        self.port.mapping = replace(self.mapping, dpi_reference_hwnds=(777, 888))

    def test_startup_waits_only_known_own_to_native_transitions_without_input_retry(self):
        self.prepare_startup()
        own = self.startup_probe()
        transition = replace(own, status="REJECTED", foreground_hwnd_at_end=901,
            errors=("foreground_changed_during_query",))
        sequence = [self.startup_probe(found=False), own, transition, self.probe]
        with patch.object(port_module, "probe_screenshot_window", side_effect=sequence) as query, \
                patch.object(port_module.time, "sleep", return_value=None) as sleep:
            result = self.port.native_snapshot(self.request, 10_000)
        self.assertEqual(result.instance, self.instance)
        self.assertEqual(self.port._bound, self.instance)
        self.assertEqual(query.call_count, 4)
        self.assertEqual(sleep.call_count, 3)
        self.assertEqual(self.port.native_startup_transitions,
            {"startup_wait_not_found": 1, "startup_wait_native_foreground": 2})
        self.assertIsNone(self.port.last_native_error_code)
        detached = self.port.last_native_probe
        detached["candidates"][0]["pid"] = 999
        self.assertEqual(self.port.last_native_probe["candidates"][0]["pid"], self.instance.pid)
        self.assertEqual(self.input.method_calls, [])

    def test_startup_rejects_other_application_foreground_immediately(self):
        self.prepare_startup()
        bad = self.startup_probe(hwnd=9999)
        with patch.object(port_module, "probe_screenshot_window", side_effect=[bad, self.probe]) as query, \
                patch.object(port_module.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "native_startup_foreground_left_allowed_windows"):
                self.port.native_snapshot(self.request, 10_000)
        self.assertEqual(query.call_count, 1)
        sleep.assert_not_called()
        self.assertIsNone(self.port._bound)
        self.assertEqual(self.port.last_native_probe, bad.to_dict())
        self.assertEqual(self.input.method_calls, [])

    def test_first_complete_startup_candidate_cannot_change_or_disappear(self):
        for replacement in (replace(self.probe, candidates=(replace(self.probe.candidates[0], pid=701),)),
                            self.startup_probe(found=False)):
            with self.subTest(status=replacement.status):
                self.prepare_startup()
                with patch.object(port_module, "probe_screenshot_window",
                        side_effect=[self.startup_probe(), replacement, self.probe]) as query, \
                        patch.object(port_module.time, "sleep", return_value=None):
                    with self.assertRaises(ValueError):
                        self.port.native_snapshot(self.request, 10_000)
                self.assertEqual(query.call_count, 2)
                self.assertIn(self.port.last_native_error_code,
                    ("native_startup_candidate_changed", "native_startup_candidate_disappeared"))
                self.assertIsNone(self.port._bound)
                self.assertEqual(self.input.method_calls, [])

    def test_startup_profile_version_ambiguity_or_query_error_never_waits(self):
        rows = [
            (replace(self.probe, candidates=(replace(self.probe.candidates[0], dpi=144),)), (4,1,15,13)),
            (self.probe, (4,1,15,12)),
            (self.startup_probe(), (4,1,15,12)),
            (replace(self.probe, status="AMBIGUOUS", candidates=self.probe.candidates*2), (4,1,15,13)),
            (replace(self.probe, status="REJECTED", errors=("enumeration_callback_failed",)), (4,1,15,13)),
        ]
        for bad, version in rows:
            with self.subTest(status=bad.status, version=version):
                self.prepare_startup()
                self.api.fixed_version.return_value = version
                with patch.object(port_module, "probe_screenshot_window", side_effect=[bad, self.probe]) as query, \
                        patch.object(port_module.time, "sleep") as sleep:
                    with self.assertRaises((ValueError, port_module._ObserverError)):
                        self.port.native_snapshot(self.request, 10_000)
                self.assertEqual(query.call_count, 1)
                sleep.assert_not_called()
                self.assertIsNone(self.port._bound)
                self.assertEqual(self.input.method_calls, [])

    def test_startup_timeout_preserves_last_actual_metadata_without_retrigger(self):
        self.prepare_startup()
        own = self.startup_probe()
        def expire(duration):
            self.clock.value = 10_000
        with patch.object(port_module, "probe_screenshot_window", return_value=own) as query, \
                patch.object(port_module.time, "sleep", side_effect=expire):
            with self.assertRaises(TimeoutError):
                self.port.native_snapshot(self.request, 10_000)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(self.port.last_native_error_code, "native_readiness_deadline_expired")
        self.assertEqual(self.port.last_native_probe, own.to_dict())
        self.assertIsNone(self.port._bound)
        self.assertEqual(self.input.method_calls, [])

    def test_ready_snapshot_or_final_current_late_return_cannot_bind(self):
        for boundary in ("version", "final_current"):
            with self.subTest(boundary=boundary):
                self.prepare_startup()
                self.clock.value = 1_000
                def version(path):
                    if boundary == "version":
                        self.clock.value = 10_000
                    return (4,1,15,13)
                currents = [0]
                def current():
                    currents[0] += 1
                    if boundary == "final_current" and currents[0] == 2:
                        self.clock.value = 10_000
                    return True
                self.api.fixed_version.side_effect = version
                with patch.object(self.port, "_current", current), \
                        patch.object(port_module.time, "sleep") as sleep:
                    with self.assertRaises(TimeoutError):
                        self.port.native_snapshot(self.request, 10_000)
                sleep.assert_not_called()
                self.assertIsNone(self.port._bound)
                self.assertEqual(self.port.last_native_error_code, "native_readiness_deadline_expired")

    def test_declared_own_foreground_requires_actual_current_pid_and_root(self):
        for invalid in ("pid", "root"):
            with self.subTest(invalid=invalid):
                self.prepare_startup()
                def owner_pid(hwnd, pointer):
                    pointer._obj.value = 999 if invalid == "pid" and hwnd == 888 else self.mapping.own_stage.pid
                    return 1
                self.own_api.user.GetWindowThreadProcessId.side_effect = owner_pid
                self.own_api.user.GetAncestor.side_effect = lambda hwnd, flag: 777 if invalid == "root" and hwnd == 888 else hwnd
                with patch.object(port_module, "probe_screenshot_window", return_value=self.startup_probe()) as query, \
                        patch.object(port_module.time, "sleep") as sleep:
                    with self.assertRaisesRegex(ValueError, "native_startup_foreground_left_allowed_windows"):
                        self.port.native_snapshot(self.request, 10_000)
                self.assertEqual(query.call_count, 1)
                sleep.assert_not_called()
                self.assertIsNone(self.port._bound)

    def test_bound_snapshot_losing_native_foreground_never_uses_startup_wait(self):
        with patch.object(port_module, "probe_screenshot_window", return_value=self.startup_probe()) as query, \
                patch.object(port_module.time, "sleep") as sleep:
            with self.assertRaises(port_module._ObserverError):
                self.port.native_snapshot(self.request, 10_000)
        self.assertEqual(query.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(self.port.native_startup_transitions, {})
        self.assertEqual(self.port.last_native_error_code, "verified_native_window_not_stably_foreground")

    def test_constructor_and_stage_receipt_bind_all_source_fields(self):
        for field, value in (("frame_id", str(uuid4())), ("generation", 2),
                ("source_capture_sha256", "a"*64), ("topology_sha256", "b"*64)):
            with self.subTest(mapping_field=field), self.assertRaises(ValueError):
                port_module.WindowsWeChatPort(self.frame, replace(self.mapping, **{field: value}),
                    lambda: True, max_evidence_age_ns=10, clock=self.clock)
        for field, value in (("frame_id", str(uuid4())), ("frame_sha256", "a"*64),
                ("generation", 2), ("topology_sha256", "b"*64), ("width", 3), ("height", 3)):
            with self.subTest(source_field=field):
                self.assertFalse(self.port._stage_matches(replace(self.stage,
                    source=replace(self.stage.source, **{field: value}))))
        for field, value in (("stage_id", str(uuid4())), ("stage_epoch", 4),
                ("payload_bbox_physical", (1000, 700, 1003, 702)),
                ("transport_bbox_physical", (990, 692, 1010, 710))):
            with self.subTest(stage_field=field):
                self.assertFalse(self.port._stage_matches(replace(self.stage, **{field: value})))
        self.assertTrue(self.port._stage_matches(self.stage))

    def test_keyword_only_capture_and_metadata_retained_on_conversion_rejection(self):
        observation = {"status": "PAYLOAD_MISMATCH", "edge_observations": [{"side": "top"}]}
        calls = []
        def capture(frame, *, stage_mapping, session_id, generation, deadline_monotonic_ns):
            calls.append((frame, stage_mapping, session_id, generation, deadline_monotonic_ns))
            return observation
        with patch.object(self.port, "_same_live", return_value=True), \
                patch.object(port_module, "observe_native_selection_pixels", capture), \
                patch.object(port_module, "extent_evidence_from_observation", side_effect=ValueError("rejected")):
            with self.assertRaises(ValueError):
                self.port.selection_evidence(self.frame, self.stage, self.instance, self.request, 10_000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], self.mapping.metadata_only())
        self.assertEqual(self.port.last_selection_error_code, "selection_extent_conversion_rejected")
        self.assertEqual(self.port.last_selection_observation, observation)
        observation["edge_observations"][0]["side"] = "mutated"
        detached = self.port.last_selection_observation
        self.assertEqual(detached["edge_observations"][0]["side"], "top")
        detached["edge_observations"].clear()
        self.assertEqual(len(self.port.last_selection_observation["edge_observations"]), 1)
        self.assertIsNone(self.port._selection_extent)
        self.input.assert_not_called()

    def test_selection_checks_deadline_after_slow_final_current(self):
        evidence = self.evidence()
        def current():
            self.clock.value = 10_000
            return True
        with patch.object(self.port, "_same_live", return_value=True), \
                patch.object(self.port, "_current", current), \
                patch.object(port_module, "observe_native_selection_pixels", return_value=evidence), \
                patch.object(port_module, "extent_evidence_from_observation", side_effect=lambda value: value):
            with self.assertRaises(TimeoutError):
                self.port.selection_evidence(self.frame, self.stage, self.instance, self.request, 10_000)
        self.assertIsNone(self.port._selection_extent)
        self.assertEqual(self.port.last_selection_error_code, "selection_evidence_deadline_expired")

    def test_snapshot_finished_brackets_version_read(self):
        def version(path):
            self.clock.value = 2_000
            return (4, 1, 15, 13)
        self.api.fixed_version.side_effect = version
        result = self.port._snapshot_once(10_000)
        self.assertGreater(result.finished_ns, 2_000)
        self.assertLess(result.started_ns, 2_000)

    def test_stage_failure_records_fixed_code_and_never_passes_or_retries(self):
        failure = port_module.StageValidationError("Explicit white stage padding is not entirely white")
        with patch.object(port_module, "verify_frozen_stage", side_effect=failure) as verify:
            with self.assertRaises(port_module.StageValidationError) as raised:
                self.port.verify_stage(self.frame, 10_000)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.port.last_stage_error_code, "white_padding_not_white")
        verify.assert_called_once_with(self.frame, self.mapping, deadline_monotonic_ns=10_000)
        self.assertEqual(self.input.method_calls, [])
        self.assertEqual(self.port.timings, [])

    def test_toolbar_rejects_unproved_equal_copy_before_sampling(self):
        extent = self.extent()
        self.port._selection_extent = extent
        unproved = replace(extent, _observed_boundary_proof=None)
        self.assertEqual(unproved, extent)  # Dataclass equality excludes the marker.
        with patch.object(self.port, "_same_live") as live, \
                patch.object(port_module, "_capture_rect") as capture:
            for bad in (unproved, object(), replace(extent, evidence_id=str(uuid4()))):
                with self.subTest(value=type(bad).__name__), self.assertRaises(ValueError):
                    self.port.observe_toolbar(bad, observation_id=1, deadline_ns=10_000)
            live.assert_not_called()
            capture.assert_not_called()

    def test_toolbar_checks_deadline_after_slow_final_current(self):
        from screenqr import native_toolbar
        extent = self.extent()
        self.port._selection_extent = extent
        @contextmanager
        def capture(api, identity, bbox, approved):
            image = Image.new("RGB", (bbox[2]-bbox[0], bbox[3]-bbox[1]))
            try:
                yield image
            finally:
                image.close()
        def current():
            self.clock.value = 10_000
            return True
        with patch.object(self.port, "_same_live", return_value=True), \
                patch.object(self.port, "_current", current), \
                patch.object(port_module, "_capture_rect", capture), \
                patch.object(native_toolbar, "detect_toolbar_icon", return_value=object()), \
                patch.object(port_module.ObservationReceipt, "from_native_observation", return_value=object()):
            with self.assertRaises(TimeoutError):
                self.port.observe_toolbar(replace(extent), observation_id=1, deadline_ns=10_000)

    def test_stale_toolbar_diagnostic_survives_receipt_refusal_without_pixels(self):
        from screenqr import native_toolbar
        extent = self.extent()
        self.port._selection_extent = extent
        self.port._age = 300_000_000
        @contextmanager
        def capture(api, identity, bbox, approved):
            with Image.new("RGB", (bbox[2]-bbox[0], bbox[3]-bbox[1])) as image:
                yield image
        detection = SimpleNamespace(status="STALE", reason="extent_not_fresh",
            toolbar_bbox_physical=(100, 720, 1700, 816), candidate_bbox_physical=None,
            candidate_count=1, finished_ns=extent.measured_ns+300_000_001,
            to_dict=lambda: dict(status="STALE", read_only=True, errors=[]))
        with patch.object(self.port, "_same_live", return_value=True), \
                patch.object(port_module, "_capture_rect", capture), \
                patch.object(native_toolbar, "detect_toolbar_icon", return_value=detection) as detect, \
                self.assertRaises(ValueError):
            self.port.observe_toolbar(extent, observation_id=1, deadline_ns=10_000)
        self.assertEqual(detect.call_args.kwargs["binding"].max_evidence_age_ns, 300_000_000)
        diagnostic = self.port.last_toolbar_diagnostic
        self.assertEqual(diagnostic["status"], "STALE")
        self.assertEqual(diagnostic["reason"], "extent_not_fresh")
        self.assertEqual(diagnostic["extent_age_ns"], 300_000_001)
        self.assertEqual(diagnostic["candidate_count"], 1)
        self.assertTrue(diagnostic["diagnostic_not_authorization"])
        diagnostic["reason"] = "changed copy"
        self.assertEqual(self.port.last_toolbar_reason_code, "extent_not_fresh")
        self.assertEqual(self.input.method_calls, [])

    def test_cleanup_late_absence_is_unknown_and_retires_port(self):
        absent = WindowProbeResult("NOT_FOUND", "synthetic", (), (0, 0, 3200, 2000),
            1, 2, 0, 0, {}, ())
        def late_probe():
            self.clock.value = 10_000
            return absent
        with patch.object(port_module, "probe_screenshot_window", late_probe):
            self.assertEqual(self.port.cleanup_once(self.instance, 10_000), "UNKNOWN")
        self.assertTrue(self.port._cleanup_attempted)
        self.assertIsNone(self.port._selection_extent)
        self.input.cancel_once.assert_not_called()

    def test_cleanup_after_normal_commit_waits_readonly_then_accepts_stable_absence(self):
        permit=CommitPermit(self.request,self.frame.frame_id,str(uuid4()),900,(10,10,20,20))
        self.port.commit_once(self.instance,permit,10_000)
        self.assertTrue(self.port.commit_metadata['commit_returned_normally'])
        absent=replace(self.probe,status='NOT_FOUND',candidates=(),
            foreground_hwnd_at_start=9999,foreground_hwnd_at_end=9999)
        with patch.object(port_module,'probe_screenshot_window',side_effect=[self.probe,absent]) as probe, \
                patch.object(port_module.time,'sleep',return_value=None):
            self.assertEqual(self.port.cleanup_once(self.instance,10_000),'CONFIRMED_ABSENT')
        self.assertEqual(probe.call_count,2)
        self.assertEqual(self.port.last_cleanup_probe_code,'cleanup_absent_postcommit_readonly')
        self.input.click_once.assert_called_once()
        self.input.cancel_once.assert_not_called()
        self.assertTrue(self.port._cleanup_attempted)

    def test_cleanup_after_normal_commit_bound_window_timeout_never_sends_escape(self):
        self.port._commit_called=self.port._commit_returned_normally=True
        def expire(_duration):
            self.clock.value=10_000
        with patch.object(port_module,'probe_screenshot_window',return_value=self.probe) as probe, \
                patch.object(port_module.time,'sleep',side_effect=expire):
            self.assertEqual(self.port.cleanup_once(self.instance,10_000),'UNKNOWN')
        self.assertEqual(probe.call_count,1)
        self.assertEqual(self.port.last_cleanup_error_code,'cleanup_deadline_expired')
        self.assertEqual(self.input.method_calls,[])

    def test_cleanup_postcommit_changed_ambiguous_unstable_or_wrong_profile_is_unknown(self):
        absent=replace(self.probe,status='NOT_FOUND',candidates=(),
            foreground_hwnd_at_start=9999,foreground_hwnd_at_end=9999)
        cases=(
            replace(self.probe,candidates=(replace(self.probe.candidates[0],pid=701),)),
            replace(self.probe,status='AMBIGUOUS',candidates=self.probe.candidates*2),
            replace(absent,errors=('foreground_changed_during_query',)),
            replace(absent,foreground_hwnd_at_start=901,foreground_hwnd_at_end=901),
            replace(absent,foreground_hwnd_at_start=0,foreground_hwnd_at_end=0),
            replace(absent,foreground_hwnd_at_end=9998),
            replace(absent,thread_dpi_awareness=0),
            replace(absent,monitor_count=2),
        )
        for bad in cases:
            with self.subTest(status=bad.status,foreground=bad.foreground_hwnd_at_start):
                port=port_module.WindowsWeChatPort(self.frame,self.mapping,lambda:True,
                    max_evidence_age_ns=100_000,clock=self.clock)
                port._request,port._bound=self.request,self.instance
                port._commit_called=port._commit_returned_normally=True
                with patch.object(port_module,'probe_screenshot_window',return_value=bad) as probe, \
                        patch.object(port_module.time,'sleep') as sleep:
                    self.assertEqual(port.cleanup_once(self.instance,10_000),'UNKNOWN')
                self.assertEqual(probe.call_count,1)
                sleep.assert_not_called()
                self.assertEqual(self.input.method_calls,[])

    def test_late_absence_requires_two_valid_observations_and_never_inputs(self):
        self.port._cleanup_attempted = True
        self.port._commit_returned_normally = True
        absent = replace(self.probe, status='NOT_FOUND', candidates=(),
            foreground_hwnd_at_start=9999, foreground_hwnd_at_end=9999)
        with patch.object(port_module, 'probe_screenshot_window', return_value=absent) as probe:
            self.assertEqual(self.port.confirm_cleanup_absence(), 'CONFIRMED_ABSENT')
            self.assertEqual(self.port.confirm_cleanup_absence(), 'UNKNOWN')
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(self.input.method_calls, [])

    def test_late_absence_rejects_window_remaining_or_unstable_query(self):
        self.port._cleanup_attempted = True
        self.port._commit_returned_normally = True
        absent = replace(self.probe, status='NOT_FOUND', candidates=(),
            foreground_hwnd_at_start=9999, foreground_hwnd_at_end=9999)
        with patch.object(port_module, 'probe_screenshot_window', side_effect=[absent, self.probe]):
            self.assertEqual(self.port.confirm_cleanup_absence(), 'UNKNOWN')
        self.assertEqual(self.input.method_calls, [])

    def test_late_absence_without_normal_commit_does_not_probe(self):
        self.port._cleanup_attempted = True
        with patch.object(port_module, 'probe_screenshot_window') as probe:
            self.assertEqual(self.port.confirm_cleanup_absence(), 'UNKNOWN')
        probe.assert_not_called()

    def test_late_absence_waits_known_foreground_transition_then_two_absences(self):
        self.port._cleanup_attempted = self.port._commit_returned_normally = True
        absent = replace(self.probe, status='NOT_FOUND', candidates=(),
            foreground_hwnd_at_start=9999, foreground_hwnd_at_end=9999)
        transition = replace(self.probe, status='REJECTED',
            foreground_hwnd_at_end=9999, errors=('foreground_changed_during_query',))
        with patch.object(port_module, 'probe_screenshot_window',
                side_effect=[transition, self.probe, absent, absent]) as probe, \
                patch.object(port_module.time, 'sleep', return_value=None):
            self.assertEqual(self.port.confirm_cleanup_absence(), 'CONFIRMED_ABSENT')
        self.assertEqual(probe.call_count, 4)
        self.assertEqual(self.port.cleanup_probe_count, 4)
        self.assertIsNone(self.port.last_cleanup_error_code)
        self.assertEqual(self.input.method_calls, [])

    def test_late_absence_transition_breaks_consecutive_absence(self):
        self.port._cleanup_attempted = self.port._commit_returned_normally = True
        absent = replace(self.probe, status='NOT_FOUND', candidates=(),
            foreground_hwnd_at_start=9999, foreground_hwnd_at_end=9999)
        transition = replace(absent, status='REJECTED', foreground_hwnd_at_start=9998,
            errors=('foreground_changed_during_query',))
        with patch.object(port_module, 'probe_screenshot_window',
                side_effect=[absent, transition, absent, absent]) as probe, \
                patch.object(port_module.time, 'sleep', return_value=None):
            self.assertEqual(self.port.confirm_cleanup_absence(), 'CONFIRMED_ABSENT')
        self.assertEqual(probe.call_count, 4)
        self.assertEqual(self.input.method_calls, [])

    def test_late_absence_rejects_actual_ambiguity_or_different_identity_without_retry(self):
        transition = replace(self.probe, status='REJECTED', foreground_hwnd_at_end=9999,
            errors=('foreground_changed_during_query',))
        bad_probes = (
            replace(transition, candidates=self.probe.candidates*2),
            replace(transition, candidates=(replace(self.probe.candidates[0], pid=701),)),
            replace(transition, errors=('eligible_window_identity_changed_during_query',)),
            replace(transition, foreground_hwnd_at_end=0),
            replace(self.probe, status='AMBIGUOUS', candidates=self.probe.candidates*2),
        )
        for bad in bad_probes:
            with self.subTest(status=bad.status, errors=bad.errors):
                self.port._absence_rechecked = False
                self.port._cleanup_attempted = self.port._commit_returned_normally = True
                with patch.object(port_module, 'probe_screenshot_window', return_value=bad) as probe, \
                        patch.object(port_module.time, 'sleep') as sleep:
                    self.assertEqual(self.port.confirm_cleanup_absence(), 'UNKNOWN')
                self.assertEqual(probe.call_count, 1)
                sleep.assert_not_called()
        self.assertEqual(self.input.method_calls, [])

    def test_late_absence_sampling_is_finite_with_bound_window_and_no_clock_progress(self):
        self.port._cleanup_attempted = self.port._commit_returned_normally = True
        with patch.object(port_module, 'probe_screenshot_window', return_value=self.probe) as probe, \
                patch.object(port_module.time, 'sleep', return_value=None):
            self.assertEqual(self.port.confirm_cleanup_absence(), 'UNKNOWN')
        self.assertEqual(probe.call_count, 8)
        self.assertEqual(self.port.last_cleanup_error_code, 'cleanup_postcommit_absence_not_stable')
        self.assertEqual(self.input.method_calls, [])

    def test_cleanup_without_normal_commit_keeps_single_escape_and_original_foreground_scope(self):
        absent=replace(self.probe,status='NOT_FOUND',candidates=(),
            foreground_hwnd_at_start=777,foreground_hwnd_at_end=777)
        for commit_called in (False,True):
            with self.subTest(commit_called=commit_called):
                port=port_module.WindowsWeChatPort(self.frame,self.mapping,lambda:True,
                    max_evidence_age_ns=100_000,clock=self.clock)
                port._request,port._bound=self.request,self.instance
                port._commit_called=commit_called  # Failed/unreached input is not normal commit.
                self.input.reset_mock()
                with patch.object(port_module,'probe_screenshot_window',side_effect=[self.probe,absent]), \
                        patch.object(port_module.time,'sleep',return_value=None):
                    self.assertEqual(port.cleanup_once(self.instance,10_000),'CONFIRMED_CLOSED')
                self.input.cancel_once.assert_called_once()
                port=port_module.WindowsWeChatPort(self.frame,self.mapping,lambda:True,
                    max_evidence_age_ns=100_000,clock=self.clock)
                port._request,port._bound=self.request,self.instance
                self.input.reset_mock()
                other=replace(absent,foreground_hwnd_at_start=9999,foreground_hwnd_at_end=9999)
                with patch.object(port_module,'probe_screenshot_window',return_value=other):
                    self.assertEqual(port.cleanup_once(self.instance,10_000),'UNKNOWN')
                self.input.cancel_once.assert_not_called()

    def test_retired_adapter_never_accepts_more_dispatch_or_snapshot(self):
        self.port._cleanup_attempted = True
        permit = CommitPermit(self.request, self.frame.frame_id, str(uuid4()), 1_000, (10, 10, 20, 20))
        with patch.object(port_module, "probe_screenshot_window") as probe:
            for operation in (
                lambda: self.port.trigger_once(self.request, str(uuid4()), 10_000),
                lambda: self.port.select_once(self.instance, self.mapping.transport_bbox_physical, self.request, 10_000),
                lambda: self.port.native_snapshot(self.request, 10_000),
                lambda: self.port.commit_once(self.instance, permit, 10_000),
                lambda: self.port.absence_receipt(str(uuid4()), 10_000),
            ):
                with self.assertRaises(ValueError):
                    operation()
            probe.assert_not_called()
        self.assertEqual(self.input.method_calls, [])


if __name__ == "__main__":
    unittest.main()
