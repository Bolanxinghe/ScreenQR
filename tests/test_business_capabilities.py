"""Real encoded format cases plus native capability gate; never business login/pay."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import qrcode
from screenqr.core import ScanResult, browser_url_for_result, classify_payload, decode_image
from screenqr.native_job import NativeJob, describe_job_result
from test_native_port import fixture


class BusinessCapabilityTests(unittest.TestCase):
    def test_generated_format_samples_decode_preserve_content_and_route(self):
        cases = json.loads((Path(__file__).parent/'fixtures/qr_payload_cases.json').read_text(encoding='utf-8'))
        for case in cases:
            with self.subTest(case=case['id']):
                with qrcode.make(case['payload']).get_image().convert('RGB') as image:
                    result, = decode_image(image)
                self.assertEqual((result.text,result.kind),(case['payload'],case['kind']))
                self.assertIs(browser_url_for_result(result) is not None,case['browser'])

    def test_payment_and_wechat_protocol_cannot_be_forged_into_browser_actions(self):
        for payload in ('weixin://wxpay/bizpayurl?pr=PUBLIC_NOT_AN_ORDER',
                        'weixin://dl/business/?t=PUBLIC_NOT_ISSUED'):
            result = ScanResult(payload,'QR Code','url','https://example.com/','forged')
            self.assertIsNone(browser_url_for_result(result))
        for payload in ('weixin://user@wxpay/bizpayurl?pr=x',
                        'weixin://wxpay:123/bizpayurl?pr=x',
                        'weixin://wxpay/bizpayurl?pr=x\n',
                        'weixin://wxpay\\other/bizpayurl'):
            self.assertEqual(classify_payload(payload).kind,'text')

    def test_known_native_payment_stops_before_any_port_or_observer_is_constructed(self):
        for payload in ('weixin://wxpay/bizpayurl?pr=PUBLIC_NOT_AN_ORDER',
                        'weixin://pay.weixin.qq.com/bizpayurl/up?pr=PUBLIC_NOT_AN_ORDER'):
            with self.subTest(payload=payload):
                frame,mapping,_,_ = fixture()
                port,observer = Mock(),Mock()
                job = NativeJob(frame,mapping,entered_ns=1_000_000_000,
                    single_target_confirmed=True,clock=lambda:1_100_000_000,
                    port_factory=port,observer_factory=observer)
                barcode = SimpleNamespace(valid=True,text=payload)
                with patch('screenqr.native_job.read_qr_instances',return_value=[barcode]):
                    job.run()
                self.assertEqual(job.result.status,'UNSUPPORTED_BUSINESS')
                self.assertFalse(job.result.dispatch_attempted)
                self.assertTrue(job.result.retry_safe_before_input)
                port.assert_not_called()
                observer.assert_not_called()
                text,warning = describe_job_result(job.result)
                self.assertTrue(text.startswith('无法识别'))
                self.assertIn('支付码',text)
                self.assertTrue(warning)


if __name__ == '__main__':
    unittest.main()
