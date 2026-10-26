"""Synthetic single-request dispatch boundaries, not real/business acceptance.

No GUI, native API, screenshot, private fixture or network is used. Dispatch
counts below mean calls into the fake port, never accepted native input.
"""
from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch

from test_controller import Clock, Port
from screenqr.controller import DispatchResult, HandoffController, PreparationResult
from screenqr.handoff import NATIVE_DISPLAY_PROFILE_A, ObservationReceipt, Policy


class DispatchPort(Port):
    def __init__(self, clock):
        super().__init__(clock)
        self.observations = []
        self.statuses = []
        self.candidates = []
        self.commits = []
        self.evidence_values = []

    def selection_evidence(self, frame, stage, instance, request_id, deadline_ns):
        evidence = super().selection_evidence(frame, stage, instance, request_id, deadline_ns)
        start = evidence['started_monotonic_ns']
        end = evidence['finished_monotonic_ns']
        comparison = evidence['payload_comparison']
        rendered = hashlib.sha256(frame.rgb8.translate(bytes(max(v-1, 0) for v in range(256)))).hexdigest()
        comparison.update(captured_rgb_sha256=rendered, exact_rgb_bytes_equal=False,
            capture_started_monotonic_ns=start, capture_ended_monotonic_ns=end)
        evidence.update(status='PAYLOAD_MISMATCH', errors=[], read_only=True,
            native_input_sent=False, raw_pixels_persisted=False, deadline_monotonic_ns=deadline_ns,
            version_metadata=dict(fixed_file_version=[4,1,15,13], expected_fixed_file_version=[4,1,15,13]))
        if self.hooks.get('evidence_value'):
            self.hooks['evidence_value'](evidence)
        self.evidence_values.append(deepcopy(evidence))
        return evidence

    def observe_toolbar(self, extent, *, observation_id, deadline_ns):
        self.visit('toolbar')
        status = self.statuses.pop(0) if self.statuses else 'READ_ONLY_CANDIDATE'
        candidate = self.candidates.pop(0) if self.candidates else (200,200,220,220)
        data = dict(read_only=True, errors=[], status=status, session_id=extent.request_id,
            generation=self.frame.generation, started_ns=self.clock(), finished_ns=self.clock(),
            version_metadata=dict(fixed_file_version=[4,1,15,13]), identity_before=self.probe,
            identity_after=self.probe, selection_bbox_physical=extent.actual_selection_bbox_physical,
            candidate_bbox_physical=candidate if status == 'READ_ONLY_CANDIDATE' else None)
        if self.hooks.get('visual_value'):
            self.hooks['visual_value'](data)
        result = ObservationReceipt.from_native_observation(data, observation_id=observation_id)
        self.observations.append((extent, result))
        return result

    def commit_once(self, instance, permit, deadline_ns):
        self.commits.append((instance, permit, deadline_ns))
        self.visit('commit')


