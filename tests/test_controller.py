"""Pure synthetic port-boundary tests, never real-machine/business acceptance.

No private fixture, native call, GUI, screen capture or WeChat input is used.
"""
from dataclasses import replace
import hashlib
from pathlib import Path
import sys
import threading
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screenqr.controller import HandoffController
from screenqr.frozen_frame import FrozenFrame, MonitorDescriptor, TopologyDescriptor, TOPOLOGY_KIND
from screenqr.handoff import AbsenceReceipt, NativeSnapshot, Policy
from screenqr.stage import FrozenStageMapping, OwnStageSnapshot, StagePixelVerification


class Clock:
    def __init__(self):
        self.value = 1_000
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.value += 1
            return self.value


def fixture():
    digest = lambda value: hashlib.sha256(value).hexdigest()
    topology = TopologyDescriptor(TOPOLOGY_KIND, (0, 0, 3200, 2000),
        (MonitorDescriptor('DISPLAY1', (0, 0, 3200, 2000), 192, 192, True, 0),))
    frame = FrozenFrame(str(uuid4()), 1, (10, 10, 12, 12), (10, 10, 12, 12),
                        2, 2, bytes(range(12)), 0.0, 0.0000001, topology)
    own = OwnStageSnapshot(777, 778, 779, 123, (50, 50, 160, 160),
                           (60, 60, 150, 150), (100, 100, 102, 102), (76, 76, 126, 126), 192)
    mapping = FrozenStageMapping(frame.frame_id, 1, frame.capture_sha256, frame.topology_sha256,
        str(uuid4()), 3, 2, 2, own.image_bbox_physical, (92, 92, 110, 110),
        (8, 8), own.padding_bbox_physical, own, (777,), 200)
    verification = StagePixelVerification(mapping, frame.capture_sha256, digest(b'transport'),
        digest(b'transport'), digest(b'white'), digest(b'white'), str(uuid4()), 0.0, 0.0, 200)
    candidate = dict(hwnd=901, pid=700, process_created_filetime_100ns=123456,
        executable_path=r'C:\Program Files\Tencent\Weixin\Weixin.exe', title='Weixin',
        window_class='Qt51514QWindowIcon', window_rect=[0, 0, 3200, 2000],
        client_screen_rect=[0, 0, 3200, 2000], dpi=192, foreground_match=True)
    probe = dict(status='UNIQUE', errors=[], candidates=[candidate], foreground_hwnd_at_start=901,
        foreground_hwnd_at_end=901, monitor_count=1, primary_screen_rect=[0, 0, 3200, 2000],
        thread_dpi_awareness=2)
    return frame, verification, probe


class Port:
    def __init__(self, clock):
        self.clock = clock
        self.frame, self.verification, self.probe = fixture()
        self.calls, self.hooks, self.cleanup_calls = [], {}, []
        self.current, self.valid_evidence, self.baseline_absent = True, True, True
        self.cleanup_result = 'CONFIRMED_ABSENT'

    def visit(self, name):
        self.calls.append(name)
        hook = self.hooks.get(name)
        if hook:
            hook()

    def verify_stage(self, frame, deadline_ns):
        self.deadline = deadline_ns
        self.visit('verify')
        return replace(self.verification, checked_monotonic_ns=self.clock())

    def stage_is_current(self, frame, verification):
        self.visit('current')
        return self.current

    def absence_receipt(self, request_id, deadline_ns):
        self.visit('absence')
        probe = dict(status='NOT_FOUND', candidates=[], errors=[]) if self.baseline_absent else self.probe
        return AbsenceReceipt.from_probe(request_id, probe, started_ns=self.clock(), finished_ns=self.clock())

    def trigger_once(self, request_id, trigger_id, deadline_ns):
        self.visit('trigger')

    def native_snapshot(self, request_id, deadline_ns):
        self.visit('snapshot')
        return NativeSnapshot.from_probe(self.probe, started_ns=self.clock(), finished_ns=self.clock(),
                                         fixed_file_version=(4, 1, 15, 13))

    def select_once(self, instance, bbox, request_id, deadline_ns):
        self.selected_instance = instance
        self.visit('select')

    def selection_evidence(self, frame, stage, instance, request_id, deadline_ns):
        self.visit('evidence')
        start, end = self.clock(), self.clock()
        return dict(schema_version=1, selection_extent_status='VERIFIED' if self.valid_evidence else 'UNVERIFIED',
            observed_selection_bbox_physical=stage.transport_bbox_physical,
            extent_verification_method='native-client-observed-boundaries-v1',
            frame_id=frame.frame_id, stage_id=stage.stage_id, stage_epoch=stage.stage_epoch,
            generation=frame.generation, source_capture_sha256=frame.capture_sha256,
            topology_sha256=frame.topology_sha256, session_id=request_id, evidence_id=str(uuid4()),
            started_monotonic_ns=start, finished_monotonic_ns=end, capture_completed_monotonic_ns=end,
            native_identity_before=self.probe, native_identity_after=self.probe,
            payload_comparison=dict(exact_rgb_bytes_equal=True, captured_rgb_sha256=frame.capture_sha256,
                expected_rgb_sha256=frame.capture_sha256, captured_bbox_physical=stage.payload_bbox_physical,
                width=frame.width, height=frame.height))

    def cleanup_once(self, instance, cleanup_deadline_ns):
        self.cleanup_calls.append((instance, cleanup_deadline_ns))
        self.visit('cleanup')
        return self.cleanup_result


