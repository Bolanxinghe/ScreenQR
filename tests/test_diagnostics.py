"""Opt-in diagnostic privacy and isolation; no native queries or GUI."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from screenqr.app import ScreenQRApp
from screenqr.diagnostics import NativeDiagnostics
from screenqr.native_job import NativeJobResult

class DiagnosticTests(unittest.TestCase):
    def test_only_fixed_codes_survive_and_one_result_replaces_previous(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'last.json'
            writer=NativeDiagnostics(path)
            writer.preparation('own_stage_foreground_rejected')
            result=NativeJobResult('private-frame-id',1,'private-stage-id',2,
                'CONTENT_UNPROVEN','https://private.example/secret',
                diagnostics=(('last_operation','selection_evidence'),
                    ('last_selection_error_code','C:/private/chat.png'),
                    ('private_extra','SECRET QR TEXT')))
            writer.result(result)
            text=path.read_text(encoding='utf-8')
            data=json.loads(text)
            self.assertEqual(data['status'],'CONTENT_UNPROVEN')
            self.assertEqual(data['reason'],'other_internal_code')
            self.assertEqual(data['diagnostics'],{'last_operation':'selection_evidence',
                'last_selection_error_code':'other_internal_code'})
            self.assertFalse(data['business_success'])
            for secret in ('private','secret','SECRET','QR TEXT','own_stage_foreground_rejected'):
                self.assertNotIn(secret,text)
            self.assertEqual(list(Path(folder).iterdir()),[path])

    def test_unwritable_destination_does_not_raise(self):
        with tempfile.TemporaryDirectory() as folder:
            NativeDiagnostics(Path(folder)/'missing'/'last.json').preparation('own_stage_not_ready')
            self.assertEqual(list(Path(folder).iterdir()),[])

    def test_optional_sink_failure_cannot_change_product_state(self):
        app=ScreenQRApp.__new__(ScreenQRApp)
        marker=object();app._candidate=marker
        app._diagnostics=Mock()
        app._diagnostics.result.side_effect=RuntimeError('storage unavailable')
        app._native_diagnostic(result=object())
        self.assertIs(app._candidate,marker)

if __name__=='__main__':unittest.main()