class DispatchControllerTests(unittest.TestCase):
    def setUp(self):
        self.setup_controller()
        # Fake clocks advance on observations; no real sleeping is necessary.
        self.wait_patch = patch('screenqr.controller.time.sleep', return_value=None)
        self.wait_patch.start()
        self.addCleanup(self.wait_patch.stop)

    def setup_controller(self):
        self.clock = Clock()
        self.port = DispatchPort(self.clock)
        self.policy = Policy(2_000_000_000, 300_000_000, 200_000_000, 100_000_000,
                             selection_evidence_mode=NATIVE_DISPLAY_PROFILE_A)
        self.controller = HandoffController(self.port, policy=self.policy,
                                            cleanup_budget_ns=500, clock=self.clock)

    def run_frame(self):
        return self.controller.run_and_dispatch(self.port.frame)

    def assert_no_commit(self, result):
        self.assertFalse(result.dispatch_attempted)
        self.assertEqual(result.dispatch_count, 0)
        self.assertIsNone(result.dispatch_latency_ns)
        self.assertFalse(self.port.commits)
        self.assertFalse(result.commit_authorization)

    def test_one_request_three_visuals_fresh_selection_then_consumed_permit(self):
        observed_gate = []
        self.port.hooks['commit'] = lambda: observed_gate.append(self.controller._active.session.snapshot())
        original = self.port.frame.rgb8
        result = self.run_frame()
        self.assertIsInstance(result, DispatchResult)
        self.assertEqual(result.status, 'DISPATCHED_UNCONFIRMED')
        self.assertEqual(result.handoff_state, 'OUTCOME_UNKNOWN')
        self.assertTrue(result.dispatch_attempted)
        self.assertEqual(result.dispatch_count, 1)
        self.assertGreater(result.dispatch_latency_ns, 0)
        self.assertLess(result.dispatch_latency_ns, self.policy.request_budget_ns)
        self.assertEqual(self.port.calls.count('trigger'), 1)
        self.assertEqual(self.port.calls.count('select'), 1)
        self.assertEqual(self.port.calls.count('evidence'), 3)
        self.assertEqual(self.port.calls.count('toolbar'), 3)
        self.assertEqual(observed_gate[0]['commit_attempts'], 1)
        self.assertEqual(observed_gate[0]['state'], 'OUTCOME_UNKNOWN')
        latest = self.port.evidence_values[-1]
        last_visual = self.port.observations[-1][1]
        self.assertGreater(last_visual.started_ns, latest['finished_monotonic_ns'])
        self.assertIs(self.port.frame.rgb8, original)
        self.assertFalse(result.commit_authorization)
        self.assertFalse(result.selection_reusable)
        self.assertEqual(result.cleanup_status, 'CONFIRMED_ABSENT')
        self.assertEqual(len(self.port.cleanup_calls), 1)
        self.assertEqual(self.port.cleanup_calls[0][0], result.native_instance)

    def test_slow_visuals_get_fresh_extent_each_frame_without_extending_age(self):
        original_observe = self.port.observe_toolbar
        ages, identifiers = [], []
        def slow_observe(extent, **kwargs):
            self.clock.value += 170_000_000
            age = self.clock.value-extent.measured_ns
            ages.append(age)
            identifiers.append(extent.evidence_id)
            if age > self.policy.max_evidence_age_ns:
                raise ValueError('synthetic fixed-age detector refusal')
            return original_observe(extent, **kwargs)
        self.port.observe_toolbar = slow_observe
        result = self.run_frame()
        self.assertEqual(result.status, 'DISPATCHED_UNCONFIRMED')
        self.assertEqual(self.policy.max_evidence_age_ns, 300_000_000)
        self.assertEqual(len(ages), 3)
        self.assertTrue(all(age <= 300_000_000 for age in ages))
        self.assertEqual(len(set(identifiers)), 3)
        self.assertEqual(self.port.calls.count('evidence'), 3)
        self.assertEqual(self.port.calls.count('trigger'), 1)
        self.assertEqual(self.port.calls.count('select'), 1)
        self.assertEqual(result.dispatch_count, 1)

    def test_run_remains_preparation_only_and_cannot_continue_selection(self):
        result = self.controller.run(self.port.frame)
        self.assertIs(type(result), PreparationResult)
        self.assertEqual(result.status, 'SELECTION_VERIFIED')
        self.assertNotIn('toolbar', self.port.calls)
        self.assertNotIn('commit', self.port.calls)
        self.assertFalse(result.selection_reusable)
        self.assertEqual(len(self.port.cleanup_calls), 1)

    def test_not_ready_ambiguous_then_three_new_stable_visuals(self):
        self.port.statuses = ['NOT_READY', 'AMBIGUOUS']
        result = self.run_frame()
        self.assertEqual(result.status, 'DISPATCHED_UNCONFIRMED')
        self.assertEqual(self.port.calls.count('toolbar'), 5)
        self.assertEqual(result.dispatch_count, 1)
        self.assertEqual(self.port.calls.count('select'), 1)

    def test_readiness_loss_after_two_resets_stability_without_input_retry(self):
        self.port.statuses = ['READ_ONLY_CANDIDATE', 'READ_ONLY_CANDIDATE', 'NOT_READY']
        result = self.run_frame()
        self.assertEqual(result.status, 'DISPATCHED_UNCONFIRMED')
        self.assertEqual(self.port.calls.count('toolbar'), 6)
        self.assertEqual(self.port.calls.count('evidence'), 6)
        self.assertEqual(self.port.calls.count('trigger'), 1)
        self.assertEqual(self.port.calls.count('select'), 1)
        self.assertEqual(result.dispatch_count, 1)

    def test_changed_candidate_requires_three_new_matching_visuals(self):
        first, second = (200,200,220,220), (240,200,260,220)
        self.port.candidates = [first, second, second, second]
        result = self.run_frame()
        self.assertEqual(result.status, 'DISPATCHED_UNCONFIRMED')
        self.assertEqual(self.port.calls.count('toolbar'), 4)
        self.assertEqual(self.port.commits[0][1].candidate_bbox_physical, second)

    def test_cancel_during_observation_or_latest_selection_prevents_commit(self):
        for boundary in ('toolbar', 'latest_selection'):
            with self.subTest(boundary=boundary):
                self.setup_controller()
                replies = []
                if boundary == 'toolbar':
                    self.port.hooks['toolbar'] = lambda: replies.append(self.controller.cancel())
                else:
                    def cancel_refresh():
                        if self.port.calls.count('evidence') == 2:
                            replies.append(self.controller.cancel())
                    self.port.hooks['evidence'] = cancel_refresh
                result = self.run_frame()
                self.assertEqual(result.status, 'CANCELLED')
                self.assertEqual(replies, ['CANCELLED_AFTER_SELECT_DISPATCH'])
                self.assert_no_commit(result)
                self.assertEqual(len(self.port.cleanup_calls), 1)

    def test_source_invalidated_at_final_native_snapshot_prevents_commit(self):
        def invalidate():
            if self.port.calls.count('snapshot') == 2:
                self.port.current = False
        self.port.hooks['snapshot'] = invalidate
        result = self.run_frame()
        self.assertEqual(result.status, 'REJECTED')
        self.assert_no_commit(result)

    def test_wrong_latest_selection_epoch_prevents_commit(self):
        def corrupt(evidence):
            if self.port.calls.count('evidence') == 2:
                evidence['stage_epoch'] += 1
        self.port.hooks['evidence_value'] = corrupt
        result = self.run_frame()
        self.assertEqual(result.status, 'CONTENT_UNPROVEN')
        self.assertEqual(self.controller.display_prediction_mismatches, 0)
        self.assert_no_commit(result)

    def test_wrong_latest_payload_can_recover_only_from_new_exact_sample(self):
        def corrupt(evidence):
            if self.port.calls.count('evidence') == 2:
                evidence['payload_comparison']['captured_rgb_sha256'] = '0'*64
        self.port.hooks['evidence_value'] = corrupt
        result = self.run_frame()
        self.assertEqual(result.status, 'DISPATCHED_UNCONFIRMED')
        self.assertEqual(self.port.calls.count('evidence'), 4)
        self.assertEqual(self.controller.display_prediction_mismatches, 1)
        self.assertEqual(self.port.calls.count('trigger'), 1)
        self.assertEqual(self.port.calls.count('select'), 1)
        self.assertEqual(result.dispatch_count, 1)
        latest = self.port.evidence_values[-1]
        visual = self.port.observations[-1][1]
        self.assertGreater(visual.started_ns, latest['finished_monotonic_ns'])
        self.assertEqual(len({v['evidence_id'] for v in self.port.evidence_values}), 4)

    def test_persistently_wrong_latest_payload_expires_original_budget_without_commit(self):
        def corrupt(evidence):
            if self.port.calls.count('evidence') >= 2:
                evidence['payload_comparison']['captured_rgb_sha256'] = '0'*64
                # Bounded fake time: persistent bad input cannot spin for 2B
                # one-nanosecond iterations when the sleep boundary is injected.
                self.clock.value += self.policy.request_budget_ns//4
        self.port.hooks['evidence_value'] = corrupt
        result = self.run_frame()
        self.assertEqual(result.status, 'TIMED_OUT')
        self.assertGreater(self.controller.display_prediction_mismatches, 0)
        self.assertLessEqual(self.controller.display_prediction_mismatches, 4)
        self.assertEqual(self.port.calls.count('trigger'), 1)
        self.assertEqual(self.port.calls.count('select'), 1)
        self.assert_no_commit(result)

    def test_expiry_during_readiness_or_before_commit_is_zero_commit(self):
        for boundary in ('toolbar', 'snapshot'):
            with self.subTest(boundary=boundary):
                self.setup_controller()
                def expire():
                    if boundary == 'toolbar' or self.port.calls.count('snapshot') == 2:
                        self.clock.value = self.port.deadline
                self.port.hooks[boundary] = expire
                result = self.run_frame()
                self.assertEqual(result.status, 'TIMED_OUT')
                self.assert_no_commit(result)

    def test_replayed_visual_or_changed_native_is_rejected_without_commit(self):
        for change in ('generation', 'instance'):
            with self.subTest(change=change):
                self.setup_controller()
                def corrupt(data):
                    if change == 'generation':
                        data['generation'] += 1
                    else:
                        data['identity_after'] = deepcopy(data['identity_after'])
                        data['identity_after']['candidates'][0]['pid'] += 1
                self.port.hooks['visual_value'] = corrupt
                result = self.run_frame()
                self.assertEqual(result.status, 'REJECTED')
                self.assert_no_commit(result)

    def test_commit_raise_or_late_return_is_unknown_one_call_never_retry(self):
        for failure in (RuntimeError, TimeoutError, 'late'):
            with self.subTest(failure=failure):
                self.setup_controller()
                def fail():
                    if failure == 'late':
                        self.clock.value = self.port.deadline
                    else:
                        raise failure('synthetic refusal or partial input, indistinguishable')
                self.port.hooks['commit'] = fail
                result = self.run_frame()
                self.assertEqual(result.status, 'OUTCOME_UNKNOWN')
                self.assertEqual(result.handoff_state, 'OUTCOME_UNKNOWN')
                self.assertTrue(result.dispatch_attempted)
                self.assertEqual(result.dispatch_count, 1)
                self.assertEqual(self.port.calls.count('commit'), 1)
                self.assertEqual(len(self.port.cleanup_calls), 1)
                self.assertFalse(result.commit_authorization)

    def test_cancel_after_consume_is_too_late_during_dispatch_and_cleanup(self):
        replies = []
        self.port.hooks['commit'] = lambda: replies.append(self.controller.cancel())
        self.port.hooks['cleanup'] = lambda: replies.append(self.controller.cancel())
        result = self.run_frame()
        self.assertEqual(replies, ['TOO_LATE', 'TOO_LATE'])
        self.assertEqual(result.status, 'DISPATCHED_UNCONFIRMED')
        self.assertEqual(result.handoff_state, 'OUTCOME_UNKNOWN')
        self.assertEqual(result.dispatch_count, 1)

    def test_source_change_during_commit_stays_unknown(self):
        self.port.hooks['commit'] = lambda: setattr(self.port, 'current', False)
        result = self.run_frame()
        self.assertEqual(result.status, 'OUTCOME_UNKNOWN')
        self.assertEqual(result.dispatch_count, 1)
        self.assertEqual(len(self.port.cleanup_calls), 1)

    def test_post_dispatch_probe_exception_is_unknown_even_after_input_returned(self):
        def fail_after_commit():
            if self.port.commits:
                raise RuntimeError('synthetic post-dispatch probe failure')
        self.port.hooks['current'] = fail_after_commit
        result = self.run_frame()
        self.assertEqual(result.status, 'OUTCOME_UNKNOWN')
        self.assertEqual(result.handoff_state, 'OUTCOME_UNKNOWN')
        self.assertEqual(result.dispatch_count, 1)

    def test_final_visual_age_is_rechecked_after_native_probe(self):
        def delay_final_probe():
            if self.port.calls.count('snapshot') == 2:
                self.clock.value += self.policy.max_commit_visual_age_ns+1
        self.port.hooks['snapshot'] = delay_final_probe
        result = self.run_frame()
        self.assertEqual(result.status, 'REJECTED')
        self.assertEqual(result.reason, 'commit_visual_expired')
        self.assert_no_commit(result)

    def test_clock_regression_at_commit_return_is_unknown_not_a_clean_dispatch(self):
        self.port.hooks['commit'] = lambda: setattr(self.clock, 'value', -1)
        result = self.run_frame()
        self.assertEqual(result.status, 'OUTCOME_UNKNOWN')
        self.assertEqual(result.dispatch_count, 1)
        self.assertIsNone(result.dispatch_latency_ns)
        self.assertEqual(result.cleanup_status, 'UNKNOWN')

    def test_busy_dispatch_does_not_cancel_existing_single_request(self):
        replies = []
        self.port.hooks['verify'] = lambda: replies.append(self.run_frame())
        result = self.run_frame()
        self.assertEqual(replies[0].status, 'BUSY')
        self.assertFalse(replies[0].dispatch_attempted)
        self.assertEqual(replies[0].dispatch_count, 0)
        self.assertIsNone(replies[0].dispatch_latency_ns)
        self.assertFalse(replies[0].commit_authorization)
        self.assertEqual(result.status, 'DISPATCHED_UNCONFIRMED')
        self.assertEqual(result.dispatch_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
