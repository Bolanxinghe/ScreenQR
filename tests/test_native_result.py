"""Pure injected-metadata tests; no Windows run or business acceptance claim."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screenqr.handoff import CommitPermit
from screenqr.native_result import (NativeResultObserver, ResultWindowEnumeration,
                                   ResultWindowIdentity, host_executable_path)


class Clock:
    def __init__(self):
        self.value = 100

    def __call__(self):
        self.value += 1
        return self.value


class NativeResultTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"APPDATA": r"C:\Users\Synthetic\AppData\Roaming"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.clock = Clock()
        self.old = self.window(10)
        self.data = ResultWindowEnumeration((self.old,), 10, 10)
        self.enumeration_calls = 0

        def enumerate_windows(expected):
            self.assertEqual(expected, host_executable_path())
            self.enumeration_calls += 1
            return self.data

        self.observer = NativeResultObserver(enumerator=enumerate_windows, clock=self.clock)
        self.request_id, self.frame_id, self.evidence_id = (str(uuid4()) for _ in range(3))
        captured = self.observer.capture_baseline(self.request_id, self.frame_id, self.evidence_id, deadline_ns=1_000)
        self.assertEqual(captured.status, "NOT_READY")
        self.baseline = captured.baseline
        self.clock.value = 110
        self.permit = CommitPermit(self.request_id, self.frame_id, str(uuid4()), 110, (0, 0, 10, 10))

    def window(self, hwnd, *, created=400, pid=200, visible=True, title="Public fixture"):
        return ResultWindowIdentity(hwnd, pid, created, host_executable_path(), "FixtureClass",
            (10, 20, 110, 120), visible, hashlib.sha256(title.encode()).hexdigest())

    def observe(self, permit=None, **kwargs):
        return self.observer.observe_new_window(self.baseline, permit or self.permit, deadline_ns=1_000, **kwargs)

    def test_old_host_navigation_and_geometry_do_not_become_new_instance(self):
        self.data = ResultWindowEnumeration((replace(self.old, title_sha256="a"*64,
            window_rect=(20, 30, 200, 300)),), 10, 10)
        result = self.observe()
        self.assertEqual(result.status, "NOT_READY")
        self.assertFalse(result.reused_host_tab_supported)
        self.assertFalse(result.business_success)

    def test_read_only_budget_starts_at_consumed_commit_and_cannot_renew(self):
        calls=[]
        observer=NativeResultObserver(enumerator=lambda path:calls.append(path) or self.data,
            clock=self.clock,max_observation_ns=100)
        baseline=observer.capture_baseline(self.request_id,self.frame_id,str(uuid4()),deadline_ns=None).baseline
        self.clock.value=120
        permit=replace(self.permit,consumed_ns=120)
        self.assertEqual(observer.observe_new_window(baseline,permit,deadline_ns=None).status,'NOT_READY')
        before=len(calls); self.clock.value=220
        self.assertEqual(observer.observe_new_window(baseline,permit,deadline_ns=None).reason,'result_deadline_elapsed')
        self.assertEqual(len(calls),before)
        self.assertIsNone(baseline.deadline_ns)
        self.assertEqual(observer.observe_new_window(baseline,permit,deadline_ns=None).status,'STALE')

    def test_read_only_budget_cannot_exceed_ten_seconds_or_accept_boolean(self):
        for value in (True,0,-1,10_000_000_001):
            with self.subTest(value=value),self.assertRaises(ValueError):
                NativeResultObserver(max_observation_ns=value)

    def test_unique_new_foreground_window_is_metadata_only_and_counted_once(self):
        new = self.window(11)
        self.data = ResultWindowEnumeration((self.old, new), 11, 11)
        result = self.observe(expected_title_sha256=new.title_sha256)
        self.assertEqual(result.status, "NEW_WINDOW_OBSERVED")
        self.assertEqual(result.candidate, new)
        self.assertEqual(result.dispatch_id, self.permit.dispatch_id)
        self.assertEqual(result.elapsed_since_consumed_ns, 2)
        self.assertFalse(result.business_success)
        self.assertFalse(hasattr(result.candidate, "title"))
        self.assertEqual(self.observe().status, "REJECTED")

    def test_multiple_new_visible_windows_are_ambiguous_even_with_one_foreground(self):
        self.data = ResultWindowEnumeration((self.old, self.window(11), self.window(12)), 11, 11)
        self.assertEqual(self.observe().status, "AMBIGUOUS")

    def test_new_nonforeground_window_stays_not_ready(self):
        self.data = ResultWindowEnumeration((self.old, self.window(11)), 10, 10)
        self.assertEqual(self.observe().status, "NOT_READY")

    def test_foreground_change_within_query_is_stale(self):
        self.data = ResultWindowEnumeration((self.old, self.window(11)), 11, 10)
        self.assertEqual(self.observe().status, "STALE")

    def test_cross_request_frame_and_prebaseline_commit_are_rejected_before_query(self):
        for permit in (replace(self.permit, request_id=str(uuid4())),
                       replace(self.permit, frame_id=str(uuid4())),
                       replace(self.permit, consumed_ns=self.baseline.finished_ns)):
            with self.subTest(permit=permit):
                self.assertEqual(self.observe(permit).status, "REJECTED")
        self.assertEqual(self.enumeration_calls, 1)

    def test_polling_cannot_rebind_dispatch_or_extend_deadline(self):
        self.assertEqual(self.observe().status, "NOT_READY")
        self.assertEqual(self.observe(replace(self.permit, dispatch_id=str(uuid4()))).status, "REJECTED")
        result = self.observer.observe_new_window(self.baseline, self.permit, deadline_ns=2_000)
        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(self.enumeration_calls, 2)

    def test_expiry_before_acquisition_is_stale(self):
        self.clock.value = 999
        self.assertEqual(self.observe().status, "STALE")
        self.assertEqual(self.enumeration_calls, 1)

    def test_expiry_during_acquisition_is_stale(self):
        self.clock.value = 998
        self.assertEqual(self.observe().status, "STALE")
        self.assertEqual(self.enumeration_calls, 2)

    def test_reused_handle_requires_a_distinct_process_lifetime(self):
        self.data = ResultWindowEnumeration((self.window(10, pid=201, created=500),), 10, 10)
        self.assertEqual(self.observe().status, "NEW_WINDOW_OBSERVED")

    def test_hidden_baseline_window_becoming_visible_is_not_new(self):
        self.data = ResultWindowEnumeration((replace(self.old, visible=False),), 10, 10)
        captured = self.observer.capture_baseline(self.request_id, self.frame_id, str(uuid4()), deadline_ns=1_000)
        self.baseline = captured.baseline
        self.clock.value = 120
        self.permit = replace(self.permit, consumed_ns=120)
        self.data = ResultWindowEnumeration((self.old,), 10, 10)
        self.assertEqual(self.observe().status, "NOT_READY")

    def test_reconstructed_baseline_is_not_accepted(self):
        altered = replace(self.baseline, windows=())
        result = self.observer.observe_new_window(altered, self.permit, deadline_ns=1_000)
        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(self.enumeration_calls, 1)

    def test_incomplete_metadata_or_wrong_path_is_rejected(self):
        for data in (ResultWindowEnumeration((self.old,), 10, 10, ("query_failed",)),
                     ResultWindowEnumeration((replace(self.old, executable_path=r"C:\Other\WeChatAppEx.exe"),), 10, 10),
                     ResultWindowEnumeration((self.old, self.old), 10, 10)):
            with self.subTest(data=data):
                self.data = data
                self.assertEqual(self.observe().status, "REJECTED")

    def test_clock_rollback_and_future_commit_are_not_fresh_observations(self):
        self.assertEqual(self.observe(replace(self.permit, consumed_ns=900)).status, "STALE")
        self.clock.value = 1
        self.assertEqual(self.observe(replace(self.permit, consumed_ns=900)).status, "REJECTED")

    def test_fixture_title_mismatch_is_not_a_result(self):
        self.data = ResultWindowEnumeration((self.old, self.window(11)), 11, 11)
        self.assertEqual(self.observe(expected_title_sha256="a"*64).status, "REJECTED")


if __name__ == "__main__":
    unittest.main()
