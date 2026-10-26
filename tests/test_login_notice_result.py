"""Bound result notices to one consumed request; no native UI or user data."""
from dataclasses import replace
import hashlib
import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from screenqr.handoff import CommitPermit
from screenqr.native_notice import NoticeResult
from screenqr.native_result import NativeResultObserver, ResultWindowEnumeration, ResultWindowIdentity, host_executable_path


class LoginNoticeResultTests(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{'APPDATA':r'C:\Users\Synthetic\AppData\Roaming'})
        env.start(); self.addCleanup(env.stop)
        self.time=100
        self.old=self.window(10)
        self.new=self.window(11)
        self.data=ResultWindowEnumeration((self.old,),10,10)
        self.reader=Mock(return_value=NoticeResult('CAMERA_REQUIRED','a'*64))
        self.observer=NativeResultObserver(enumerator=lambda expected:self.data,
            clock=self.clock,inspect_login_notice=True,notice_reader=self.reader)
        self.request,self.frame,self.evidence=(str(uuid4()) for _ in range(3))
        self.baseline=self.observer.capture_baseline(self.request,self.frame,self.evidence,deadline_ns=None).baseline
        self.time=110
        self.permit=CommitPermit(self.request,self.frame,str(uuid4()),110,(0,0,10,10))

    def clock(self):
        self.time+=1
        return self.time

    def window(self,hwnd):
        return ResultWindowIdentity(hwnd,200,400,host_executable_path(),'PublicFixture',
            (10,20,110,120),True,hashlib.sha256(b'Public notice').hexdigest())

    def observe(self):
        return self.observer.observe_new_window(self.baseline,self.permit,deadline_ns=None)

    def publish_new(self):
        self.data=ResultWindowEnumeration((self.old,self.new),11,11)

    def test_camera_notice_is_correlated_to_current_unique_new_foreground_host(self):
        self.publish_new()
        observed=self.observe()
        self.assertEqual(observed.status,'CAMERA_REQUIRED')
        self.assertEqual(observed.reason,'login_requires_camera_scan')
        self.assertEqual(observed.dispatch_id,self.permit.dispatch_id)
        self.assertFalse(observed.business_success)
        self.reader.assert_called_once_with(11)
        self.assertEqual(self.observe().status,'REJECTED')

    def test_old_ambiguous_or_nonforeground_hosts_are_not_read(self):
        for data in (ResultWindowEnumeration((replace(self.old,title_sha256='b'*64),),10,10),
                     ResultWindowEnumeration((self.old,self.new,self.window(12)),11,11),
                     ResultWindowEnumeration((self.old,self.new),10,10)):
            self.data=data
            self.assertNotEqual(self.observe().status,'CAMERA_REQUIRED')
        self.reader.assert_not_called()

    def test_foreground_or_identity_changes_across_text_read_invalidate_notice(self):
        self.publish_new()
        def changed(hwnd):
            self.data=replace(self.data,foreground_after=10)
            return NoticeResult('CAMERA_REQUIRED','a'*64)
        self.reader.side_effect=changed
        result=self.observe()
        self.assertEqual(result.status,'STALE')
        self.assertFalse(result.business_success)

    def test_loading_notice_can_progress_without_new_input_or_global_time_limit(self):
        self.publish_new()
        self.reader.side_effect=[NoticeResult('UNCONFIRMED','a'*64),
                                 NoticeResult('UNCONFIRMED','b'*64),NoticeResult('CAMERA_REQUIRED','c'*64)]
        first=self.observe()
        self.time+=30_000_000_000
        second=self.observe()
        self.assertEqual((first.status,second.status),('NOT_READY','NOT_READY'))
        self.assertNotEqual(first.progress_sha256,second.progress_sha256)
        self.assertEqual(self.observe().status,'CAMERA_REQUIRED')
        self.assertEqual(self.reader.call_count,3)

    def test_provider_unavailable_does_not_invent_camera_restriction(self):
        self.publish_new()
        self.reader.return_value=NoticeResult('UNAVAILABLE',None)
        result=self.observe()
        self.assertEqual(result.status,'NEW_WINDOW_OBSERVED')
        self.assertFalse(result.business_success)

    def test_partial_read_cannot_rebind_to_another_window(self):
        self.publish_new()
        self.reader.return_value=NoticeResult('UNCONFIRMED','a'*64)
        self.assertEqual(self.observe().status,'NOT_READY')
        replacement=self.window(12)
        self.data=ResultWindowEnumeration((self.old,replacement),12,12)
        self.assertEqual(self.observe().status,'REJECTED')
        self.reader.assert_called_once()
