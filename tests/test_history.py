"""本机记录的真实临时文件检查；没有联网或用户数据。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from screenqr.core import classify_payload, browser_url_for_result
from screenqr.history import HistoryStore, MAX_ENTRIES


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'nested' / 'history.json'
        self.store = HistoryStore(self.path)

    def test_restart_keeps_full_text_time_and_format_locally(self):
        result = classify_payload('WIFI:T:WPA;S:公开样本;P:NOT-A-REAL-PASSWORD;;')
        self.assertTrue(self.store.record(result))
        again = HistoryStore(self.path)
        self.assertEqual(again.entries[0].result, result)
        self.assertEqual(again.entries[0].time, self.store.entries[0].time)
        self.assertNotIn(result.text, repr(again.entries[0]))

    def test_disable_persists_stops_new_writes_and_clear_really_removes_contents(self):
        self.store.record(classify_payload('PUBLIC-HISTORY'))
        self.assertTrue(self.store.set_enabled(False))
        before = self.path.read_bytes()
        self.assertFalse(self.store.record(classify_payload('MUST-NOT-BE-SAVED')))
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(HistoryStore(self.path).enabled)
        self.assertTrue(self.store.clear())
        self.assertEqual(HistoryStore(self.path).entries, ())
        self.assertNotIn('PUBLIC-HISTORY', self.path.read_text())

    def test_repeat_frames_do_not_write_or_grow_records(self):
        result = classify_payload('https://example.com/public')
        self.store.record(result)
        with patch.object(self.store, '_save', side_effect=AssertionError('repeat disk write')):
            self.assertFalse(self.store.record(result))
        self.assertEqual(len(self.store.entries), 1)

    def test_record_count_is_bounded_and_latest_is_first(self):
        for index in range(MAX_ENTRIES + 3):
            self.store.record(classify_payload(f'PUBLIC-{index}'))
        loaded = HistoryStore(self.path)
        self.assertEqual(len(loaded.entries), MAX_ENTRIES)
        self.assertEqual(loaded.entries[0].text, f'PUBLIC-{MAX_ENTRIES + 2}')

    def test_payment_is_not_saved_and_disk_does_not_grant_a_url(self):
        self.assertFalse(self.store.record(classify_payload('weixin://wxpay/bizpayurl?pr=PUBLIC')))
        self.store.record(classify_payload('file:///C:/public.txt'))
        entry = HistoryStore(self.path).entries[0]
        self.assertIsNone(browser_url_for_result(entry.result))
        self.assertIsNone(self.store.get('unknown'))

    def test_corrupt_or_invented_action_fields_disable_recording_without_crashing(self):
        self.path.parent.mkdir()
        for data in ('not-json', json.dumps(dict(version=1, enabled=True, entries=[], open_url='https://example.com'))):
            self.path.write_text(data)
            loaded = HistoryStore(self.path)
            self.assertFalse(loaded.enabled)
            self.assertTrue(loaded.error)
            self.assertEqual(loaded.entries, ())

    def test_unwritable_storage_fails_locally_without_breaking_result(self):
        with patch('screenqr.history.os.replace', side_effect=PermissionError()):
            self.assertFalse(self.store.record(classify_payload('PUBLIC-FAILURE')))
        self.assertTrue(self.store.error)
        self.assertEqual(list(self.path.parent.glob('.history-*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
