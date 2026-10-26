"""只读报告和自查的真实文件检查；不连接网络或读取用户数据。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from screenqr.reporting import save_report
from screenqr.release import executable_checksum


class ReportingTests(unittest.TestCase):
    def test_report_writes_expected_data_and_does_not_overwrite(self):
        public=dict(version='PUBLIC',screen_captured=False,input_sent=False,network_requests=0)
        with tempfile.TemporaryDirectory() as directory, patch('screenqr.reporting.collect',return_value=public) as probe:
            path=Path(directory)/'report.json'
            self.assertEqual(save_report(path,own_hwnd=42),public)
            probe.assert_called_once_with(42)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),public)
            with self.assertRaises(FileExistsError): save_report(path)

    def test_executable_checksum_is_actual_file_and_source_run_does_not_fake_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'public.exe'; path.write_bytes(b'PUBLIC-NOT-EXECUTABLE')
            with patch('screenqr.release.sys.frozen',True,create=True), patch('screenqr.release.sys.executable',str(path)):
                self.assertEqual(executable_checksum(),hashlib.sha256(path.read_bytes()).hexdigest())
            with patch('screenqr.release.sys.frozen',False,create=True): self.assertIsNone(executable_checksum())


if __name__=='__main__': unittest.main()
