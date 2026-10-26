"""真实画面观察与选择状态、历史打开门禁；外部打开被明确截断。"""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import qrcode

from screenqr.app import ScreenQRApp
from screenqr.core import classify_payload
from screenqr.history import HistoryStore
import test_automatic_wechat_detection as helpers


class P2FlowTests(unittest.TestCase):
    def app(self):
        app=helpers.AutomaticWeChatDetectionTests().app()
        app.scale=1
        app._selected=ScreenQRApp._selected.__get__(app)
        app._multi_list=Mock()
        app._instances=()
        app._choice_token=None
        app._selected_instance=None
        return app

    def observation(self, app, texts):
        return helpers.AutomaticWeChatDetectionTests.observation(app,texts)

    def test_multi_list_select_does_not_open_or_grant_old_confirmation(self):
        app=self.app()
        texts=['https://example.com/a','https://example.com/b']
        observation=self.observation(app,texts)
        app._accept_results(observation[0],None,.1,observation)
        self.assertIsNone(app._candidate)
        self.assertIsNone(app._selected())
        app._multi_list.curselection.return_value=(1,)
        app._choose_instance()
        app._open_url.assert_not_called()
        self.assertIsNone(app._candidate)
        selected=app._selected_instance
        with Image.new('RGB',(1000,500),'white') as image:
            for index,text in enumerate(texts):
                with qrcode.make(text).get_image().convert('RGB') as code: image.paste(code,(10+index*500,10))
            observation=app._observe_image(image,(1,'screen',app.finder.capture_bbox()),lambda:False,selected=selected)
        # 测试不建立 Tk 位图；候选与真实像素来自实际解码。
        observation=(*observation[:3],None,observation[4])
        app._accept_results(observation[0],None,.1,observation)
        self.assertEqual(app._candidate.result.text,selected.text)
        app.open_selected()
        self.assertEqual(app._pending_confirm,app._candidate)
        app._open_url.assert_not_called()
        empty=self.observation(app,[])
        app._accept_results(empty[0],None,.1,empty)
        self.assertIsNone(app._selected_instance)
        self.assertIsNone(app._pending_confirm)

    def test_picked_wechat_in_multiple_still_never_enters_native_input(self):
        app=self.app()
        observation=self.observation(app,['https://weixin.qq.com/g/PUBLIC','https://example.com/'])
        app._accept_results(observation[0],None,.1,observation)
        app._selected_instance=next(i for i in app._instances if 'weixin' in i.text)
        app._accept_results(observation[0],None,.1,observation)
        app.request_freeze.assert_not_called()
        app._open_url.assert_not_called()
        self.assertIsNone(app._candidate)

    def test_history_browser_action_refetches_entry_and_rechecks_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.app(); app._history=HistoryStore(Path(directory)/'history.json')
            app._launch_browser=Mock()
            for text in ('file:///C:/x','javascript:alert(1)','weixin://dl/scan'):
                app._history.record(classify_payload(text))
                app.open_history_entry(app._history.entries[0].id)
            app.open_history_entry('missing')
            app._launch_browser.assert_not_called()
            app._history.record(classify_payload('https://example.com/history'))
            entry=app._history.entries[0]
            app.open_history_entry(entry.id)
            app._launch_browser.assert_called_once_with('https://example.com/history',history=True)
            app._opening=False; app._history.clear(); app.open_history_entry(entry.id)
            app._launch_browser.assert_called_once()

    def test_opened_browser_waits_for_deliberate_next_selection(self):
        app=self.app(); app.paused.set(False)
        app._complete_browser_open(True,app._generation,False,False)
        self.assertTrue(app.paused.get())
        self.assertTrue(app._handoff_idle)
        app._resume_after_handoff()
        self.assertFalse(app.paused.get())
        self.assertFalse(app._handoff_idle)

    def test_late_browser_event_cannot_pause_new_target_and_failure_restores_state(self):
        app=self.app(); app.paused.set(False)
        app._complete_browser_open(True,app._generation-1,False,False)
        self.assertFalse(app.paused.get())
        app.paused.set(True)
        app._complete_browser_open(False,app._generation,False,False)
        self.assertFalse(app.paused.get())


if __name__=='__main__': unittest.main()
