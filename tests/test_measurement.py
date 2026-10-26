"""计时严格区分取像/计算/界面；只允许指定公开样本，日志不含内容。"""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from screenqr.measurement import Measurement,sample_text
from screenqr.app import ScreenQRApp


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'measure.jsonl'
        self.run='0123456789ABCDEF'
        self.probe=Measurement(self.path,self.run)
        self.addCleanup(self.probe.close)

    def test_only_complete_public_markers_are_allowed(self):
        public=sample_text(self.run,12)
        self.assertTrue(self.probe.allows([SimpleNamespace(text=public)]))
        for text in ('PRIVATE-CONTENT',public+'x',sample_text('1123456789ABCDEF',12)):
            self.assertFalse(self.probe.allows([SimpleNamespace(text=text)]))
        self.assertTrue(self.probe.allows([]))

    def test_exact_clock_intervals_and_log_contains_no_payload_or_digest(self):
        payload=sample_text(self.run,12)
        with patch('screenqr.measurement.time.monotonic_ns',return_value=40_000_000):
            self.probe.record([SimpleNamespace(text=payload,format='QR Code')],(1_000_000,3_000_000,13_000_000),5)
        rows=[json.loads(line) for line in self.path.read_text().splitlines()]
        row=rows[-1]
        self.assertTrue(row['correct']); self.assertEqual(row['case'],12)
        self.assertEqual((row['capture_ms'],row['decode_ms'],row['product_ms']),(2,10,39))
        text=self.path.read_text()
        self.assertNotIn(payload,text); self.assertNotIn(self.run,text)

    def test_multiple_or_wrong_format_is_not_a_correct_result(self):
        instance=SimpleNamespace(text=sample_text(self.run,12),format='Code 39')
        self.probe.record([instance],(1,2,3),1)
        self.probe.record([instance,instance],(1,2,3),1)
        rows=[json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertFalse(rows[-1]['correct']); self.assertFalse(rows[-2]['correct'])

    def test_opt_in_measurement_cannot_dispatch_browser_or_native_input(self):
        app=ScreenQRApp.__new__(ScreenQRApp)
        app._measurement=self.probe
        app._opening=True
        with patch('screenqr.app.webbrowser.open') as browser:
            app._launch_browser('https://example.com/')
            browser.assert_not_called()
        self.assertFalse(app._opening)
        self.assertEqual(app.request_freeze(native_entered_ns=1),'break')


if __name__=='__main__': unittest.main()
