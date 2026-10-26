"""Synthetic timing/identity checks; these are not real WeChat acceptance."""
from dataclasses import replace
import hashlib
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_controller import Clock
from test_dispatch_controller import DispatchPort
import test_native_job as job_fixtures
from screenqr.controller import HandoffController
from screenqr.handoff import CommitPermit, NATIVE_DISPLAY_PROFILE_A, Policy
from screenqr.native_job import _DeadlinePort, NativeJobResult, describe_job_result, MAX_SETTLED_RESULT_SAMPLES
from screenqr.native_result import NativeResultObserver, NativeResultObservation, ResultWindowEnumeration, ResultWindowIdentity


class UnboundedNativeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.port = DispatchPort(self.clock)
        original_evidence = self.port.selection_evidence
        def acquire(frame, stage, instance, request, deadline):
            actual = self.clock()+2_000_000_000 if deadline is None else deadline
            return original_evidence(frame, stage, instance, request, actual)
        self.port.selection_evidence = acquire
        self.policy = Policy(None, 300_000_000, 200_000_000, 100_000_000,
                             selection_evidence_mode=NATIVE_DISPLAY_PROFILE_A)
        self.controller = HandoffController(self.port, policy=self.policy, cleanup_budget_ns=500, clock=self.clock)
        wait = patch("screenqr.controller.time.sleep", return_value=None)
        wait.start()
        self.addCleanup(wait.stop)

    def test_preparation_and_multiple_short_operations_can_exceed_old_total(self):
        self.port.prepare_request = lambda request: setattr(self.clock, "value", self.clock.value+20_000_000_000)
        for name in ("verify", "trigger", "select"):
            self.port.hooks[name] = lambda: setattr(self.clock, "value", self.clock.value+1_200_000_000)
        result = self.controller.run_and_dispatch(self.port.frame)
        self.assertEqual(result.status, "DISPATCHED_UNCONFIRMED")
        self.assertGreater(result.dispatch_latency_ns, 20_000_000_000)
        self.assertEqual(self.port.calls.count("trigger"), 1)
        self.assertEqual(self.port.calls.count("select"), 1)
        self.assertEqual(len(self.port.commits), 1)

    def test_unbounded_job_still_rejects_old_or_changed_evidence(self):
        self.port.hooks["visual_value"] = lambda data: data.update(finished_ns=self.clock.value-400_000_000)
        result = self.controller.run_and_dispatch(self.port.frame)
        self.assertNotEqual(result.status, "DISPATCHED_UNCONFIRMED")
        self.assertEqual(len(self.port.commits), 0)

    def test_persistent_complete_mismatch_terminates_without_wall_clock_expiry(self):
        self.port.hooks["evidence_value"] = lambda data: data["payload_comparison"].update(captured_rgb_sha256="0"*64)
        result = self.controller.run_and_dispatch(self.port.frame)
        self.assertEqual(result.status, "CONTENT_UNPROVEN")
        self.assertEqual(self.port.calls.count("evidence"), self.policy.max_pending_observations)
        self.assertEqual(self.port.calls.count("trigger"), 1)
        self.assertEqual(self.port.calls.count("select"), 1)
        self.assertEqual(len(self.port.commits), 0)

    def test_stable_absent_toolbar_terminates_without_more_inputs(self):
        self.port.statuses = ["NOT_READY"]*self.policy.max_pending_observations
        result = self.controller.run_and_dispatch(self.port.frame)
        self.assertEqual(result.status, "CONTENT_UNPROVEN")
        self.assertEqual(self.port.calls.count("toolbar"), self.policy.max_pending_observations)
        self.assertEqual(self.port.calls.count("trigger"), 1)
        self.assertEqual(self.port.calls.count("select"), 1)
        self.assertEqual(len(self.port.commits), 0)

    def test_none_job_deadline_does_not_allow_late_single_input_return(self):
        now = [100]
        class Port:
            def commit_once(self, *args):
                now[0] = args[-1]
            def cleanup_once(self, *args):
                return "CONFIRMED_ABSENT"
        port = _DeadlinePort(Port(), None, lambda: now[0])
        with self.assertRaises(TimeoutError):
            port.commit_once(None, None, 200)
        self.assertEqual(port.cleanup_once(None, 300), "CONFIRMED_ABSENT")

    def test_result_no_total_deadline_remains_bound_to_same_baseline_and_dispatch(self):
        with patch.dict(os.environ, {"APPDATA": r"C:\Users\Synthetic\AppData\Roaming"}):
            now = [100]
            data = ResultWindowEnumeration((), 7, 7)
            observer = NativeResultObserver(enumerator=lambda expected: data, clock=lambda: now[0])
            request, frame, evidence = (str(uuid4()) for _ in range(3))
            baseline = observer.capture_baseline(request, frame, evidence, deadline_ns=None).baseline
            permit = CommitPermit(request, frame, str(uuid4()), 110, (0, 0, 2, 2))
            now[0] = 60_000_000_000
            result = observer.observe_new_window(baseline, permit, deadline_ns=None)
            self.assertEqual(result.status, "NOT_READY")
            self.assertIsNotNone(result.progress_sha256)
            self.assertFalse(result.business_success)
            self.assertEqual(observer.observe_new_window(baseline, replace(permit, dispatch_id=str(uuid4())), deadline_ns=None).status, "REJECTED")
            self.assertEqual(observer.observe_new_window(baseline, permit, deadline_ns=90_000_000_000).status, "REJECTED")

    def test_public_failure_wording_never_exposes_diagnostic_codes(self):
        result = NativeJobResult("frame", 1, "stage", 1, "FAILED", "source_pixels_differ", diagnostics=(("last_operation", "verify_stage"),))
        text, warning = describe_job_result(result)
        self.assertTrue(text.startswith("无法识别"))
        self.assertNotIn("source_pixels_differ", text)
        self.assertNotIn("校验", text)
        self.assertTrue(warning)


class UnboundedJobProgressTests(unittest.TestCase):
    def test_result_progress_continues_beyond_settled_sample_threshold(self):
        fixture = job_fixtures.JobTests()
        fixture.setUp()
        job = fixture.make_job()
        observations = [NativeResultObservation("NOT_READY", "synthetic", progress_sha256=hashlib.sha256(str(i).encode()).hexdigest())
                        for i in range(MAX_SETTLED_RESULT_SAMPLES+5)]
        observations.append(NativeResultObservation("NEW_WINDOW_OBSERVED", "synthetic_metadata_only"))
        fixture.port.observe_result.side_effect = observations
        with patch.object(job._observe_stop, "wait", return_value=False):
            job.run()
        self.assertEqual(fixture.port.observe_result.call_count, len(observations))
        self.assertEqual(job.result.observation.status, "NEW_WINDOW_OBSERVED")
        self.assertFalse(job.result.business_success)
        self.assertEqual(sum(name == "commit" for name, _ in fixture.calls), 1)


if __name__ == "__main__":
    unittest.main()
