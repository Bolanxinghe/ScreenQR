"""Automatic admission and raw-capture binding; no GUI, input or network.

Public synthetic QR images below validate decoding, not issued WeChat business
credentials. Widget contracts use fake Tk constructors and never create windows.
"""
from dataclasses import replace
import hashlib
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import qrcode
from PIL import Image

from screenqr.app import ScreenQRApp, _FreezeRequest, _Job
from screenqr.core import classify_payload, decode_image
from screenqr.confirmation import build_candidate
from screenqr.decoding import read_qr_instances
from screenqr.frozen_frame import FrozenFrame, MonitorDescriptor, TopologyDescriptor, TOPOLOGY_KIND


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class AutomaticWeChatDetectionTests(unittest.TestCase):
    FIRST = 'https://weixin.qq.com/g/PUBLIC_NOT_ISSUED_FIRST'
    SECOND = 'https://weixin.qq.com/g/PUBLIC_NOT_ISSUED_SECOND'

    def app(self):
        app = ScreenQRApp.__new__(ScreenQRApp)
        app._closed = app._closing = app._opening = False
        app._results, app._result_key = [], ()
        app._selected = Mock(return_value=None)
        app.result_list, app.count_label = Mock(), Mock()
        app._selection_changed, app._set_status = Mock(), Mock()
        app._set_payload, app._refresh_actions = Mock(), Mock()
        app.kind_label, app.copy_button = Mock(), Mock()
        app.auto_open, app.wechat_on_release = Value(True), Value(True)
        app.paused = Value(False)
        app._automatic_candidate, app._candidate_count = None, 0
        app._wechat_candidate, app._wechat_candidate_count = None, 0
        app._auto_seen, app._auto_seen_frames = set(), set()
        app._wechat_seen_payloads = set()
        app._wechat_outcomes = {}
        app._wechat_active_digest = None
        app._native_outcome_digest = app._native_source_generation = None
        app._native_requires_gesture = False
        app._native_job = app._auto_handoff_request = None
        app._generation = 1
        app._candidate = app._pending_confirm = app._confirm_inflight = app._browser_grant = None
        app._last_opened_candidate = app._file_source = None
        app._preview_label, app._target_label = Mock(), Mock()
        app._native_stage_focus_ready = False
        app._next_scan = 0
        app.finder = SimpleNamespace(_visible=True, _drag=None, _resize=None,
            _begin_resize=Mock(),_motion=Mock(),_release=Mock(),
            attributes=Mock(), lower=Mock(),
            capture_bbox=Mock(return_value=(0, 0, 1000, 500)))
        app.request_freeze, app._open_url = Mock(), Mock()
        return app

    @staticmethod
    def digest(text):
        return hashlib.sha256(text.encode('utf-8')).digest()

    @staticmethod
    def observation(app, texts, *, path=None):
        """Complete located observations, retaining repeated payload instances."""
        token=(app._generation,'file',path,(1,2,3,4)) if path else (
            app._generation,'screen',app.finder.capture_bbox())
        with Image.new('RGB',(1000,500),'white') as pixels:
            for index,text in enumerate(texts):
                with qrcode.make(text).get_image().convert('RGB') as qr:
                    pixels.paste(qr,(10+index*500,10))
            instances=tuple(read_qr_instances(pixels))
            results=list({item.text:classify_payload(item.text,item.format) for item in instances}.values())
            candidate=build_candidate(instances,token,pixels)
        return results,instances,candidate,None,token

    def read(self, app, text, *, path=None):
        observation=self.observation(app,[text],path=path)
        app._accept_results(observation[0],path,0.02,observation=observation)

    def test_two_stable_wechat_reads_reserve_content_before_native_admission(self):
        app = self.app()
        digest = self.digest(self.FIRST)
        def admitted(**kwargs):
            self.assertIn(digest, app._wechat_seen_payloads)
            self.assertEqual(kwargs, dict(native_entered_ns=10_000_000_000,
                                         expected_wechat_digest=digest))
        app.request_freeze.side_effect = admitted
        with patch('screenqr.app.time.monotonic_ns', return_value=10_000_000_000):
            self.read(app, self.FIRST)
            app.request_freeze.assert_not_called()
            self.read(app, self.FIRST)
        app.request_freeze.assert_called_once()
        app._open_url.assert_not_called()

    def test_changed_empty_or_multiple_reads_cannot_complete_the_old_candidate(self):
        for interruption in ([], [classify_payload(self.SECOND)],
                             [classify_payload(self.FIRST), classify_payload(self.SECOND)]):
            with self.subTest(interruption=interruption):
                app = self.app()
                self.read(app, self.FIRST)
                observation=self.observation(app,[item.text for item in interruption])
                app._accept_results(observation[0],None,0.02,observation=observation)
                self.read(app, self.FIRST)
                app.request_freeze.assert_not_called()
                self.read(app, self.FIRST)
                app.request_freeze.assert_called_once()

    def test_busy_paused_hidden_dragging_and_unknown_outcome_block_automatic_admission(self):
        for blocked in ('job', 'request', 'paused', 'hidden', 'drag', 'resize',
                        'unknown', 'closed', 'closing'):
            with self.subTest(blocked=blocked):
                app = self.app()
                if blocked == 'job': app._native_job = object()
                elif blocked == 'request': app._auto_handoff_request = object()
                elif blocked == 'paused': app.paused.set(True)
                elif blocked == 'hidden': app.finder._visible = False
                elif blocked == 'drag': app.finder._drag = (1, 2, 3, 4)
                elif blocked == 'resize': app.finder._resize = (1, 2, 3, 4)
                elif blocked == 'unknown': app._native_requires_gesture = True
                elif blocked == 'closed': app._closed = True
                else: app._closing = True
                self.read(app, self.FIRST)
                self.read(app, self.FIRST)
                app.request_freeze.assert_not_called()
                self.assertEqual(app._wechat_seen_payloads, set())

    def test_web_screen_and_file_require_confirmation_while_wechat_files_do_not_auto_input(self):
        app = self.app()
        url = 'https://example.com/ordinary-public-test'
        self.read(app, url)
        app._open_url.assert_not_called()
        self.read(app, url)
        self.read(app, url)
        app._open_url.assert_not_called()
        self.assertEqual(app._candidate.open_url,url)
        app.request_freeze.assert_not_called()
        app = self.app()
        self.read(app, self.FIRST, path='public-test.png')
        self.read(app, self.FIRST, path='public-test.png')
        app.request_freeze.assert_not_called()
        app._open_url.assert_not_called()
        self.read(app, url, path='public-test.png')
        app._open_url.assert_not_called()
        self.assertEqual(app._candidate.open_url,url)
        app.request_freeze.assert_not_called()

    def test_zero_input_failure_does_not_turn_continuous_reads_into_native_retry(self):
        app = self.app()
        self.read(app, self.FIRST)
        self.read(app, self.FIRST)
        app.request_freeze.assert_called_once()
        digest = self.digest(self.FIRST)
        raw_key = (100, 100, 'public-frame-digest')
        app._auto_seen_frames = {raw_key}
        app._native_auto_frame_id, app._native_auto_frame_key = 'frame', raw_key
        done = threading.Event(); done.set()
        app._native_job = SimpleNamespace(frame_id='frame', done=done,
            result=SimpleNamespace(status='REJECTED', cleanup_status='NOT_NEEDED',
                                   retry_safe_before_input=True))
        app._resume_scanning, app._refresh_native_action = Mock(), Mock()
        with patch('screenqr.app.describe_job_result', return_value=('stopped', True)):
            app._poll_native_job()
        self.assertEqual(app._auto_seen_frames, set())
        self.assertEqual(app._wechat_seen_payloads, {digest})
        observation=self.observation(app,[])
        app._accept_results([],None,0.02,observation=observation)
        for _ in range(4): self.read(app, self.FIRST)
        # A stationary, handled target must not claim to have a running job.
        app._set_status.assert_called_with('无法识别：请重新框选完整二维码',warning=True)
        app.request_freeze.assert_called_once()
        self.read(app, self.SECOND)
        self.read(app, self.SECOND)
        self.assertEqual(app.request_freeze.call_count, 2)

    def prepared_capture_app(self):
        """Only the actual event/status path runs; no desktop reads or input."""
        app=self.app()
        app.root=Mock()
        app.root.winfo_screenwidth.return_value=3200
        app.root.winfo_screenheight.return_value=2000
        app.finder._draw=Mock()
        app._event_lock=threading.Lock()
        app._events,app._jobs=queue.Queue(),queue.Queue()
        app._stop=threading.Event()
        app._dropped_event_kinds=set()
        app._busy=True
        app._auto_internal_pause=True
        app._pending_confirm=app._pending_stage_verify=app._pending_image=None
        app._freeze_preview=app._frozen_frame=None
        app._freeze_stage_epoch=0
        app._next_scan=float('inf')
        return app

    def enqueue_capture(self,app,*,width=3190,height=100,expected_digest=True):
        topology=TopologyDescriptor(TOPOLOGY_KIND,(0,0,3200,2000),
            (MonitorDescriptor('PUBLIC',(0,0,3200,2000),192,192,True,0),))
        box=(0,0,width,height)
        frame=FrozenFrame(str(uuid4()),app._generation,box,box,width,height,
            b'\xff'*(width*height*3),1.01,1.02,topology)
        digest=self.digest(self.FIRST)
        request=_FreezeRequest(app._generation,frame.frame_id,box,(1,),1.0,None,
            1_000_000_000,expected_wechat_digest=digest if expected_digest else None)
        app._active_freeze=app._pending_freeze=app._auto_handoff_request=request
        app._wechat_active_digest=request.expected_wechat_digest
        app.finder.capture_bbox.return_value=box
        app.paused.set(True)
        app._events.put(('frozen',app._generation,frame.frame_id,frame,
            [classify_payload(self.FIRST)],0.02))
        return frame,digest

    def test_screen_wide_capture_reports_actionable_failure_and_never_fake_waits(self):
        app=self.prepared_capture_app()
        _,digest=self.enqueue_capture(app)
        # 3190px plus the mandatory 64px white border cannot fit 3200px.
        # Exercise the real early ValueError, event handler and resume path.
        with patch('screenqr.app.tk.Toplevel') as native_stage:
            app._tick()
            native_stage.assert_not_called()
        status=app._set_status.call_args.args[0]
        self.assertTrue(status.startswith('无法识别：'))
        self.assertIn('缩小取景框',status)
        self.assertIn('缩小图片显示比例',status)
        self.assertFalse(app.paused.get())
        self.assertIsNone(app._active_freeze)
        self.assertIsNone(app._native_job)
        self.assertEqual(app._wechat_outcomes[digest],(status,True))
        app.request_freeze.reset_mock()
        for _ in range(4): self.read(app,self.FIRST)
        app._set_status.assert_called_with(status,warning=True)
        app.request_freeze.assert_not_called()
        self.read(app,self.SECOND)
        self.read(app,self.SECOND)
        app.request_freeze.assert_called_once()

    def test_worker_rejects_only_exact_uniform_capture_before_native_preparation(self):
        for colour, changed_pixel in (((255,255,255),None), ((10,20,30),None),
                                      ((10,20,30),(10,20,31))):
            with self.subTest(colour=colour, changed_pixel=changed_pixel):
                app=self.prepared_capture_app()
                frame,_=self.enqueue_capture(app,width=4,height=4,expected_digest=False)
                app._events.get_nowait()  # Use the actual worker result below.
                with Image.new('RGB',(4,4),colour) as pixels:
                    if changed_pixel is not None:
                        pixels.putpixel((3,3),changed_pixel)
                    frame=replace(frame,rgb8=pixels.tobytes())
                original=frame.rgb8
                request=app._active_freeze
                app._pending_freeze=None
                app._jobs.put(_Job(app._generation,freeze=request))
                app._observe_image=Mock(return_value=([],(),None,None,
                    (app._generation,'screen',request.bbox)))
                app._show_frozen_frame=Mock()
                events=[]
                def posted(event):
                    events.append(event)
                    app._stop.set()
                    return True
                app._post_event=posted
                with patch('screenqr.app.capture_region_with_metadata',return_value=frame):
                    app._work()
                self.assertEqual(frame.rgb8,original)
                self.assertEqual(len(events),1)
                app._events.put(events[0])
                app._tick()
                if changed_pixel is None:
                    self.assertEqual(events[0][0],'freeze_error')
                    app._observe_image.assert_not_called()
                    app._show_frozen_frame.assert_not_called()
                    app._set_status.assert_called_with('无法识别',warning=True)
                    self.assertFalse(app.paused.get())
                    self.assertIsNone(app._native_job)
                else:
                    self.assertEqual(events[0][0],'frozen')
                    app._observe_image.assert_called_once()
                    app._show_frozen_frame.assert_called_once_with(frame)

    def test_new_deliberate_capture_replaces_only_the_same_targets_completion(self):
        app=self.prepared_capture_app()
        digest=self.digest(self.FIRST)
        other=self.digest(self.SECOND)
        app._wechat_outcomes={digest:('无法识别：旧结果',True),other:('other',True)}
        frame,_=self.enqueue_capture(app,width=100,height=100,expected_digest=False)
        app._show_frozen_frame=Mock()
        app._tick()
        app._show_frozen_frame.assert_called_once_with(frame)
        self.assertEqual(app._wechat_active_digest,digest)
        self.assertNotIn(digest,app._wechat_outcomes)
        self.assertEqual(app._wechat_outcomes[other],('other',True))
        self.assertIn(digest,app._wechat_seen_payloads)
        app.request_freeze.assert_not_called()

    def test_native_completions_remain_bound_to_content_including_unknown_result(self):
        for result,status in ((SimpleNamespace(status='REJECTED',cleanup_status='NOT_NEEDED'),
                               '无法识别：请检查微信'),
                              (SimpleNamespace(status='DISPATCHED_UNCONFIRMED',cleanup_status='NOT_NEEDED'),
                               '已交给微信，页面尚未确认，请查看识别结果。'),
                              (SimpleNamespace(status='UNSUPPORTED_BUSINESS',dispatch_count=1,cleanup_status='CONFIRMED_ABSENT'),
                               '无法识别：这个登录码仅支持微信摄像头扫码，当前方式不支持。'),
                              (None,'无法识别：请先查看微信，避免重复操作')):
            with self.subTest(result=result):
                app=self.prepared_capture_app()
                digest=self.digest(self.FIRST)
                app._active_freeze=None
                app._wechat_seen_payloads={digest}
                app._wechat_active_digest=app._native_outcome_digest=digest
                app._native_source_generation=app._generation
                done=threading.Event();done.set()
                app._native_job=SimpleNamespace(frame_id='frame',generation=app._generation,
                    done=done,result=result)
                with patch('screenqr.app.describe_job_result',return_value=(status,result is None or result.status!='DISPATCHED_UNCONFIRMED')):
                    app._poll_native_job()
                self.assertEqual(app.paused.get(),result is not None and (result.status=='DISPATCHED_UNCONFIRMED'
                    or (result.status=='UNSUPPORTED_BUSINESS' and result.dispatch_count==1)))
                self.assertIsNone(app._native_job)
                self.assertEqual(app._wechat_outcomes[digest][0],status)
                for _ in range(3): self.read(app,self.FIRST)
                self.assertEqual(app._set_status.call_args.args[0],status)
                app.request_freeze.assert_not_called()

    def test_dispatched_graphical_code_waits_for_new_selection_without_rescanning_page(self):
        app=self.prepared_capture_app()
        app._active_freeze=None
        app._native_source_generation=app._generation
        done=threading.Event();done.set()
        app._native_job=SimpleNamespace(frame_id='frame',generation=app._generation,done=done,
            result=SimpleNamespace(status='DISPATCHED_UNCONFIRMED',cleanup_status='CONFIRMED_ABSENT'))
        message='已交给微信，页面尚未确认，请查看识别结果。'
        with patch('screenqr.app.describe_job_result',return_value=(message,False)):
            app._poll_native_job()
        self.assertTrue(app.paused.get())
        self.assertTrue(app._handoff_idle)
        app.finder.attributes.assert_called_once_with('-topmost',False)
        app.finder.lower.assert_called_once()
        app._busy=False;app._next_scan=0
        app._tick()
        self.assertTrue(app._jobs.empty())
        app._set_status.assert_called_with(message,warning=False)
        app._resume_after_handoff()
        self.assertFalse(app.paused.get())
        self.assertFalse(app._handoff_idle)
        app.finder.attributes.assert_called_with('-topmost',True)

    def test_old_native_completion_does_not_release_or_overwrite_new_source(self):
        app=self.app()
        digest=self.digest(self.FIRST)
        app._native_outcome_digest=digest
        app._native_source_generation=app._generation-1
        app._native_auto_frame_id=app._native_auto_frame_key=None
        app._resume_scanning=Mock()
        app._refresh_native_action=Mock()
        done=threading.Event();done.set()
        app._native_job=SimpleNamespace(frame_id='old',done=done,
            result=SimpleNamespace(status='REJECTED',cleanup_status='NOT_NEEDED'))
        with patch('screenqr.app.describe_job_result',return_value=('无法识别：旧码',True)):
            app._poll_native_job()
        app._resume_scanning.assert_not_called()
        app._set_status.assert_not_called()
        self.assertEqual(app._wechat_outcomes[digest],('无法识别：旧码',True))
        self.read(app,self.SECOND)
        self.read(app,self.SECOND)
        app.request_freeze.assert_called_once()

    def test_failure_wording_is_not_rewritten_to_progress_and_outcome_cache_is_bounded(self):
        app=self.app()
        app.status=Mock()
        app._native_job=object()
        message='无法识别：原图显示不下，请缩小取景框'
        ScreenQRApp._set_status(app,message,warning=True)
        shown=app.status.configure.call_args.kwargs['text']
        self.assertTrue(shown.startswith('没扫出来'))
        self.assertNotIn('正在',shown)
        first=self.digest(self.FIRST)
        app._record_wechat_outcome(first,message,True)
        for index in range(520):
            app._record_wechat_outcome(self.digest(str(index)),'无法识别',True)
        self.assertEqual(len(app._wechat_outcomes),512)
        self.assertEqual(app._wechat_outcomes[first],(message,True))
        app._record_wechat_outcome(first,'已交给微信，页面尚未确认',False)
        self.assertEqual(app._wechat_outcomes[first],('已交给微信，页面尚未确认',False))

    def test_admission_limit_never_evicts_old_payloads(self):
        app = self.app()
        recorded = {self.digest('public-' + str(index)) for index in range(512)}
        app._wechat_seen_payloads = recorded.copy()
        self.read(app, self.FIRST)
        self.read(app, self.FIRST)
        app.request_freeze.assert_not_called()
        self.assertEqual(app._wechat_seen_payloads, recorded)

    def test_independently_decoded_capture_must_keep_the_same_unique_wechat_content(self):
        request = _FreezeRequest(1, str(uuid4()), (0, 0, 100, 100), (1,),
            1.0, 3.0, 1_000_000_000, expected_wechat_digest=self.digest(self.FIRST))
        with qrcode.make(self.FIRST).get_image().convert('RGB') as original:
            original_results = decode_image(original)
        with qrcode.make(self.SECOND).get_image().convert('RGB') as changed:
            changed_results = decode_image(changed)
        self.assertTrue(ScreenQRApp._capture_matches_automatic_intent(request, original_results))
        for results in (None, [], changed_results, original_results + changed_results,
                        [classify_payload('https://example.com/')]):
            with self.subTest(results=results):
                self.assertFalse(ScreenQRApp._capture_matches_automatic_intent(request, results))
        # A deliberate raw-graphical gesture has no fabricated decoded digest.
        self.assertTrue(ScreenQRApp._capture_matches_automatic_intent(
            replace(request, expected_wechat_digest=None), None))

    def test_changed_raw_capture_is_cancelled_before_preview_or_native_routing(self):
        app = self.app()
        topology = TopologyDescriptor(TOPOLOGY_KIND, (0, 0, 3200, 2000),
            (MonitorDescriptor('PUBLIC', (0, 0, 3200, 2000), 192, 192, True, 0),))
        with qrcode.make(self.SECOND).get_image().convert('RGB') as pixels:
            box = (100, 100, 100 + pixels.width, 100 + pixels.height)
            frame = FrozenFrame(str(uuid4()), 1, box, box, pixels.width, pixels.height,
                pixels.tobytes(), 1.01, 1.02, topology)
            changed_results = decode_image(pixels)
        app._active_freeze = _FreezeRequest(1, frame.frame_id, box, (1,), 1.0, 3.0,
            1_000_000_000, expected_wechat_digest=self.digest(self.FIRST))
        app._event_lock = threading.Lock()
        app._events = queue.Queue()
        app._events.put(('frozen', 1, frame.frame_id, frame, changed_results, 0.02))
        app._dropped_event_kinds = set()
        app._busy = True
        app._poll_native_job, app._freeze_matches = Mock(), Mock(return_value=True)
        app.finder.capture_bbox = Mock(return_value=box)
        app._show_frozen_frame, app._route_captured_target = Mock(), Mock()
        app._pending_freeze = app._pending_stage_verify = app._pending_image = None
        app.root = Mock()
        def resume():
            app._active_freeze = None
            # Prevent this pure seam from scheduling the next desktop capture.
            app.paused.set(True)
        app._resume_scanning = Mock(side_effect=resume)
        app._tick()
        app._resume_scanning.assert_called_once()
        app._show_frozen_frame.assert_not_called()
        app._route_captured_target.assert_not_called()
        app.request_freeze.assert_not_called()

    def test_missing_stage_is_not_layout_ready_even_before_focus_preparation(self):
        app = self.app()
        app._native_stage_focus_ready = False
        app._freeze_preview = app._freeze_image_label = app._freeze_white_stage = None
        self.assertEqual(app._auto_stage_readiness(SimpleNamespace(width=20, height=30)),
                         (False, None))

    def test_result_and_history_buttons_have_no_main_checkboxes_and_native_preview_has_no_controls(self):
        app = self.app()
        app.root, app.scale = Mock(), 1
        app._button, app._label = Mock(return_value=Mock()), Mock(return_value=Mock())
        app.finder.px=lambda value:value
        app.finder.FOOTER=112
        app.finder.winfo_width=Mock(return_value=420)
        constructors = ('Frame', 'Button', 'Label', 'Listbox', 'Text', 'Scrollbar')
        patches = [patch('screenqr.app.tk.' + name) for name in constructors]
        try:
            for constructor in patches: constructor.start()
            with patch('screenqr.app.tk.Checkbutton') as checkbox:
                app._build_panel()
                checkbox.assert_not_called()
            button_labels = [call.args[1] for call in app._button.call_args_list]
            self.assertEqual(button_labels,['在浏览器打开','复制','历史'])
        finally:
            for constructor in reversed(patches): constructor.stop()
        app._button.reset_mock()
        app.root.winfo_screenwidth.return_value = 3200
        app.root.winfo_screenheight.return_value = 2000
        app.finder.withdraw = Mock()
        app._freeze_stage_epoch = 0
        app._freeze_matches, app._own_top_hwnd = Mock(return_value=True), Mock(return_value=1)
        app._refresh_native_action = Mock()
        topology = TopologyDescriptor(TOPOLOGY_KIND, (0, 0, 3200, 2000),
            (MonitorDescriptor('PUBLIC', (0, 0, 3200, 2000), 192, 192, True, 0),))
        frame = FrozenFrame(str(uuid4()), 1, (100, 100, 102, 102), (100, 100, 102, 102),
            2, 2, b'\xff' * 12, 1.0, 1.01, topology)
        with patch('screenqr.app.tk.Toplevel') as top, patch('screenqr.app.tk.Frame'), \
                patch('screenqr.app.tk.Label'), patch('screenqr.app.tk.Checkbutton') as checkbox, \
                patch('screenqr.app.ImageTk.PhotoImage'), patch('screenqr.app._position'), \
                patch('screenqr.app.disable_own_stage_transitions'), \
                patch('screenqr.app.focus_own_stage') as focus:
            top.return_value.winfo_reqwidth.return_value = 500
            top.return_value.winfo_reqheight.return_value = 500
            app._show_frozen_frame(frame)
            checkbox.assert_not_called()
            focus.assert_not_called()  # Deferred until a mapped, stable idle observation.
            top.return_value.deiconify.assert_called_once()
        app.finder.withdraw.assert_not_called()
        self.assertFalse(app._finder_hidden_for_native)
        self.assertFalse(app._native_stage_focus_ready)
        app._button.assert_not_called()


if __name__ == '__main__':
    unittest.main()
