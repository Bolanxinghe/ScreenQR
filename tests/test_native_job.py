"""Pure job/UI lifecycle seams; no Tk root, native API, capture, input or network.

Fake controller/port results establish orchestration only, never QR acceptance.
Existing controller/port/result tests own their independent proof boundaries.
"""
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from test_native_port import fixture
from screenqr.app import ScreenQRApp
from screenqr.controller import DispatchResult
from screenqr.native_job import (INPUT_BUDGET_NS, NativeJob, NativeJobResult,
    count_qr_symbols, describe_job_result, _DeadlinePort, MAX_SETTLED_RESULT_SAMPLES, TargetInspection)
from screenqr.native_result import NativeResultObservation


def dispatched(status="DISPATCHED_UNCONFIRMED"):
    return DispatchResult(status, "synthetic", str(uuid4()), "OUTCOME_UNKNOWN", True, True,
        "CONFIRMED_ABSENT", None, True, 1, 10)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.frame, self.mapping, _, _ = fixture()
        self.now = 1_000_000_000
        self.calls = []
        self.port = SimpleNamespace(commit_permit=object(),
            last_stage_error_code=None, last_native_error_code=None, last_selection_error_code=None)
        self.port.commit_once = lambda *args: self.calls.append(("commit", args[-1]))
        self.port.cleanup_once = lambda *args: self.calls.append(("cleanup", args[-1])) or "CONFIRMED_ABSENT"
        self.port.observe_result = Mock(return_value=NativeResultObservation(
            "NEW_WINDOW_OBSERVED", "synthetic_metadata_only"))
        self.port_factory = Mock(return_value=self.port)
        self.controller = SimpleNamespace(cancel=Mock(return_value="CANCELLED_BEFORE_DISPATCH"))
        def run(frame):
            self.calls.append(("run", frame.frame_id))
            self.capped.commit_once(None, None, self.now + 20_000_000_000)
            self.capped.cleanup_once(None, self.now + 500_000_000)
            return dispatched()
        self.controller.run_and_dispatch = run
        def controller_factory(capped, **kwargs):
            self.capped = capped
            return self.controller
        self.controller_factory = Mock(side_effect=controller_factory)

    def make_job(self, *, counter=None, confirmed=True):
        return NativeJob(self.frame, self.mapping, entered_ns=1_000_000_000,
            single_target_confirmed=confirmed, clock=lambda: self.now,
            counter=counter or (lambda frame: 0), port_factory=self.port_factory,
            controller_factory=self.controller_factory, observer_factory=Mock())

    def test_zero_decode_still_needs_confirmation_and_two_symbols_never_create_port(self):
        for confirmed, count, expected in ((False, 0, "TARGET_NOT_CONFIRMED"),
                                           (True, 2, "MULTIPLE_CODES")):
            with self.subTest(confirmed=confirmed, count=count):
                job = self.make_job(counter=lambda frame: count, confirmed=confirmed)
                job.run()
                self.assertEqual(job.result.status, expected)
                self.assertTrue(job.done.is_set())
        self.port_factory.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_display_epoch_change_blocks_native_before_port_creation(self):
        job=self.make_job()
        job._display_guard=lambda:False
        self.assertFalse(job.source_is_current())
        job.run()
        self.assertEqual(job.result.status,'CANCELLED')
        self.port_factory.assert_not_called()
        self.assertEqual(self.calls,[])

    def test_unknown_display_guard_fails_closed_without_native_calls(self):
        job=self.make_job()
        job._display_guard=Mock(side_effect=OSError())
        self.assertFalse(job.source_is_current())
        job.run()
        self.port_factory.assert_not_called()
        self.assertEqual(self.calls,[])

    def test_symbol_count_does_not_merge_same_text_instances(self):
        duplicate = SimpleNamespace(valid=True, text="same")
        with patch("screenqr.native_job.read_qr_instances", return_value=[duplicate, duplicate]):
            self.assertEqual(count_qr_symbols(self.frame), 2)

    def test_unknown_symbol_may_dispatch_once_but_window_is_not_business_success(self):
        job = self.make_job()
        job.run()
        job.run()  # One context cannot run a second controller.
        self.assertEqual(sum(name == "commit" for name, value in self.calls), 1)
        self.assertTrue(job.done.is_set())
        self.assertEqual(job.result.status, "DISPATCHED_UNCONFIRMED")
        self.assertFalse(job.result.business_success)
        text, _ = describe_job_result(job.result)
        self.assertNotIn("成功", text)
        self.assertIn("尚未确认", text)
        self.assertFalse(job.source_is_current())
        self.assertIsNone(job._frame)
        self.assertIsNone(job._mapping)

    def test_login_notice_is_enabled_only_by_independently_inspected_target(self):
        job=self.make_job(counter=lambda frame:TargetInspection(1,None,True))
        self.port.observe_result.return_value=NativeResultObservation('CAMERA_REQUIRED','login_requires_camera_scan')
        job.run()
        self.assertTrue(job._observer_factory.call_args.kwargs['inspect_login_notice'])
        self.assertEqual(job.result.status,'UNSUPPORTED_BUSINESS')
        self.assertEqual(job.result.reason,'login_requires_camera_scan')
        self.assertEqual(job.result.dispatch_count,1)
        message,warning=describe_job_result(job.result)
        self.assertIn('仅支持微信摄像头扫码',message)
        self.assertTrue(warning)
        self.assertFalse(job.result.business_success)

    def test_result_rechecks_unknown_cleanup_without_repeating_input(self):
        from dataclasses import replace
        self.controller.run_and_dispatch = lambda frame: replace(dispatched(), cleanup_status='UNKNOWN')
        self.port.confirm_cleanup_absence = Mock(return_value='CONFIRMED_ABSENT')
        job = self.make_job()
        job.run()
        self.assertEqual(job.result.cleanup_status, 'CONFIRMED_ABSENT')
        self.port.confirm_cleanup_absence.assert_called_once_with()
        self.assertEqual(self.calls, [])
        self.assertNotIn('Esc', describe_job_result(job.result)[0])

    def test_cancel_controller_first_then_pure_lease_and_no_queued_admission(self):
        job = self.make_job()
        observed = []
        job._controller = SimpleNamespace(cancel=lambda: observed.append(job.source_is_current()) or "CANCELLED")
        self.assertEqual(job.cancel(), "CANCELLED")
        self.assertEqual(observed, [True])
        self.assertFalse(job.source_is_current())
        job.run()
        self.assertEqual(job.result.status, "CANCELLED")
        self.port_factory.assert_not_called()

    def test_cancel_during_controller_creation_cannot_enter_run(self):
        job = self.make_job()
        def construct(*args, **kwargs):
            job.cancel()
            return self.controller
        self.controller_factory.side_effect = construct
        job.run()
        self.assertEqual(job.result.status, "CANCELLED")
        self.assertEqual(self.calls, [])

    def test_source_invalidation_after_commit_keeps_read_only_observation(self):
        job = self.make_job()
        run = self.controller.run_and_dispatch
        self.controller.cancel.return_value = "TOO_LATE"
        def cancel_after_commit(frame):
            result = run(frame)
            self.assertEqual(job.cancel(), "TOO_LATE")
            return result
        self.controller.run_and_dispatch = cancel_after_commit
        job.run()
        self.assertFalse(job.source_is_current())
        self.port.observe_result.assert_called_once_with()
        self.assertEqual(sum(name == "commit" for name, value in self.calls), 1)
        self.assertEqual(job.result.observation.status, "NEW_WINDOW_OBSERVED")
        self.assertFalse(job.result.business_success)

    def test_retired_controller_does_not_stop_known_dispatch_observation(self):
        job = self.make_job()
        self.controller.cancel.return_value = "NO_ACTIVE_REQUEST"
        observed_once = []
        def observe():
            if not observed_once:
                observed_once.append(True)
                self.assertEqual(job.cancel(), "NO_ACTIVE_REQUEST")
                return NativeResultObservation("NOT_READY", "synthetic")
            return NativeResultObservation("NEW_WINDOW_OBSERVED", "synthetic_metadata_only")
        self.port.observe_result.side_effect = observe
        job.run()
        self.assertEqual(self.port.observe_result.call_count, 2)
        self.assertEqual(job.result.observation.status, "NEW_WINDOW_OBSERVED")

    def test_explicit_exit_stops_observation_after_commit(self):
        job = self.make_job()
        run = self.controller.run_and_dispatch
        self.controller.cancel.return_value = "TOO_LATE"
        def exit_after_commit(frame):
            result = run(frame)
            self.assertEqual(job.cancel(stop_observation=True), "TOO_LATE")
            return result
        self.controller.run_and_dispatch = exit_after_commit
        job.run()
        self.port.observe_result.assert_not_called()
        self.assertIsNone(job.result.observation)
        self.assertEqual(sum(name == "commit" for name, value in self.calls), 1)

    def test_uncertain_cancellation_or_dispatch_does_not_continue_observation(self):
        for cancellation_raises, dispatch_status in ((True, "DISPATCHED_UNCONFIRMED"),
                                                     (False, "OUTCOME_UNKNOWN")):
            with self.subTest(cancellation_raises=cancellation_raises):
                self.controller.cancel.side_effect = RuntimeError("synthetic") if cancellation_raises else None
                self.controller.cancel.return_value = "TOO_LATE"
                job = self.make_job()
                def run(frame):
                    job.cancel()
                    return dispatched(dispatch_status)
                self.controller.run_and_dispatch = run
                job.run()
                self.assertIsNone(job.result.observation)
                self.port.observe_result.assert_not_called()

    def test_settled_result_metadata_has_terminal_without_job_wall_clock_expiry(self):
        job = self.make_job()
        self.controller.cancel.return_value = "TOO_LATE"
        def observe_settled():
            job.cancel()
            self.now += 20_000_000_000
            return NativeResultObservation("NOT_READY", "synthetic", progress_sha256="a"*64)
        self.port.observe_result.side_effect = observe_settled
        with patch.object(job._observe_stop, "wait", return_value=False):
            job.run()
        self.assertEqual(self.port.observe_result.call_count, MAX_SETTLED_RESULT_SAMPLES)
        self.assertIsNone(job.result_deadline_ns)
        self.assertIsNone(self.port_factory.call_args.kwargs["result_deadline_ns"])

    def test_unchanged_login_page_uses_finite_low_frequency_observation_not_input_retries(self):
        job=self.make_job(counter=lambda frame:TargetInspection(1,None,True))
        self.port.observe_result.return_value=NativeResultObservation('NOT_READY','login_notice_not_confirmed',
            progress_sha256='a'*64)
        with patch.object(job._observe_stop,'wait',return_value=False) as waiting:
            job.run()
        self.assertEqual(self.port.observe_result.call_count,MAX_SETTLED_RESULT_SAMPLES)
        delays=[call.args[0] for call in waiting.call_args_list]
        self.assertGreater(max(delays),min(delays))
        self.assertGreater(sum(delays),20)
        self.assertLessEqual(max(delays),2)
        self.assertEqual(sum(name=='commit' for name,value in self.calls),1)
        self.assertEqual(job.result.status,'DISPATCHED_UNCONFIRMED')
        self.assertEqual(job.result.observation.reason, "result_unconfirmed_after_complete_settled_samples")
        self.assertFalse(job.result.business_success)
        self.assertEqual(sum(name == "commit" for name, _ in self.calls), 1)

    def test_slow_counter_does_not_consume_a_job_input_budget(self):
        def count(frame):
            self.now += 20_000_000_000
            return 0
        job = self.make_job(counter=count)
        job.run()
        self.assertIn(("commit", self.now+20_000_000_000), self.calls)
        self.assertIn(("cleanup", self.now+500_000_000), self.calls)
        self.assertIsNone(job.input_deadline_ns)
        # 新红线：解码不消耗输入时间；进入微信控制器后共用两秒，不逐步续期。
        self.assertEqual(self.controller_factory.call_args.kwargs["policy"].request_budget_ns,INPUT_BUDGET_NS)
        self.assertIsNone(self.port_factory.call_args.kwargs["result_deadline_ns"])
        self.assertEqual(job.result.status, "DISPATCHED_UNCONFIRMED")

    def test_late_input_return_is_rejected_and_cleanup_keeps_own_budget(self):
        def late_commit(*args):
            self.now = 3_000_000_000
        self.port.commit_once = late_commit
        capped = _DeadlinePort(self.port, 3_000_000_000, lambda: self.now)
        with self.assertRaises(TimeoutError):
            capped.commit_once(None, None, 99_000_000_000)
        self.assertEqual(capped.cleanup_once(None, 3_500_000_000), "CONFIRMED_ABSENT")
        self.assertIn(("cleanup", 3_500_000_000), self.calls)

    def test_completion_slot_survives_full_notification_queue_and_waits_for_cleanup(self):
        release = threading.Event()
        cleaning = threading.Event()
        def run(frame):
            cleaning.set()
            release.wait(2)
            return dispatched()
        self.controller.run_and_dispatch = run
        self.port.commit_permit = None
        job = self.make_job()
        thread = threading.Thread(target=job.run)
        notifications = queue.Queue(maxsize=1)
        notifications.put_nowait("full")
        thread.start()
        self.assertTrue(cleaning.wait(1))
        self.assertFalse(job.done.is_set())
        self.assertIsNone(job.result)
        release.set()
        thread.join(1)
        self.assertTrue(job.done.is_set())
        self.assertIsNotNone(job.result)
        self.assertEqual(notifications.get_nowait(), "full")

    def test_exception_text_is_not_published_and_unknown_window_never_upgrades_dispatch(self):
        job = self.make_job(counter=Mock(side_effect=ValueError(r"C:\private\secret.png")))
        job.run()
        self.assertEqual(job.result.reason, "native_job_failed")
        self.assertTrue(job.result.retry_safe_before_input)
        self.assertNotIn("private", repr(job.result))
        result = NativeJobResult(self.frame.frame_id, 1, self.mapping.stage_id, 3,
            "OUTCOME_UNKNOWN", "synthetic", True, 1, 20, "UNKNOWN",
            NativeResultObservation("NEW_WINDOW_OBSERVED", "synthetic"))
        text, warning = describe_job_result(result)
        self.assertIn("无法识别", text)
        self.assertNotIn("成功", text)
        self.assertTrue(warning)

    def test_failure_reports_stage_and_fixed_reason_without_guessing_configuration(self):
        result = NativeJobResult(self.frame.frame_id, 1, self.mapping.stage_id, 3,
            "FAILED", "port_error:ValueError", diagnostics=(
                ("last_operation", "verify_stage"),
                ("last_stage_error_code", "source_pixels_differ")))
        text, warning = describe_job_result(result)
        self.assertIn("无法识别", text)
        self.assertNotIn("source_pixels_differ", text)
        self.assertNotIn("4.1.15.13", text)
        self.assertTrue(warning)
        unknown = NativeJobResult(self.frame.frame_id, 1, self.mapping.stage_id, 3,
            "REJECTED", "https://private.test/secret", diagnostics=(
                ("last_operation", "C:/private/screenshot.png"),))
        text, _ = describe_job_result(unknown)
        self.assertNotIn("private", text)
        self.assertNotIn("4.1.15.13", text)

    def test_failed_job_retains_actual_fixed_operation_without_exception_text(self):
        self.port.verify_stage = Mock(side_effect=ValueError("C:/private/screenshot.png"))
        def fail_in_stage(frame):
            self.capped.verify_stage(frame, self.now+INPUT_BUDGET_NS)
        self.controller.run_and_dispatch = fail_in_stage
        job = self.make_job()
        job.run()
        diagnostics = dict(job.result.diagnostics)
        self.assertEqual(diagnostics["last_operation"], "verify_stage")
        self.assertNotIn("private", repr(job.result))
        text, _ = describe_job_result(job.result)
        self.assertIn("无法识别", text)


    def test_only_transient_result_foreground_change_is_resampled_without_more_input(self):
        self.port.observe_result.side_effect = [
            NativeResultObservation("STALE", "foreground_changed_during_result_query"),
            NativeResultObservation("NEW_WINDOW_OBSERVED", "synthetic_metadata_only")]
        job = self.make_job()
        original_deadline = job.result_deadline_ns
        job.run()
        self.assertEqual(self.port.observe_result.call_count, 2)
        self.assertEqual(job.result.observation.status, "NEW_WINDOW_OBSERVED")
        self.assertEqual(job.result_deadline_ns, original_deadline)
        self.assertEqual(sum(name == "commit" for name, _ in self.calls), 1)
        self.assertFalse(job.result.retry_safe_before_input)
        self.calls.clear()
        self.port.observe_result.reset_mock(side_effect=True)
        self.port.observe_result.return_value = NativeResultObservation("STALE", "other_stale_reason")
        job = self.make_job()
        job.run()
        self.assertEqual(self.port.observe_result.call_count, 1)

    def test_only_proven_zero_input_failure_may_release_frame_reservation(self):
        for triggered in (False, True):
            with self.subTest(triggered=triggered):
                self.controller.run_and_dispatch = lambda frame: DispatchResult(
                    "REJECTED", "source_generation_or_stage_changed", str(uuid4()),
                    "CANCELLED", triggered, False, "NOT_NEEDED", None, False, 0, None)
                job = self.make_job()
                job.run()
                self.assertIs(job.result.retry_safe_before_input, not triggered)


