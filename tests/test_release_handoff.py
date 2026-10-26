"""Release-mode admission boundaries only; no Tk windows or native input."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from screenqr.app import ScreenQRApp, Viewfinder, _FreezeRequest


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class ReleaseHandoffTests(unittest.TestCase):
    def app(self):
        app = ScreenQRApp.__new__(ScreenQRApp)
        app._closed = app._closing = False
        app._generation = 3
        app._native_job = app._native_thread = None
        app._native_auto_frame_id = None
        app._native_auto_frame_key = None
        app._native_spent_frame_id = None
        app._native_requires_gesture = False
        app._native_stage_focus_ready = True
        app._readiness_misses = 0
        app._candidate = app._pending_confirm = app._confirm_inflight = app._browser_grant = None
        app._auto_internal_pause = True
        app._auto_handoff_callback = None
        app._auto_stage_settling = False
        app._auto_stage_signature = ("ready",)
        app._auto_stage_readiness = Mock(return_value=(True, ("ready",)))
        app._auto_seen_frames = set()
        app._active_freeze = None
        app._freeze_preview = None
        app._freeze_stage_epoch = 0
        app.wechat_on_release = Value(True)
        app.auto_open = Value(False)
        app.paused = Value(True)
        app.root = Mock()
        app.auto_check = Mock()
        app.pause_button = Mock()
        app.finder = SimpleNamespace(_visible=True, _draw=Mock(),
            capture_bbox=Mock(return_value=(0, 0, 20, 30)))
        app._set_status = Mock()
        app.count_label, app.kind_label = Mock(), Mock()
        app._set_payload = Mock()
        app._refresh_native_action = Mock()
        app._require_ui_thread = Mock()
        app._auto_handoff_request = self.request()
        app._frozen_frame = SimpleNamespace(frame_id="release", generation=3,
            width=20, height=30, capture_sha256="same-rgb-hash")
        return app

    @staticmethod
    def request():
        return _FreezeRequest(3, "release", (0, 0, 20, 30), (1,),
            10.01, None, 10_000_000_000)

    def test_only_changed_explicit_gesture_triggers_and_stacking_is_ignored(self):
        finder = Viewfinder.__new__(Viewfinder)
        finder.app = SimpleNamespace(_viewfinder_released=Mock(), _invalidate_source=Mock())
        finder.capture_bbox = Mock(return_value=(0, 0, 20, 30))
        finder._gesture_bbox = (0, 0, 20, 30)
        finder._drag, finder._resize = (1, 2, 3, 4), None
        finder._release(None)  # A click without a changed source is not consent.
        finder.app._viewfinder_released.assert_not_called()
        finder._gesture_bbox = (0, 0, 20, 30)
        finder._drag = (1, 2, 3, 4)
        finder.capture_bbox.return_value = (1, 0, 21, 30)
        with patch("screenqr.app.time.monotonic_ns", return_value=10_000_000_000):
            finder._release(None)
        finder.app._viewfinder_released.assert_called_once_with(10_000_000_000, (1, 0, 21, 30))
        finder._last_geometry = (0, 0, 20, 30)
        for name, value in (("winfo_rootx", 0), ("winfo_rooty", 0),
                            ("winfo_width", 20), ("winfo_height", 30)):
            setattr(finder, name, Mock(return_value=value))
        finder._region_changed(SimpleNamespace(widget=finder))
        finder.app._invalidate_source.assert_not_called()
        finder.winfo_rootx.return_value = 1
        finder._region_changed(SimpleNamespace(widget=finder))
        finder.app._invalidate_source.assert_called_once()

    def test_source_invalidation_cancels_pending_and_stale_mode_or_generation_cannot_enter(self):
        app = self.app()
        app._active_freeze = app._frozen_frame = None
        app._freeze_stage_epoch, app._freeze_preview = 1, None
        app._auto_handoff_callback = "idle-1"
        app._invalidate_source()
        app.root.after_cancel.assert_called_once_with("idle-1")
        self.assertIsNone(app._auto_handoff_request)
        self.assertEqual(app._generation, 4)
        for changed in ("mode", "generation", "geometry"):
            with self.subTest(changed=changed):
                app = self.app()
                request = app._auto_handoff_request
                app.request_native_handoff = Mock()
                if changed == "mode":
                    app.wechat_on_release.set(False)
                elif changed == "generation":
                    app._generation += 1
                else:
                    app.finder.capture_bbox.return_value = (1, 0, 21, 30)
                app._start_auto_handoff(request)
                app.request_native_handoff.assert_not_called()

    def test_busy_does_not_queue_and_polling_pause_cannot_arm_another_freeze(self):
        app = self.app()
        app._poll_native_job = Mock()
        app._native_job = object()
        app.request_freeze = Mock()
        app._viewfinder_released(10_000_000_000, (0, 0, 20, 30))
        app.request_freeze.assert_not_called()
        app._native_job = None
        def pause_while_polling():
            app.paused.set(True)
            app._auto_internal_pause = False
        app._poll_native_job.side_effect = pause_while_polling
        app._viewfinder_released(10_000_000_000, (0, 0, 20, 30))
        app.request_freeze.assert_not_called()

    def test_shortcut_and_gesture_admission_preserve_request_identity_and_distinct_consent(self):
        for automatic in (True, False):
            with self.subTest(automatic=automatic):
                app = self.app()
                request = app._auto_handoff_request
                app._poll_native_job = Mock()
                app.describe_frozen_stage = Mock(return_value="own-mapping")
                if not automatic:
                    app._auto_handoff_request = None
                with patch("screenqr.app.NativeJob") as job_type, \
                        patch("screenqr.app.threading.Thread") as thread_type, \
                        patch("screenqr.app.time.monotonic_ns", return_value=11_500_000_000):
                    app.request_native_handoff(auto_request=request if automatic else None)
                self.assertEqual(job_type.call_args.kwargs["entered_ns"],
                    request.native_entered_ns if automatic else 11_500_000_000)
                self.assertIs(job_type.call_args.kwargs["single_target_confirmed"], True)
                thread_type.return_value.start.assert_called_once()
                self.assertEqual(app._native_spent_frame_id, "release")

    def test_dedup_uses_dimensions_and_hash_and_never_evicts_at_limit(self):
        app = self.app()
        request = app._auto_handoff_request
        app.request_native_handoff = Mock()
        app._auto_seen_frames = {(20, 30, "same-rgb-hash")}
        with patch("screenqr.app.time.monotonic_ns", return_value=11_000_000_000):
            app._start_auto_handoff(request)
        app.request_native_handoff.assert_not_called()
        app = self.app()
        request = app._auto_handoff_request
        app.request_native_handoff = Mock()
        app._auto_seen_frames = {(30, 20, "same-rgb-hash")}
        with patch("screenqr.app.time.monotonic_ns", return_value=11_000_000_000):
            app._start_auto_handoff(request)
        app.request_native_handoff.assert_called_once_with(auto_request=request)
        app.request_native_handoff.reset_mock()
        app._auto_seen_frames = {(20, 30, str(i)) for i in range(512)}
        with patch("screenqr.app.time.monotonic_ns", return_value=11_000_000_000):
            app._start_auto_handoff(request)
        app.request_native_handoff.assert_not_called()
        self.assertFalse(app.wechat_on_release.get())
        self.assertEqual(len(app._auto_seen_frames), 512)

    def test_slow_preparation_does_not_expire_a_still_current_request(self):
        app = self.app()
        request = app._auto_handoff_request
        app.request_native_handoff = Mock()
        with patch("screenqr.app.time.monotonic_ns", return_value=120_000_000_000):
            app._start_auto_handoff(request)
        app.request_native_handoff.assert_called_once_with(auto_request=request)
        self.assertIs(app._auto_handoff_request,request)

    def test_unmapped_or_changing_stage_waits_for_readiness_without_total_recognition_deadline(self):
        app = self.app()
        request = app._auto_handoff_request
        app.request_native_handoff = Mock()
        app._auto_stage_readiness.side_effect = [(False, None), (True, ("mapped",)), (True, ("mapped",))]
        with patch("screenqr.app.time.monotonic_ns", return_value=10_300_000_000):
            app._start_auto_handoff(request)
            app.request_native_handoff.assert_not_called()
            self.assertIs(app._auto_handoff_request, request)
            app._start_auto_handoff(request)
            app.request_native_handoff.assert_not_called()
            app._start_auto_handoff(request)
        app.request_native_handoff.assert_called_once_with(auto_request=request)
        self.assertEqual(request.native_entered_ns, 10_000_000_000)
        self.assertFalse(app._auto_stage_settling)
        # Production admission consumes the request; a completed stage remains
        # bound to the original source even after a long preparation interval.
        self.assertIsNone(request.deadline_monotonic)

    def test_mapping_settles_before_one_normal_activation_and_finder_hide(self):
        app = self.app()
        request = app._auto_handoff_request
        app._native_stage_focus_ready = False
        app._freeze_preview = object()
        app._own_top_hwnd = Mock(side_effect=lambda widget: 101 if widget is app._freeze_preview else 202)
        app.finder.withdraw = Mock()
        app.request_native_handoff = Mock()
        app._auto_stage_readiness.side_effect = [(False, None), (True, ('mapped',)), (True, ('mapped',))]
        with patch('screenqr.app.focus_own_stage', return_value=True) as focus:
            app._start_auto_handoff(request)
            app._start_auto_handoff(request)
            focus.assert_not_called()
            app.finder.withdraw.assert_not_called()
            app.request_native_handoff.assert_not_called()
            app._start_auto_handoff(request)
        focus.assert_called_once_with(101, allowed_foreground_hwnds=(202, 101))
        app.finder.withdraw.assert_called_once_with()
        app.request_native_handoff.assert_called_once_with(auto_request=request)

    def test_external_focus_or_os_refusal_stops_without_hide_or_native_input(self):
        app = self.app()
        request = app._auto_handoff_request
        app._native_stage_focus_ready = False
        app._own_top_hwnd = Mock(side_effect=[101, 202])
        app.finder.withdraw = Mock()
        app._native_diagnostic = Mock()
        app._finish_wechat_attempt = Mock()
        app.request_native_handoff = Mock()
        with patch('screenqr.app.focus_own_stage', return_value=False) as focus:
            app._start_auto_handoff(request)
        focus.assert_called_once_with(101, allowed_foreground_hwnds=(202, 101))
        app.finder.withdraw.assert_not_called()
        app.request_native_handoff.assert_not_called()
        app._finish_wechat_attempt.assert_called_once()
        app.root.after.assert_not_called()  # No focus-stealing retry loop.

    def test_cancelled_while_mapping_never_activates_and_changed_after_focus_never_submits(self):
        app = self.app()
        request = app._auto_handoff_request
        app._native_stage_focus_ready = False
        app._auto_stage_readiness.return_value = (False, None)
        app.request_native_handoff = Mock()
        with patch('screenqr.app.focus_own_stage') as focus:
            app._start_auto_handoff(request)
            app._auto_handoff_request = None
            app._start_auto_handoff(request)
        focus.assert_not_called()
        app.request_native_handoff.assert_not_called()
        app = self.app()
        request = app._auto_handoff_request
        app._native_stage_focus_ready = False
        app.request_native_handoff = Mock()
        def changed_layout(frame):
            app._generation += 1
            return True, ('ready',)
        app._auto_stage_readiness.side_effect = changed_layout
        with patch('screenqr.app.focus_own_stage') as focus:
            app._start_auto_handoff(request)
        focus.assert_not_called()
        app.request_native_handoff.assert_not_called()
        app = self.app()
        request = app._auto_handoff_request
        app._native_stage_focus_ready = False
        app._own_top_hwnd = Mock(side_effect=[101, 202])
        app.finder.withdraw = Mock()
        app.request_native_handoff = Mock()
        def focus(*args, **kwargs):
            app._generation += 1
            return True
        with patch('screenqr.app.focus_own_stage', side_effect=focus):
            app._start_auto_handoff(request)
        app.finder.withdraw.assert_not_called()
        app.request_native_handoff.assert_not_called()
        self.assertFalse(app._native_stage_focus_ready)

    def test_unknown_requires_new_gesture_without_disabling_wechat_or_replaying_frame(self):
        app = self.app()
        done = threading.Event()
        done.set()
        app._native_auto_frame_id = "release"
        key = (20, 30, "same-rgb-hash")
        app._native_auto_frame_key = key
        app._auto_seen_frames = {key}
        app._native_job = SimpleNamespace(frame_id="release", done=done,
            result=SimpleNamespace(status="OUTCOME_UNKNOWN", cleanup_status="UNKNOWN"))
        with patch("screenqr.app.describe_job_result", return_value=("unknown", True)):
            app._poll_native_job()
        self.assertTrue(app.wechat_on_release.get())
        self.assertTrue(app._native_requires_gesture)
        self.assertEqual(app._auto_seen_frames, {key})
        self.assertFalse(app.paused.get())
        self.assertIsNone(app._frozen_frame)
        self.assertIsNone(app._auto_handoff_request)
        app.root.after.assert_not_called()
        app.auto_check.configure.assert_not_called()

    def test_zero_input_rejection_releases_dedup_without_scheduling_another_request(self):
        for retry_safe in (True, False):
            with self.subTest(retry_safe=retry_safe):
                app = self.app()
                key = (20, 30, "same-rgb-hash")
                app._auto_seen_frames = {key}
                app._native_auto_frame_id = "release"
                app._native_auto_frame_key = key
                done = threading.Event()
                done.set()
                app._native_job = SimpleNamespace(frame_id="release", done=done,
                    result=SimpleNamespace(status="REJECTED", cleanup_status="NOT_NEEDED",
                        retry_safe_before_input=retry_safe))
                with patch("screenqr.app.describe_job_result", return_value=("rejected", True)):
                    app._poll_native_job()
                self.assertEqual(key in app._auto_seen_frames, not retry_safe)
                app.root.after.assert_not_called()
                self.assertIsNone(app._native_auto_frame_key)

    def test_stage_stacking_is_ignored_but_real_change_cancels_waiting_handoff(self):
        app = self.app()
        widget = Mock()
        widget.winfo_rootx.return_value = 1
        widget.winfo_rooty.return_value = 2
        widget.winfo_width.return_value = 20
        widget.winfo_height.return_value = 30
        widget.winfo_ismapped.return_value = True
        app._freeze_preview, app._freeze_image_label, app._freeze_white_stage = widget, None, None
        app._stage_geometry = {widget: (1, 2, 20, 30, 1)}
        app._freeze_stage_epoch = 4
        app._auto_handoff_callback = "idle-2"
        app._stage_layout_changed(SimpleNamespace(widget=widget))
        app.root.after_cancel.assert_not_called()
        widget.winfo_rootx.return_value = 2
        app._stage_layout_changed(SimpleNamespace(widget=widget))
        app.root.after_cancel.assert_called_once_with("idle-2")
        self.assertIsNone(app._auto_handoff_request)


if __name__ == "__main__":
    unittest.main()
