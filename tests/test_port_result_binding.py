"""Pure result-wiring boundaries: no real Win32, GUI, input, capture or network.

Injected metadata/input calls do not count as real dispatch or business tests.
The shared fixture is data-only; its old test suite is not executed here.
"""
from dataclasses import replace
import hashlib
import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from test_native_port import Clock, fixture
from screenqr import native_port as port_module
from screenqr.handoff import CommitPermit, NativeSnapshot
from screenqr.native_input import InputOutcomeUnknown
from screenqr.native_result import (NativeResultObserver, ResultWindowEnumeration,
                                   ResultWindowIdentity, host_executable_path)


class PortResultBindingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.frame, self.mapping, _, probe = fixture()
        self.probe = replace(probe, status="NOT_FOUND", candidates=(),
            foreground_hwnd_at_start=777, foreground_hwnd_at_end=777)
        self.instance = NativeSnapshot.from_probe(probe, started_ns=600, finished_ns=700,
            fixed_file_version=(4, 1, 15, 13)).instance
        self.input = MagicMock()
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
        self.current = True
        self.data = ResultWindowEnumeration((), 777, 777)
        self.enumeration_calls = []
        self.enumeration_hook = None
        environment = patch.dict(os.environ, {"APPDATA": r"C:\Users\Synthetic\AppData\Roaming"})
        environment.start()
        self.addCleanup(environment.stop)
        for target, replacement in (("_Gdi", MagicMock),
                ("_StageApi", lambda: self.own_api),
                ("WeChatInput", lambda **kwargs: self.input),
                ("stage_mapping_is_current", lambda *args: True),
                ("probe_screenshot_window", lambda: self.probe)):
            patched = patch.object(port_module, target, replacement)
            patched.start()
            self.addCleanup(patched.stop)

        def enumerate_windows(expected):
            self.enumeration_calls.append(expected)
            if self.enumeration_hook:
                self.enumeration_hook()
            return self.data

        self.observer = NativeResultObserver(enumerator=enumerate_windows, clock=self.clock)
        self.port = self.new_port(result_observer=self.observer, result_deadline_ns=100_000)
        self.request_id = str(uuid4())
        self.input_deadline = 10_000

    def new_port(self, **kwargs):
        return port_module.WindowsWeChatPort(self.frame, self.mapping, lambda: self.current,
            max_evidence_age_ns=100_000, clock=self.clock, **kwargs)

    def baseline(self):
        return self.port.absence_receipt(self.request_id, self.input_deadline)

    def permit(self, **changes):
        permit = CommitPermit(self.request_id, self.frame.frame_id, str(uuid4()),
            self.clock(), (100, 100, 120, 120))
        return replace(permit, **changes)

    def new_window(self):
        return ResultWindowIdentity(301, 302, 303, host_executable_path(), "PublicFixture",
            (100, 100, 600, 600), True, hashlib.sha256(b"public fixture").hexdigest())

    def test_result_options_are_paired_and_default_remains_without_observation(self):
        for options in ({"result_deadline_ns": 100_000},
                        {"result_observer": self.observer, "result_deadline_ns": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.new_port(**options)
        default_port = self.new_port()
        receipt = default_port.absence_receipt(str(uuid4()), self.input_deadline)
        self.assertIsNotNone(receipt)
        self.assertIsNone(default_port.result_baseline)
        self.assertEqual(self.enumeration_calls, [])
        self.assertEqual(default_port.observe_result().status, "REJECTED")

    def test_optional_result_deadline_and_slow_preparation_precede_fresh_absence(self):
        self.port = self.new_port(result_observer=self.observer)
        self.enumeration_hook = lambda: setattr(self.clock, "value", 20_000_000_000)
        self.port.prepare_request(self.request_id)
        baseline = self.port.result_baseline
        self.assertIsNone(baseline.deadline_ns)
        self.assertGreaterEqual(baseline.finished_ns, 20_000_000_000)
        self.input_deadline = self.clock()+2_000_000_000
        receipt = self.port.absence_receipt(self.request_id, self.input_deadline)
        self.assertLess(baseline.finished_ns, receipt.started_ns)
        self.assertEqual(len(self.enumeration_calls), 1)
        self.assertEqual(self.input.method_calls, [])

    def test_source_change_during_prepare_does_not_publish_a_usable_baseline(self):
        self.port = self.new_port(result_observer=self.observer)
        self.enumeration_hook = lambda: setattr(self, "current", False)
        with self.assertRaisesRegex(ValueError, "source_changed_across_result_baseline"):
            self.port.prepare_request(self.request_id)
        self.assertIsNone(self.port.result_baseline)
        self.current = True
        with self.assertRaises(ValueError):
            self.port.trigger_once(self.request_id, str(uuid4()), self.input_deadline)
        self.assertEqual(self.input.method_calls, [])

    def test_rejected_baseline_blocks_trigger_with_zero_input(self):
        self.data = replace(self.data, errors=("query_denied",))
        with self.assertRaisesRegex(ValueError, "result_baseline_rejected"):
            self.baseline()
        with self.assertRaises(ValueError):
            self.port.trigger_once(self.request_id, str(uuid4()), self.input_deadline)
        self.assertIsNone(self.port.result_baseline)
        self.assertEqual(self.input.method_calls, [])

    def test_real_absence_interval_is_not_refreshed_after_baseline_work(self):
        def advance():
            self.clock.value = 2_000
        self.enumeration_hook = advance
        receipt = self.baseline()
        baseline = self.port.result_baseline
        self.assertLess(receipt.finished_ns, baseline.started_ns)
        self.assertGreaterEqual(baseline.finished_ns, 2_000)
        self.assertEqual(baseline.request_id, self.request_id)
        self.assertEqual(baseline.frame_id, self.frame.frame_id)
        absence = next(t for t in self.port.timings if t.operation == "absence")
        self.assertEqual((absence.started_ns, absence.finished_ns),
                         (receipt.started_ns, receipt.finished_ns))
        self.assertEqual(baseline.deadline_ns, 100_000)

    def test_slow_baseline_cannot_borrow_business_result_budget_for_input(self):
        self.enumeration_hook = lambda: setattr(self.clock, "value", self.input_deadline)
        with self.assertRaises(TimeoutError):
            self.baseline()
        self.assertIsNone(self.port.result_baseline)
        self.assertEqual(self.input.method_calls, [])

    def test_changed_source_after_baseline_or_slow_final_source_check_blocks_input(self):
        self.enumeration_hook = lambda: setattr(self, "current", False)
        with self.assertRaisesRegex(ValueError, "source_changed_across_result_baseline"):
            self.baseline()
        self.assertIsNone(self.port.result_baseline)
        self.assertEqual(self.input.method_calls, [])

        self.port = self.new_port(result_observer=self.observer, result_deadline_ns=100_000)
        self.current = True
        self.enumeration_hook = None
        def slow_current():
            self.clock.value = self.input_deadline
            return True
        with patch.object(self.port, "_current", slow_current), self.assertRaises(TimeoutError):
            self.baseline()
        self.assertIsNone(self.port.result_baseline)
        self.assertEqual(self.input.method_calls, [])

    def test_cross_request_frame_and_prebaseline_permit_never_reach_click(self):
        self.baseline()
        for changes in ({"request_id": str(uuid4())}, {"frame_id": str(uuid4())},
                        {"consumed_ns": self.port.result_baseline.finished_ns}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.port.commit_once(self.instance, self.permit(**changes), self.input_deadline)
        self.assertIsNone(self.port.commit_permit)
        self.assertFalse(self.port.commit_metadata["commit_called"])
        self.input.click_once.assert_not_called()

    def test_accidentally_rebound_observer_invalidates_commit_before_input(self):
        self.baseline()
        self.observer.capture_baseline(str(uuid4()), self.frame.frame_id, str(uuid4()), deadline_ns=100_000)
        with self.assertRaises(ValueError):
            self.port.commit_once(self.instance, self.permit(), self.input_deadline)
        self.input.click_once.assert_not_called()

    def test_actual_permit_and_distinct_input_result_deadlines_are_preserved(self):
        self.baseline()
        permit = self.permit()
        self.port.commit_once(self.instance, permit, self.input_deadline)
        self.assertIs(self.port.commit_permit, permit)
        self.assertEqual(self.input.click_once.call_args.args[:3], (self.instance, permit, self.input_deadline))
        new = self.new_window()
        self.data = ResultWindowEnumeration((new,), new.hwnd, new.hwnd)
        result = self.port.observe_result(expected_title_sha256=new.title_sha256)
        self.assertEqual(result.status, "NEW_WINDOW_OBSERVED")
        self.assertEqual(result.dispatch_id, permit.dispatch_id)
        self.assertEqual(result.consumed_ns, permit.consumed_ns)
        self.assertFalse(result.business_success)
        self.assertEqual(self.port.commit_metadata,
            {"commit_called": True, "commit_returned_normally": True,
             "input_outcome_unknown": False, "business_success": False})
        with self.assertRaises(ValueError):
            self.port.commit_once(self.instance, permit, self.input_deadline)
        self.input.click_once.assert_called_once()

    def test_cleanup_retires_input_but_allows_later_read_only_observation(self):
        self.baseline()
        permit = self.permit()
        self.port._bound = self.instance
        self.port.commit_once(self.instance, permit, self.input_deadline)
        self.assertEqual(self.port.cleanup_once(self.instance, 15_000), "CONFIRMED_ABSENT")
        self.clock.value = self.input_deadline + 1_000
        new = self.new_window()
        self.data = ResultWindowEnumeration((new,), new.hwnd, new.hwnd)
        result = self.port.observe_result()
        self.assertEqual(result.status, "NEW_WINDOW_OBSERVED")
        self.assertGreater(result.finished_ns, self.input_deadline)
        for operation in (
                lambda: self.port.trigger_once(self.request_id, str(uuid4()), 20_000),
                lambda: self.port.select_once(self.instance, self.mapping.transport_bbox_physical, self.request_id, 20_000),
                lambda: self.port.native_snapshot(self.request_id, 20_000),
                lambda: self.port.commit_once(self.instance, permit, 20_000)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual([call[0] for call in self.input.method_calls], ["click_once"])

    def test_unknown_dispatch_stays_unknown_even_if_window_is_observed(self):
        self.baseline()
        permit = self.permit()
        self.input.click_once.side_effect = InputOutcomeUnknown("dispatch_partial_or_late")
        with self.assertRaises(InputOutcomeUnknown):
            self.port.commit_once(self.instance, permit, self.input_deadline)
        self.assertIs(self.port.commit_permit, permit)
        self.assertTrue(self.port.commit_metadata["input_outcome_unknown"])
        self.assertFalse(self.port.commit_metadata["commit_returned_normally"])
        new = self.new_window()
        self.data = ResultWindowEnumeration((new,), new.hwnd, new.hwnd)
        self.assertEqual(self.port.observe_result().status, "NEW_WINDOW_OBSERVED")
        self.assertTrue(self.port.commit_metadata["input_outcome_unknown"])
        with self.assertRaises(ValueError):
            self.port.commit_once(self.instance, permit, self.input_deadline)
        self.input.click_once.assert_called_once()

    def test_observation_without_actual_commit_does_not_query_or_create_permit(self):
        self.baseline()
        self.assertEqual(self.port.observe_result().status, "REJECTED")
        self.assertEqual(len(self.enumeration_calls), 1)
        self.assertIsNone(self.port.commit_permit)
        self.assertEqual(self.input.method_calls, [])

    def test_expired_input_keeps_consumed_permit_and_never_allows_second_commit(self):
        self.baseline()
        permit = self.permit()
        self.clock.value = self.input_deadline
        with self.assertRaises(TimeoutError):
            self.port.commit_once(self.instance, permit, self.input_deadline)
        self.assertIs(self.port.commit_permit, permit)
        self.assertTrue(self.port.commit_metadata["commit_called"])
        self.assertFalse(self.port.commit_metadata["commit_returned_normally"])
        self.assertFalse(self.port.commit_metadata["input_outcome_unknown"])
        with self.assertRaises(ValueError):
            self.port.commit_once(self.instance, permit, 20_000)
        self.input.click_once.assert_not_called()


if __name__ == "__main__":
    unittest.main()
