"""Routing and explicit browser confirmation; no external inputs."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from screenqr.app import ScreenQRApp
from screenqr.core import classify_payload
import test_release_handoff as helpers
from test_release_handoff import Value
import test_automatic_wechat_detection as automatic_helpers


class SimpleFlowTests(unittest.TestCase):
    def test_release_routes_web_text_multiple_and_payment_without_native_preview(self):
        for texts in (['https://example.com/'],['hello'],
                      ['https://example.com/one','https://example.com/two'],
                      ['weixin://wxpay/bizpayurl?pr=PUBLIC_NOT_AN_ORDER']):
            app=ScreenQRApp.__new__(ScreenQRApp)
            app._resume_scanning=Mock()
            app._accept_results,app._set_status=Mock(),Mock()
            results=[classify_payload(t) for t in texts]
            self.assertTrue(app._route_captured_target(results,0.05))
            app._resume_scanning.assert_called_once()
            # A resumed generation needs a fresh located observation; an old
            # frozen result must not become a browser action or confirmation.
            app._accept_results.assert_not_called()
        app=ScreenQRApp.__new__(ScreenQRApp)
        app._resume_scanning=Mock()
        self.assertFalse(app._route_captured_target([],0.05))
        self.assertFalse(app._route_captured_target([classify_payload('weixin://dl/scan')],0.05))
        app._resume_scanning.assert_not_called()

    def test_selection_events_never_substitute_for_explicit_confirmation(self):
        app=ScreenQRApp.__new__(ScreenQRApp)
        app._selected=lambda:classify_payload('https://example.com/selected')
        app._refresh_actions=Mock()
        app.open_selected=Mock()
        app._selection_changed()
        app.open_selected.assert_not_called()
        app._selection_changed(SimpleNamespace(widget='user selection'))
        app.open_selected.assert_not_called()
        self.assertEqual(app._refresh_actions.call_count,2)

    def test_pending_automatic_request_cannot_be_replaced_with_manual_fresh_deadline(self):
        app=helpers.ReleaseHandoffTests().app()
        app._poll_native_job=Mock()
        app.describe_frozen_stage=Mock()
        app.request_native_handoff()
        app.describe_frozen_stage.assert_not_called()
        app.request_freeze=Mock()
        app.request_wechat_scan()
        app.request_freeze.assert_not_called()

    def test_optional_scan_shortcut_preserves_identity_without_total_preparation_timeout(self):
        app=helpers.ReleaseHandoffTests().app()
        app.wechat_on_release.set(False)
        request=replace(app._auto_handoff_request,manual_scan=True)
        app._auto_handoff_request=request
        app.request_native_handoff=Mock()
        with patch('screenqr.app.time.monotonic_ns',return_value=110_000_000_000):
            app._start_auto_handoff(request)
        app.request_native_handoff.assert_called_once_with(auto_request=request)
        self.assertEqual(request.native_entered_ns,10_000_000_000)
        self.assertIsNone(request.deadline_monotonic)

    def test_confirmation_queues_fresh_worker_check_and_never_opens_directly(self):
        helper=automatic_helpers.AutomaticWeChatDetectionTests()
        app=helper.app()
        helper.read(app,'https://example.com/selected')
        candidate=app._candidate
        app.open_selected()
        self.assertIs(app._pending_confirm,candidate)
        app._open_url.assert_not_called()
        app._pending_confirm=None
        app._generation+=1
        app.open_selected()
        self.assertIsNone(app._candidate)
        self.assertIsNone(app._pending_confirm)
        app._open_url.assert_not_called()


if __name__=='__main__':
    unittest.main()
