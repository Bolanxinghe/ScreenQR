"""配置迁移、白名单和请求隔离；不把配置检查当作跨机实测。"""
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import threading
import unittest

from screenqr.profiles import (DEFAULT_PROFILE, current_profile, load_profiles,
    profile_from_record, profile_scope, ProfileRegistry)
from screenqr.handoff import Policy, NATIVE_DISPLAY_PROFILE_A
from screenqr.native_input import _absolute_point


def record():
    return json.loads(json.dumps(asdict(DEFAULT_PROFILE)))


class ProfileTests(unittest.TestCase):
    def test_bundled_profile_exactly_preserves_existing_calibration(self):
        loaded=load_profiles()
        self.assertIsNone(loaded.error)
        self.assertEqual(loaded.profiles,(DEFAULT_PROFILE,))
        self.assertEqual(loaded.match((4,1,15,13),(0,0,3200,2000),192,1),DEFAULT_PROFILE)

    def test_version_dpi_screen_and_monitor_mismatches_never_match(self):
        registry=ProfileRegistry((DEFAULT_PROFILE,))
        for version,screen,dpi,count in (
            ((4,1,15,14),DEFAULT_PROFILE.screen,192,1),
            (DEFAULT_PROFILE.version,(0,0,1920,1080),192,1),
            (DEFAULT_PROFILE.version,DEFAULT_PROFILE.screen,144,1),
            (DEFAULT_PROFILE.version,DEFAULT_PROFILE.screen,192,2),
        ):
            self.assertIsNone(registry.match(version,screen,dpi,count))

    def test_new_profile_changes_policy_and_input_mapping_without_global_rewrite(self):
        data=record(); data.update(id='public-layout-check',version=[4,1,99,1],screen=[0,0,2560,1440],dpi=144,
            border_id='public-border',toolbar_id='public-toolbar',display_id='public-display')
        profile=profile_from_record(data)
        with profile_scope(profile):
            policy=Policy(2_000_000_000,300_000_000,200_000_000,100_000_000,
                selection_evidence_mode=NATIVE_DISPLAY_PROFILE_A)
            self.assertEqual(policy.expected_file_version,(4,1,99,1))
            self.assertEqual(policy.expected_screen,(0,0,2560,1440))
            self.assertEqual(policy.expected_dpi,144)
            self.assertEqual(_absolute_point(2559,1439),((5119*65536)//5120,(2879*65536)//2880))
            with self.assertRaises(ValueError): _absolute_point(2560,1440)
        self.assertIs(current_profile(),DEFAULT_PROFILE)

    def test_profile_is_frozen_and_request_context_does_not_leak_to_other_thread(self):
        profile=replace(DEFAULT_PROFILE,id='isolated')
        seen=[]
        with profile_scope(profile):
            thread=threading.Thread(target=lambda:seen.append(current_profile()))
            thread.start(); thread.join()
            self.assertIs(current_profile(),profile)
            with self.assertRaises(AttributeError): profile.dpi=96
        self.assertEqual(seen,[DEFAULT_PROFILE])
        self.assertIs(current_profile(),DEFAULT_PROFILE)

    def test_missing_corrupt_duplicate_and_oversized_configuration_disable_native_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'profiles.json'
            for value in ('not json','{"schema_version":1,"schema_version":1,"profiles":[]}',
                          ' '*65_537,json.dumps(dict(schema_version=1,profiles=[record(),record()]))):
                path.write_text(value,encoding='utf-8')
                self.assertEqual(load_profiles(path).profiles,())
                self.assertIsNotNone(load_profiles(path).error)
            self.assertEqual(load_profiles(Path(folder)/'missing').profiles,())

    def test_unknown_fields_commands_paths_and_relaxed_masks_are_rejected(self):
        for key,value in (('extra','anything'),('executable','C:/untrusted/Weixin.exe'),
                          ('model','run-python'),('title',''),('dpi',True),('version',[4,1,15,True])):
            data=record(); data[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError): profile_from_record(data)
        for key,value in (('neutral_max',21),('dark_max',177),('light_min',223),('quiet_pixels',0)):
            data=record(); data['toolbar'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError): profile_from_record(data)

    def test_border_structure_and_original_white_padding_cannot_be_relaxed(self):
        for key,value in (('guard',0),('padding',0),('green_run',1),('tangent',1),('inner',0)):
            data=record(); data['boundary'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError): profile_from_record(data)
        data=record(); data['boundary']['green']=data['boundary']['white']
        with self.assertRaises(ValueError): profile_from_record(data)

    def test_profile_scope_restores_on_exception(self):
        with self.assertRaises(RuntimeError):
            with profile_scope(replace(DEFAULT_PROFILE,id='temporary')): raise RuntimeError()
        self.assertIs(current_profile(),DEFAULT_PROFILE)


if __name__=='__main__': unittest.main()