class ControllerTests(unittest.TestCase):
    def setup_controller(self):
        self.clock = Clock()
        self.port = Port(self.clock)
        policy = Policy(request_budget_ns=10_000, max_evidence_age_ns=1_000,
                        max_observation_gap_ns=100, max_commit_visual_age_ns=50)
        self.controller = HandoffController(self.port, policy=policy, cleanup_budget_ns=500, clock=self.clock)
        return self.controller

    def run_frame(self):
        return self.controller.run(self.port.frame)

    def test_strict_selection_success_is_retired_without_commit(self):
        self.setup_controller()
        result = self.run_frame()
        self.assertEqual(result.status, 'SELECTION_VERIFIED')
        self.assertEqual(result.cleanup_status, 'CONFIRMED_ABSENT')
        self.assertEqual(self.port.calls.count('trigger'), 1)
        self.assertEqual(self.port.calls.count('select'), 1)
        self.assertEqual(self.port.cleanup_calls[0][0], result.native_instance)
        self.assertFalse(result.commit_authorization)
        self.assertFalse(result.selection_reusable)
        self.assertFalse(hasattr(self.controller, 'commit'))

    def test_existing_baseline_is_never_triggered_or_closed(self):
        self.setup_controller()
        self.port.baseline_absent = False
        result = self.run_frame()
        self.assertFalse(result.trigger_attempted)
        self.assertFalse(self.port.cleanup_calls)
        self.assertNotIn('trigger', self.port.calls)
        self.assertIsNone(result.native_instance)

    def test_cancellation_before_dispatch_prevents_input(self):
        self.setup_controller()
        replies = []
        self.port.hooks['absence'] = lambda: replies.append(self.controller.cancel())
        result = self.run_frame()
        self.assertEqual(replies, ['CANCELLED_BEFORE_DISPATCH'])
        self.assertEqual(result.status, 'CANCELLED')
        self.assertNotIn('trigger', self.port.calls)
        self.assertFalse(self.port.cleanup_calls)

    def test_source_change_or_deadline_at_boundary_prevents_trigger(self):
        for expiry in (False, True):
            with self.subTest(expiry=expiry):
                self.setup_controller()
                def invalidate():
                    if expiry:
                        self.clock.value = self.port.deadline
                    else:
                        self.port.current = False
                self.port.hooks['absence'] = invalidate
                result = self.run_frame()
                self.assertEqual(result.status, 'TIMED_OUT' if expiry else 'REJECTED')
                self.assertNotIn('trigger', self.port.calls)
                self.assertFalse(self.port.cleanup_calls)

    def test_trigger_dispatch_exception_is_unknown_without_retry_or_guessed_cleanup(self):
        for exception in (RuntimeError, TimeoutError):
            with self.subTest(exception=exception):
                self.setup_controller()
                def fail():
                    raise exception('synthetic dispatch failure')
                self.port.hooks['trigger'] = fail
                result = self.run_frame()
                self.assertEqual(result.status, 'OUTCOME_UNKNOWN')
                self.assertEqual(result.cleanup_status, 'UNBOUND_OUTCOME_UNKNOWN')
                self.assertEqual(self.port.calls.count('trigger'), 1)
                self.assertNotIn('snapshot', self.port.calls)
                self.assertFalse(self.port.cleanup_calls)

    def test_unproven_content_cleans_only_full_bound_instance(self):
        self.setup_controller()
        self.port.valid_evidence = False
        result = self.run_frame()
        self.assertEqual(result.status, 'CONTENT_UNPROVEN')
        self.assertEqual(self.port.cleanup_calls[0][0], self.port.selected_instance)
        self.assertEqual(self.port.cleanup_calls[0][0], result.native_instance)
        self.assertEqual(len(self.port.cleanup_calls), 1)

    def test_select_exception_does_not_retry_and_keeps_cleanup_unknown(self):
        self.setup_controller()
        def fail():
            raise RuntimeError('synthetic select failure')
        self.port.hooks['select'] = fail
        self.port.cleanup_result = 'CLOSED'  # Unrecognized status is not a confirmation.
        result = self.run_frame()
        self.assertEqual(result.status, 'OUTCOME_UNKNOWN')
        self.assertEqual(self.port.calls.count('select'), 1)
        self.assertEqual(result.cleanup_status, 'UNKNOWN')
        self.assertEqual(self.port.cleanup_calls[0][0], result.native_instance)

    def test_cancel_during_evidence_rejects_late_valid_receipt_and_cleans_bound_instance(self):
        self.setup_controller()
        replies = []
        self.port.hooks['evidence'] = lambda: replies.append(self.controller.cancel())
        result = self.run_frame()
        self.assertEqual(replies, ['CANCELLED_AFTER_SELECT_DISPATCH'])
        self.assertEqual(result.status, 'CANCELLED')
        self.assertEqual(len(self.port.cleanup_calls), 1)
        self.assertNotEqual(result.handoff_state, 'SELECTION_VERIFIED')

    def test_cleanup_has_independent_deadline_and_late_confirmation_is_unknown(self):
        self.setup_controller()
        self.port.hooks['select'] = lambda: setattr(self.clock, 'value', self.port.deadline + 100)
        result = self.run_frame()
        self.assertEqual(result.status, 'TIMED_OUT')
        self.assertGreater(self.port.cleanup_calls[0][1], self.port.deadline)
        self.setup_controller()
        self.port.hooks['cleanup'] = lambda: setattr(self.clock, 'value', self.port.cleanup_calls[-1][1])
        result = self.run_frame()
        self.assertEqual(result.status, 'SELECTION_VERIFIED')
        self.assertEqual(result.cleanup_status, 'UNKNOWN')

    def test_busy_run_does_not_cancel_existing_request(self):
        self.setup_controller()
        entered, release = threading.Event(), threading.Event()
        def pause():
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test barrier expired')
        self.port.hooks['verify'] = pause
        results = []
        worker = threading.Thread(target=lambda: results.append(self.run_frame()))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.run_frame().status, 'BUSY')
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].status, 'SELECTION_VERIFIED')

    def test_cancel_and_trigger_have_a_shared_linearization_lock(self):
        self.setup_controller()
        entered, release, snapshot_entered, snapshot_release = [threading.Event() for _ in range(4)]
        cancel_started, cancel_done = threading.Event(), threading.Event()
        def trigger_pause():
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test barrier expired')
        def snapshot_pause():
            snapshot_entered.set()
            if not snapshot_release.wait(2):
                raise RuntimeError('test barrier expired')
        self.port.hooks.update(trigger=trigger_pause, snapshot=snapshot_pause)
        results, replies = [], []
        worker = threading.Thread(target=lambda: results.append(self.run_frame()))
        def cancel():
            cancel_started.set()
            replies.append(self.controller.cancel())
            cancel_done.set()
        canceller = threading.Thread(target=cancel)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            canceller.start()
            self.assertTrue(cancel_started.wait(2))
            self.assertFalse(cancel_done.wait(0.03))  # Cannot acknowledge cancellation during held dispatch.
            release.set()
            self.assertTrue(cancel_done.wait(2))
            self.assertTrue(snapshot_entered.wait(2))
        finally:
            release.set()
            snapshot_release.set()
            worker.join(2)
            if canceller.ident is not None:
                canceller.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(canceller.is_alive())
        self.assertEqual(replies, ['CANCELLED_AFTER_TRIGGER_DISPATCH'])
        self.assertEqual(results[0].status, 'CANCELLED')
        self.assertEqual(results[0].cleanup_status, 'UNBOUND_OUTCOME_UNKNOWN')
        self.assertEqual(self.port.calls.count('trigger'), 1)
        self.assertNotIn('select', self.port.calls)
        self.assertFalse(self.port.cleanup_calls)


if __name__ == '__main__':
    unittest.main(verbosity=2)
