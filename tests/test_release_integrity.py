"""临时目录实测完整性与路径门禁，不读取用户文件。"""
from pathlib import Path
import tempfile
import unittest

from screenqr.release import MANIFEST,write_checksums,verify_checksums


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)
        (self.root/'ScreenQR.exe').write_bytes(b'public-fake-file-not-executable')
        (self.root/'工具').mkdir(); (self.root/'工具/公开.txt').write_text('public',encoding='utf-8')

    def test_generated_manifest_verifies_and_does_not_hash_itself(self):
        self.assertEqual(write_checksums(self.root),2)
        self.assertTrue(verify_checksums(self.root).okay)
        self.assertNotIn(MANIFEST,(self.root/MANIFEST).read_text(encoding='utf-8'))
        self.assertEqual(write_checksums(self.root),2)
        self.assertTrue(verify_checksums(self.root).okay)

    def test_changed_missing_and_extra_files_are_distinguished(self):
        write_checksums(self.root)
        (self.root/'ScreenQR.exe').write_bytes(b'changed')
        (self.root/'工具/公开.txt').unlink()
        (self.root/'extra.txt').write_text('public')
        result=verify_checksums(self.root)
        self.assertEqual((result.changed,result.missing,result.extra),(1,1,1))
        self.assertFalse(result.okay)

    def test_traversal_absolute_duplicate_self_and_malformed_entries_rejected(self):
        digest='0'*64
        invalid=['../outside','C:/outside','/outside','工具/../outside','工具\\outside',
                 'ScreenQR.exe ',MANIFEST,'./ScreenQR.exe']
        for name in invalid:
            with self.subTest(name=name):
                (self.root/MANIFEST).write_text(f'{digest}  {name}\n',encoding='utf-8')
                with self.assertRaises(ValueError): verify_checksums(self.root)
        for text in ('not-a-checksum',f'{digest}  ScreenQR.exe\n{digest}  screenqr.exe\n',''):
            (self.root/MANIFEST).write_text(text)
            with self.assertRaises(ValueError): verify_checksums(self.root)


if __name__=='__main__': unittest.main()
