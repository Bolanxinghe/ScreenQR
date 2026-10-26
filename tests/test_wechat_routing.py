"""Local content-routing checks. No Tk root, browser, native UI or network."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from screenqr.app import ScreenQRApp
from screenqr.core import (ScanResult, browser_url_for_result, classify_payload,
                           is_wechat_login_confirm)
import test_automatic_wechat_detection as automatic_helpers


class WeChatRoutingTests(unittest.TestCase):
    LOGIN = "https://open.weixin.qq.com/connect/confirm?uuid=PUBLIC_SYNTHETIC_UUID_0001"

    def test_personal_card_route_never_becomes_a_browser_action(self):
        for text in (
            'https://u.wechat.com/PUBLIC_NOT_ISSUED?s=3',
            'HTTP://U.WECHAT.COM.:80/PUBLIC_Test-1?s=3',
        ):
            with self.subTest(text=text):
                result = classify_payload(text)
                self.assertEqual(result.kind, 'wechat')
                self.assertEqual(result.text, text)
                self.assertIsNone(browser_url_for_result(result))
                self.assertIsNone(browser_url_for_result(ScanResult(text, 'QR Code', 'url', text, 'forged')))
        for text in (
            'https://u.wechat.com/',
            'https://u.wechat.com/PUBLIC/other',
            'https://u.wechat.com.evil.test/PUBLIC?s=3',
            'https://notu.wechat.com/PUBLIC?s=3',
            'https://example.com/PUBLIC?s=3',
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_payload(text).kind, 'url')

    def test_personal_card_reaches_native_candidate_without_browser_confirm(self):
        helper = automatic_helpers.AutomaticWeChatDetectionTests()
        app = helper.app()
        text = 'https://u.wechat.com/PUBLIC_NOT_ISSUED?s=3'
        for _ in range(3):
            helper.read(app, text)
        app._open_url.assert_not_called()
        self.assertIsNone(app._candidate)
        app.request_freeze.assert_called_once()

    def test_official_native_payment_host_is_restricted_before_native_or_browser_action(self):
        for text in (
            'weixin://pay.weixin.qq.com/bizpayurl/up?pr=PUBLIC_NOT_AN_ORDER&groupid=00',
            'WEIXIN://PAY.WEIXIN.QQ.COM/bizpayurl/up?pr=PUBLIC_NOT_AN_ORDER',
        ):
            with self.subTest(text=text):
                result = classify_payload(text)
                self.assertEqual(result.kind, 'wechat_payment')
                self.assertEqual(result.text, text)
                self.assertIsNone(browser_url_for_result(result))
                self.assertIsNone(browser_url_for_result(ScanResult(text, 'QR Code', 'url', text, 'forged')))
        for text in (
            'weixin://pay.weixin.qq.com.evil.test/bizpayurl/up?pr=PUBLIC',
            'weixin://pay.weixin.qq.com/bizpayurl/other?pr=PUBLIC',
        ):
            self.assertNotEqual(classify_payload(text).kind, 'wechat_payment')

    def test_login_confirm_routes_original_payload_to_wechat_only(self):
        for text in (
            self.LOGIN,
            "HTTP://OPEN.WEIXIN.QQ.COM:80/connect/confirm?uuid=Public_Test-0001",
            "https://OPEN.WEIXIN.QQ.COM.:443/connect/confirm?foo=1&uuid=Public%5FTest%2D0001&next=a%2Fb#fragment",
            "https://open.weixin.qq.com/connect/confirm?uuid=public-test&uuid_extra=other",
        ):
            with self.subTest(text=text):
                result = classify_payload(text)
                self.assertTrue(is_wechat_login_confirm(text))
                self.assertEqual(result.kind, "wechat")
                self.assertEqual(result.text, text)
                self.assertIsNone(result.open_url)
                self.assertIsNone(browser_url_for_result(result))
                self.assertIn("需微信处理", result.summary)

    def test_malformed_login_confirm_never_becomes_native_or_browser_action(self):
        for suffix in (
            "", "?", "?uuid", "?uuid=", "?UUID=public-test",
            "?uuid=one&uuid=two", "?uuid=one&%75uid=two", "?uuid=one&uuid=",
            "?uuid=bad+token", "?uuid=bad%20token", "?uuid=%00",
            "?uuid=%E9%9D%9EASCII", "?uuid=%FF", "?uuid=public%2Ftoken",
            "?uuid=bad%token", "?uuid=one&extra=%GG", "?uuid=" + "x" * 257,
        ):
            text = "https://open.weixin.qq.com/connect/confirm" + suffix
            with self.subTest(suffix=suffix):
                result = classify_payload(text)
                self.assertFalse(is_wechat_login_confirm(text))
                self.assertEqual(result.kind, "text")
                self.assertEqual(result.text, text)
                self.assertIsNone(result.open_url)
                self.assertIsNone(browser_url_for_result(result))
                self.assertEqual(result.summary, "微信登录二维码格式异常 · 无法识别")

    def test_login_confirm_uses_only_default_scheme_port(self):
        for text in (
            "https://open.weixin.qq.com:80/connect/confirm?uuid=public-test",
            "http://open.weixin.qq.com:443/connect/confirm?uuid=public-test",
            "https://open.weixin.qq.com:8443/connect/confirm?uuid=public-test",
            "https://open.weixin.qq.com:bad/connect/confirm?uuid=public-test",
            "https://open.weixin.qq.com:/connect/confirm?uuid=public-test",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_wechat_login_confirm(text))
                result = classify_payload(text)
                self.assertEqual(result.kind, "text")
                self.assertIsNone(result.open_url)
                self.assertIsNone(browser_url_for_result(result))

    def test_login_policy_does_not_capture_lookalikes_or_other_routes(self):
        for text in (
            "https://open.weixin.qq.com.evil.test/connect/confirm?uuid=public-test",
            "https://notopen.weixin.qq.com/connect/confirm?uuid=public-test",
            "https://open-weixin.qq.com/connect/confirm?uuid=public-test",
            "https://example.com/connect/confirm?uuid=public-test",
            "https://open.weixin.qq.com/connect/Confirm?uuid=public-test",
            "https://open.weixin.qq.com/connect/confirm/?uuid=public-test",
            "https://open.weixin.qq.com/connect/%63onfirm?uuid=public-test",
            "https://open.weixin.qq.com/connect/qrconnect?appid=public-test",
            "https://open.weixin.qq.com/connect/oauth2/authorize?appid=public-test",
            "https://mp.weixin.qq.com/s/public-test",
            "https://qq.com/connect/confirm?uuid=public-test",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_wechat_login_confirm(text))
                result = classify_payload(text)
                self.assertEqual(result.kind, "url")
                self.assertEqual(browser_url_for_result(result), result.open_url)
        self.assertFalse(is_wechat_login_confirm(None))

    def test_login_confirm_cannot_bypass_browser_boundary_with_forged_result(self):
        for text in (self.LOGIN, "https://open.weixin.qq.com/connect/confirm?uuid="):
            forged = ScanResult(text, "QR Code", "url", text, "forged")
            self.assertIsNone(browser_url_for_result(forged))
            self.assertIsNone(ScreenQRApp._safe_url(forged))
        app = ScreenQRApp.__new__(ScreenQRApp)
        app._closed = app._closing = False
        with patch("screenqr.app.webbrowser.open") as browser:
            app._open_url(self.LOGIN)
            app._open_url("https://open.weixin.qq.com/connect/confirm?uuid=")
            browser.assert_not_called()

    def test_real_encoded_login_format_is_native_without_business_success_claim(self):
        import qrcode
        from screenqr.core import decode_image
        image = qrcode.make(self.LOGIN).get_image().convert("RGB")
        results = decode_image(image)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].text, self.LOGIN)
        self.assertEqual(results[0].kind, "wechat")
        self.assertIsNone(browser_url_for_result(results[0]))

    def test_observed_route_is_not_a_browser_action(self):
        for text in (
            "http://weixin.qq.com/x/test-token",
            "https://weixin.qq.com/x/test-token?q=1#fragment",
            "HTTP://WEIXIN.QQ.COM:80/x/test-token",
            "https://WEIXIN.QQ.COM.:443/x/test-token",
            "https://weixin.qq.com/g/public-test-token",
            "http://WEIXIN.QQ.COM.:80/g/another-test-token?x=1#fragment",
        ):
            with self.subTest(text=text):
                result = classify_payload(text)
                self.assertEqual(result.kind, "wechat")
                self.assertEqual(result.text, text)
                self.assertIsNone(result.open_url)
                self.assertIn("需微信处理", result.summary)
                self.assertIsNone(browser_url_for_result(result))

    def test_other_routes_articles_and_lookalike_hosts_remain_web_urls(self):
        for text in (
            "https://mp.weixin.qq.com/s/test-article",
            "https://weixin.qq.com/",
            "https://weixin.qq.com/x",
            "https://weixin.qq.com/x/",
            "https://weixin.qq.com/g/",
            "https://weixin.qq.com/G/public-test-token",
            "https://example.com/g/public-test-token",
            "https://weixin.qq.com.evil.test/g/public-test-token",
            "https://weixin.qq.com/X/test-token",
            "https://weixin.qq.com/x-other/test-token",
            "https://weixin.qq.com/%78/test-token",
            "https://weixin.qq.com.evil.test/x/test-token",
            "https://notweixin.qq.com/x/test-token",
            "https://sub.weixin.qq.com/x/test-token",
            "https://example.com/x/test-token",
        ):
            with self.subTest(text=text):
                result = classify_payload(text)
                self.assertEqual(result.kind, "url")
                self.assertEqual(result.text, text)
                self.assertEqual(browser_url_for_result(result), result.open_url)

    def test_ambiguous_inputs_are_still_unopenable(self):
        for text in (
            "https://weixin.qq.com@evil.test/x/test-token",
            "https://weixin.qq.com\\@evil.test/x/test-token",
            "https://%77eixin.qq.com/x/test-token",
            "https://weixin.qq.com/x/test-token\n",
            "https://weixin.qq.com:bad/x/test-token",
            "weixin://dl/scan",
        ):
            with self.subTest(text=text):
                result = classify_payload(text)
                self.assertIsNone(result.open_url)
                self.assertIsNone(browser_url_for_result(result))

    def test_hand_constructed_results_cannot_bypass_payload_routing(self):
        wechat = "http://weixin.qq.com/x/test-token"
        web = "https://example.com/"
        for result in (
            ScanResult(wechat, "QR Code", "url", wechat, "forged"),
            ScanResult(wechat, "QR Code", "url", web, "forged"),
            ScanResult(web, "QR Code", "url", wechat, "forged"),
            ScanResult(web, "QR Code", "url", "https://elsewhere.test/", "forged"),
            ScanResult("javascript:alert(1)", "QR Code", "url", web, "forged"),
            ScanResult(web, "QR Code", "url", 123, "forged"),
        ):
            with self.subTest(result=result):
                self.assertIsNone(browser_url_for_result(result))
                self.assertIsNone(ScreenQRApp._safe_url(result))
        equivalent = ScanResult("HTTPS://EXAMPLE.COM/", "QR Code", "url", "https://example.com/", "")
        self.assertEqual(browser_url_for_result(equivalent), "https://example.com/")

    def test_forged_wechat_result_cannot_create_a_browser_confirmation_or_open(self):
        text = "http://weixin.qq.com/x/test-token"
        forged = ScanResult(text, "QR Code", "url", text, "forged")
        helper=automatic_helpers.AutomaticWeChatDetectionTests()
        app=helper.app()
        app._selected = lambda: forged
        app._open_url = Mock()
        app.open_selected()
        app._open_url.assert_not_called()
        observation=helper.observation(app,[text])
        observation=([forged],*observation[1:])
        app._accept_results([forged],None,0.01,observation=observation)
        app._accept_results([forged],None,0.01,observation=observation)
        app._open_url.assert_not_called()
        self.assertEqual((app._automatic_candidate, app._candidate_count), (None, 0))
        self.assertEqual(app._auto_seen, set())

    def test_ordinary_web_reads_never_open_without_confirm_and_fresh_recheck(self):
        result = classify_payload("https://example.com/ordinary")
        helper=automatic_helpers.AutomaticWeChatDetectionTests()
        app=helper.app()
        for _ in range(3): helper.read(app,result.text)
        app._open_url.assert_not_called()
        self.assertEqual(app._candidate.open_url,result.open_url)
        app.open_selected()
        self.assertIs(app._pending_confirm,app._candidate)
        app._open_url.assert_not_called()

    def test_copy_shortcut_keeps_original_without_a_visible_copy_button(self):
        result = classify_payload("http://weixin.qq.com/x/test-token")
        helper=automatic_helpers.AutomaticWeChatDetectionTests()
        app=helper.app()
        app._selected = lambda: result
        app.root=Mock()
        app.copy_selected()
        app.root.clipboard_clear.assert_called_once()
        app.root.clipboard_append.assert_called_once_with(result.text)
        app._open_url.assert_not_called()

    def test_final_browser_boundary_also_refuses_known_wechat_route(self):
        app = ScreenQRApp.__new__(ScreenQRApp)
        app._closed=app._closing=False
        with patch("screenqr.app.webbrowser.open") as browser:
            app._open_url("http://weixin.qq.com/x/test-token")
            app._open_url("https://weixin.qq.com/g/public-test-token")
            browser.assert_not_called()

    def test_real_encoded_group_url_is_native_and_forged_browser_action_is_refused(self):
        import qrcode
        from screenqr.core import decode_image
        text = "https://weixin.qq.com/g/public-generated-test"
        image = qrcode.make(text).get_image().convert("RGB")
        results = decode_image(image)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "wechat")
        self.assertEqual(results[0].text, text)
        forged = ScanResult(text, "QR Code", "url", text, "forged")
        self.assertIsNone(browser_url_for_result(forged))
        app = automatic_helpers.AutomaticWeChatDetectionTests().app()
        app._selected = lambda: forged
        app._open_url = Mock()
        app.open_selected()
        app._open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
