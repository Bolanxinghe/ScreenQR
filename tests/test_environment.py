"""环境错配和屏幕变化的确定性检查；不声称真的切过系统缩放或插拔显示器。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from PIL import Image
from screenqr.environment import (DesktopChanged,DesktopState,MonitorState,RuntimeAssessment,
    assess_environment,capture_checked)
from screenqr.profiles import DEFAULT_PROFILE,ProfileRegistry
from screenqr.app import ScreenQRApp,_Job
import test_automatic_wechat_detection as automatic_helpers


def desktop(dpi=192):
    rect=(0,0,3200,2000)
    return DesktopState(rect,(MonitorState(rect,(0,0,3200,1920),True,0),),rect,dpi)


class EnvironmentTests(unittest.TestCase):
    def setUp(self): self.registry=ProfileRegistry((DEFAULT_PROFILE,))

    def test_actual_facts_required_and_unknowns_disable_only_wechat(self):
        for registry,state,reader in (
            (ProfileRegistry((),'bad'),desktop(),lambda _:DEFAULT_PROFILE.version),
            (self.registry,None,lambda _:DEFAULT_PROFILE.version),
            (self.registry,replace(desktop(),interactive=False),lambda _:DEFAULT_PROFILE.version),
            (self.registry,desktop(),Mock(side_effect=OSError())),
            (self.registry,desktop(),lambda _:[4,1,15,13]),
        ):
            result=assess_environment(registry,state,version_reader=reader)
            self.assertIsNone(result.profile)
            self.assertTrue(result.message)

    def test_matching_environment_uses_exact_loaded_profile(self):
        result=assess_environment(self.registry,desktop(),version_reader=lambda _:DEFAULT_PROFILE.version)
        self.assertIs(result.profile,DEFAULT_PROFILE)
        self.assertEqual(result.reason,'matched')

    def test_version_and_display_mismatch_have_separate_plain_messages(self):
        wrong=assess_environment(self.registry,desktop(),version_reader=lambda _:(4,1,99,1))
        self.assertIsNone(wrong.profile); self.assertIn('这个微信版本',wrong.message)
        for dpi in (96,120,144):
            wrong=assess_environment(self.registry,desktop(dpi),version_reader=lambda _:DEFAULT_PROFILE.version)
            self.assertIsNone(wrong.profile); self.assertIn('屏幕设置',wrong.message)
        self.assertIn('普通码照常',wrong.message)

    def test_multi_monitor_never_inherits_single_monitor_native_profile(self):
        first=desktop().monitors[0]
        state=DesktopState((-1920,0,3200,2000),
            (MonitorState((-1920,0,0,1080),(-1920,0,0,1040),False,0),first),first.rect,192)
        self.assertTrue(state.permits((10,20,300,400)))
        self.assertFalse(state.permits((-20,20,300,400)))
        self.assertIsNone(assess_environment(self.registry,state,version_reader=lambda _:DEFAULT_PROFILE.version).profile)
        on_left=replace(state,own_monitor=(-1920,0,0,1080),dpi=120)
        self.assertTrue(on_left.permits((-1800,50,-1400,600)))
        self.assertFalse(on_left.permits((10,20,300,400)))

    def test_rotated_display_is_not_native_calibration(self):
        state=replace(desktop(),monitors=(replace(desktop().monitors[0],rotation=90),))
        self.assertIsNone(assess_environment(self.registry,state,version_reader=lambda _:DEFAULT_PROFILE.version).profile)

    def test_changed_before_capture_never_reads_old_rectangle(self):
        capture=Mock()
        with self.assertRaises(DesktopChanged):
            capture_checked((10,20,100,200),42,desktop(),probe=lambda _:desktop(120),capture=capture)
        capture.assert_not_called()

    def test_change_during_capture_closes_and_discards_owned_pixels(self):
        image=Image.new('RGB',(90,180),'white')
        with self.assertRaises(DesktopChanged):
            capture_checked((10,20,100,200),42,desktop(),probe=Mock(side_effect=[desktop(),desktop(120)]),capture=lambda _:image)
        with self.assertRaises(ValueError): image.tobytes()

    def test_same_facts_preserve_exact_image_and_negative_coordinate_source(self):
        rect=(-1920,0,0,1080)
        state=DesktopState(rect,(MonitorState(rect,rect,True,0),),rect,120)
        image=Image.new('RGB',(90,180),(3,4,5)); capture=Mock(return_value=image)
        actual=capture_checked((-1800,20,-1710,200),42,state,probe=lambda _:state,capture=capture)
        self.assertIs(actual,image); capture.assert_called_once_with((-1800,20,-1710,200))
        image.close()

    def test_cross_screen_or_locked_source_never_captures(self):
        capture=Mock()
        for bbox,state in (((-10,20,100,200),desktop()),((10,20,100,200),replace(desktop(),interactive=False))):
            with self.assertRaises(DesktopChanged): capture_checked(bbox,42,state,probe=lambda _:state,capture=capture)
        capture.assert_not_called()

    def test_mismatch_blocks_automatic_native_admission_before_reservation(self):
        helper=automatic_helpers.AutomaticWeChatDetectionTests(); app=helper.app()
        app._profiles=self.registry; app._environment=RuntimeAssessment(None,(4,1,99,1),'version','这个微信版本暂不能扫，普通码照常能扫。')
        app._display_signals=SimpleNamespace(epoch=0)
        for _ in range(3): helper.read(app,helper.FIRST)
        app.request_freeze.assert_not_called(); app._open_url.assert_not_called()
        self.assertEqual(app._wechat_seen_payloads,set())

    def test_mismatch_does_not_disable_ordinary_web_confirmation(self):
        helper=automatic_helpers.AutomaticWeChatDetectionTests(); app=helper.app()
        app._profiles=self.registry; app._environment=RuntimeAssessment(None,None,'configuration','微信配置没读好，普通码照常能扫。')
        helper.read(app,'https://example.com/public')
        self.assertEqual(app._candidate.open_url,'https://example.com/public')
        app._open_url.assert_not_called()

    def test_display_notification_epoch_invalidates_even_unchanged_screen_geometry(self):
        helper=automatic_helpers.AutomaticWeChatDetectionTests(); app=helper.app()
        app._display_signals=SimpleNamespace(epoch=2)
        job=_Job(1,bbox=(10,20,100,200),desktop=desktop(),display_epoch=1,own_hwnd=42)
        with patch('screenqr.app.probe_desktop',return_value=desktop()) as probe:
            self.assertFalse(app._job_display_current(job))
            probe.assert_not_called()


if __name__=='__main__': unittest.main()
