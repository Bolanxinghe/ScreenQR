"""Pure fixed-F readiness boundaries, not real Windows/business acceptance.

All samples, time, ports and dispatches are synthetic. No GUI, Win32, screenshots,
network or private fixture is involved; repeated reads never mean repeated input.
"""
import threading
import unittest
from unittest.mock import patch

import test_dispatch_controller as fixtures
from screenqr.controller import HandoffController
from screenqr.handoff import NATIVE_DISPLAY_PROFILE_A, Policy


class DisplayReadinessTests(unittest.TestCase):
    def setUp(self):
        self.clock = fixtures.Clock()
        self.port = fixtures.DispatchPort(self.clock)
        self.policy = Policy(2_000_000_000, 300_000_000, 200_000_000, 100_000_000,
                             selection_evidence_mode=NATIVE_DISPLAY_PROFILE_A)
        self.controller = HandoffController(self.port, policy=self.policy, cleanup_budget_ns=500, clock=self.clock)
        patched = patch('screenqr.controller.time.sleep', return_value=None)
        patched.start()
        self.addCleanup(patched.stop)

    def corrupt_first(self, evidence):
        if self.port.calls.count('evidence') == 1:
            evidence['payload_comparison']['captured_rgb_sha256'] = '0'*64

    def assert_single_inputs(self):
        self.assertEqual(self.port.calls.count('trigger'), 1)
        self.assertEqual(self.port.calls.count('select'), 1)
        self.assertLessEqual(len(self.port.commits), 1)

    def test_initial_mismatch_then_fresh_exact_sample_can_complete_single_dispatch(self):
        original = self.port.frame.rgb8
        self.port.hooks['evidence_value'] = self.corrupt_first
        result = self.controller.run_and_dispatch(self.port.frame)
        self.assertEqual(result.status, 'DISPATCHED_UNCONFIRMED')
        self.assertEqual(result.dispatch_count, 1)
        self.assertEqual(self.controller.display_prediction_mismatches, 1)
        self.assertEqual(len(self.port.evidence_values), 4)  # two initial + two visual refreshes
        self.assertEqual(len({v['evidence_id'] for v in self.port.evidence_values}), 4)
        self.assertIs(self.port.frame.rgb8, original)
        self.assert_single_inputs()

    def test_persistent_mismatch_times_out_original_request_with_count_preserved(self):
        self.port.hooks['evidence_value'] = lambda e: e['payload_comparison'].update(captured_rgb_sha256='0'*64)
        with patch('screenqr.controller.time.sleep', side_effect=lambda seconds: setattr(self.clock, 'value', self.port.deadline)):
            result = self.controller.run_and_dispatch(self.port.frame)
        self.assertEqual(result.status, 'TIMED_OUT')
        self.assertEqual(self.controller.display_prediction_mismatches, 1)
        self.assertEqual(self.port.calls.count('evidence'), 1)
        self.assertEqual(result.dispatch_count, 0)
        self.assert_single_inputs()

    def test_wait_releases_edge_for_cancellation_from_another_thread(self):
        self.port.hooks['evidence_value'] = self.corrupt_first
        replies = []
        threads = []
        def cancel_while_waiting(seconds):
            worker = threading.Thread(target=lambda: replies.append(self.controller.cancel()))
            threads.append(worker)
            worker.start()
            worker.join(0.5)
            self.assertFalse(worker.is_alive(), 'read-only waiting must release the dispatch edge')
        with patch('screenqr.controller.time.sleep', side_effect=cancel_while_waiting):
            result = self.controller.run_and_dispatch(self.port.frame)
        for worker in threads:
            worker.join(1)
        self.assertEqual(result.status, 'CANCELLED')
        self.assertEqual(replies, ['CANCELLED_AFTER_SELECT_DISPATCH'])
        self.assertEqual(self.port.calls.count('evidence'), 1)
        self.assertEqual(self.controller.display_prediction_mismatches, 1)
        self.assertEqual(result.dispatch_count, 0)
        self.assert_single_inputs()

    def test_source_change_while_waiting_prevents_next_sample_and_commit(self):
        self.port.hooks['evidence_value'] = self.corrupt_first
        with patch('screenqr.controller.time.sleep', side_effect=lambda seconds: setattr(self.port, 'current', False)):
            result = self.controller.run_and_dispatch(self.port.frame)
        self.assertEqual(result.status, 'REJECTED')
        self.assertEqual(result.reason, 'source_generation_or_stage_changed')
        self.assertEqual(self.port.calls.count('evidence'), 1)
        self.assertEqual(result.dispatch_count, 0)
        self.assert_single_inputs()

    def test_replayed_or_overlapping_final_refresh_cannot_reuse_initial_sample(self):
        for change in ('id', 'overlap', 'nonincreasing'):
            with self.subTest(change=change):
                # New synthetic request; no actual application inputs occur.
                clock = fixtures.Clock()
                port = fixtures.DispatchPort(clock)
                controller = HandoffController(port, policy=self.policy, cleanup_budget_ns=500, clock=clock)
                def corrupt(evidence):
                    if port.calls.count('evidence') == 2:
                        previous = port.evidence_values[0]
                        if change == 'id':
                            evidence['evidence_id'] = previous['evidence_id']
                        elif change == 'overlap':
                            evidence['started_monotonic_ns'] = previous['finished_monotonic_ns']-1
                        else:
                            evidence['started_monotonic_ns'] = previous['finished_monotonic_ns']
                            evidence['finished_monotonic_ns'] = previous['finished_monotonic_ns']
                port.hooks['evidence_value'] = corrupt
                result = controller.run_and_dispatch(port.frame)
                self.assertEqual(result.status, 'CONTENT_UNPROVEN')
                self.assertEqual(result.reason, 'selection_evidence_replayed_or_overlapping')
                self.assertEqual(result.dispatch_count, 0)
                self.assertEqual(controller.display_prediction_mismatches, 0)

    def test_replayed_id_after_mismatch_is_rejected_even_with_an_exact_hash(self):
        def corrupt(evidence):
            if self.port.calls.count('evidence') == 1:
                evidence['payload_comparison']['captured_rgb_sha256'] = '0'*64
            else:
                evidence['evidence_id'] = self.port.evidence_values[0]['evidence_id']
        self.port.hooks['evidence_value'] = corrupt
        result = self.controller.run_and_dispatch(self.port.frame)
        self.assertEqual(result.status, 'CONTENT_UNPROVEN')
        self.assertEqual(result.reason, 'selection_evidence_replayed_or_overlapping')
        self.assertEqual(self.controller.display_prediction_mismatches, 1)
        self.assertEqual(self.port.calls.count('evidence'), 2)
        self.assertEqual(result.dispatch_count, 0)
        self.assert_single_inputs()


if __name__ == '__main__':
    unittest.main()