class AppLifecycleTests(unittest.TestCase):
    def app(self):
        app = ScreenQRApp.__new__(ScreenQRApp)
        app._closed = app._closing = False
        app._generation = 1
        app._native_job = None
        app._native_thread = None
        app._native_button = None
        app._set_status = Mock()
        return app

    def test_ui_invalidation_cancels_before_generation_and_widget_destruction(self):
        app = self.app()
        app._generation = 7
        order = []
        observation_stops = []
        def cancel(*, stop_observation=False):
            observation_stops.append(stop_observation)
            order.append(("cancel", app._generation))
        app._native_job = SimpleNamespace(done=threading.Event(),
            cancel=cancel)
        app._active_freeze = app._frozen_frame = None
        app._freeze_stage_epoch = 3
        app._freeze_preview = SimpleNamespace(destroy=lambda: order.append(("destroy", app._generation)))
        app._invalidate_source()
        self.assertEqual(order, [("cancel", 7), ("destroy", 8)])
        self.assertEqual(observation_stops, [False])
        self.assertIsNotNone(app._native_job)  # Cleanup still owns the context.

    def test_explicit_app_exit_stops_read_only_native_observation(self):
        app = self.app()
        app._closing = True
        cancel = Mock()
        app._native_job = SimpleNamespace(done=threading.Event(), cancel=cancel)
        app._cancel_native_before_mutation()
        cancel.assert_called_once_with(stop_observation=True)

    def test_same_frozen_frame_cannot_start_second_native_job(self):
        app = self.app()
        app._require_ui_thread = Mock()
        app._poll_native_job = Mock()
        app._frozen_frame = SimpleNamespace(frame_id="frame")
        app._auto_handoff_request = None
        app._native_spent_frame_id = "frame"
        app.describe_frozen_stage = Mock()
        app.request_native_handoff()
        app.describe_frozen_stage.assert_not_called()
        self.assertIsNone(app._native_job)

    def test_close_waits_asynchronously_for_native_and_decoder_before_tk_destroy(self):
        app = self.app()
        app._closing = True  # _finish_close runs only after close() cancels the source.
        done = threading.Event()
        app._native_job = SimpleNamespace(done=done, result=None)
        app._worker = SimpleNamespace(is_alive=lambda: False)
        app.root, app.finder = Mock(), Mock()
        app._stop_event_delivery = Mock()
        app._finish_close()
        app.root.after.assert_called_once_with(50, app._finish_close)
        app.root.destroy.assert_not_called()
        done.set()
        app._finish_close()
        app._stop_event_delivery.assert_called_once()
        app.root.destroy.assert_called_once()

    def test_close_wait_rejects_new_user_actions_before_any_widget_or_external_work(self):
        app = self.app()
        app._closing = True
        # No Tk widgets exist on this seam: accessing them while closing is a
        # failure. The app remains responsive only to let its old job retire.
        app._selected = Mock(side_effect=AssertionError("new selection while closing"))
        app._invalidate_source = Mock(side_effect=AssertionError("new mutation while closing"))
        app._opening = False
        with patch("screenqr.app.webbrowser.open") as browser:
            for method in (app.toggle_pause, app.hide_viewfinder, app.show_viewfinder,
                           app.choose_image, app.copy_selected, app.open_selected,
                           app._auto_changed, app.cancel_freeze, app.request_freeze):
                method()
            app._open_url("https://example.com/")
            browser.assert_not_called()
        app._selected.assert_not_called()
        app._invalidate_source.assert_not_called()


if __name__ == "__main__":
    unittest.main()
