"""Click/worker/UI browser grant boundaries; no real desktop or URL opens."""
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import qrcode

from screenqr.app import ScreenQRApp, _Job
import test_automatic_wechat_detection as helpers


class FinalConfirmationFlowTests(unittest.TestCase):
    URL='https://example.com/confirm-final'

    def app(self):
        app=helpers.AutomaticWeChatDetectionTests().app()
        app.root=Mock()
        app.scale=1
        app.paused.set(True)
        app._event_lock=threading.Lock()
        app._events=queue.Queue()
        app._jobs=queue.Queue()
        app._stop=threading.Event()
        app._dropped_event_kinds=set()
        app._busy=False
        app._active_freeze=app._frozen_frame=None
        app._pending_freeze=app._pending_stage_verify=app._pending_image=None
        app._poll_native_job=Mock()
        helpers.AutomaticWeChatDetectionTests().read(app,self.URL)
        return app

    def test_click_only_queues_same_candidate_for_worker_recapture(self):
        app=self.app()
        candidate=app._candidate
        self.assertIsNotNone(candidate)
        app.open_selected()
        app._tick()
        job=app._jobs.get_nowait()
        self.assertIs(job.confirm,candidate)
        self.assertEqual(job.generation,app._generation)
        self.assertEqual(job.bbox,candidate.source_token[2])
        app._open_url.assert_not_called()

    def test_tiny_symbol_preview_is_legible_without_changing_source_pixels(self):
        app=ScreenQRApp.__new__(ScreenQRApp)
        app.scale=2
        qr=qrcode.QRCode(box_size=1,border=4)
        qr.add_data(self.URL)
        qr.make(fit=True)
        with qr.make_image().convert('RGB') as image:
            original=image.tobytes()
            observation=app._observe_image(image,(1,'screen',(0,0,image.width,image.height)),lambda:False)
            self.assertIsNotNone(observation[2])
            self.assertGreaterEqual(min(observation[3][0]),96)
            self.assertEqual(image.tobytes(),original)

    def test_actual_worker_recaptures_and_decodes_before_issuing_confirmed_event(self):
        app=self.app()
        candidate=app._candidate
        app._jobs.put(_Job(app._generation,bbox=candidate.source_token[2],confirm=candidate))
        events=[]
        def posted(event):
            events.append(event)
            app._stop.set()
        app._post_event=posted
        with Image.new('RGB',(1000,500),'white') as pixels:
            with qrcode.make(self.URL).get_image().convert('RGB') as qr:
                pixels.paste(qr,(10,10))
            with patch('screenqr.app.capture_region',return_value=pixels) as capture:
                app._work()
        # Decode is followed by a second current-pixel check, so a slow decode
        # cannot authorize content that disappeared while it was computing.
        self.assertEqual(capture.call_count,2)
        for call in capture.call_args_list:
            self.assertEqual(call.args,(candidate.source_token[2],))
        self.assertEqual(len(events),1)
        self.assertEqual(events[0][:4],('confirmed',app._generation,candidate,True))
        self.assertIsInstance(events[0][4],int)
        app._open_url.assert_not_called()

    def test_matching_confirmed_event_grants_url_but_change_or_negative_result_never_does(self):
        for changed in ('none','generation','geometry','candidate','not-inflight','failed','aged-evidence'):
            with self.subTest(changed=changed):
                app=self.app()
                candidate=app._candidate
                app._confirm_inflight=candidate
                event_generation=app._generation
                if changed=='generation': app._generation+=1
                elif changed=='geometry': app.finder.capture_bbox.return_value=(1,0,1001,500)
                elif changed=='candidate': app._candidate=None
                elif changed=='not-inflight': app._confirm_inflight=None
                evidence_time=0 if changed=='aged-evidence' else 1_000_000_000
                app._events.put(('confirmed',event_generation,candidate,changed!='failed',evidence_time))
                with patch('screenqr.app.time.monotonic_ns',return_value=1_000_000_001):
                    app._tick()
                if changed=='none':
                    app._open_url.assert_called_once_with(self.URL)
                    self.assertEqual(app._browser_grant,candidate)
                else:
                    app._open_url.assert_not_called()
                    self.assertIsNone(app._browser_grant)

    def test_browser_boundary_requires_and_consumes_one_explicit_recheck_grant(self):
        app=self.app()
        app._post_event=Mock()
        with patch('screenqr.app.webbrowser.open',return_value=True) as browser, \
                patch('screenqr.app.threading.Thread') as thread:
            thread.side_effect=lambda *,target,**kwargs:SimpleNamespace(start=target)
            ScreenQRApp._open_url(app,self.URL)
            browser.assert_not_called()
            grant=app._candidate
            app._browser_grant=grant
            ScreenQRApp._open_url(app,self.URL)
            browser.assert_called_once_with(self.URL,new=2)
            self.assertIsNone(app._browser_grant)
            app._opening=False
            ScreenQRApp._open_url(app,self.URL)
            browser.assert_called_once()

    def test_new_empty_or_repeated_payload_instances_remove_existing_confirmation(self):
        helper=helpers.AutomaticWeChatDetectionTests()
        for texts in ([],[self.URL,self.URL],[self.URL,'https://example.com/second']):
            with self.subTest(texts=texts):
                app=self.app()
                app.open_selected()
                observation=helper.observation(app,texts)
                app._accept_results(observation[0],None,0.02,observation=observation)
                self.assertIsNone(app._candidate)
                self.assertIsNone(app._pending_confirm)
                self.assertIsNone(app._confirm_inflight)
                app._open_url.assert_not_called()


if __name__=='__main__':
    unittest.main()
