"""Pure creation-race metadata tests; no Win32, UI, input, capture or network."""
from dataclasses import replace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import test_native_port as fixtures
from screenqr import native_port as module


class StartupCreationRaceTests(unittest.TestCase):
    def setUp(self):
        self.clock = fixtures.Clock()
        self.frame, self.mapping, _, self.ready = fixtures.fixture()
        self.api, self.input, self.own_api = MagicMock(), MagicMock(), MagicMock()
        self.api.fixed_version.return_value = (4, 1, 15, 13)
        self.own_api.kernel.GetCurrentProcessId.return_value = self.mapping.own_stage.pid
        self.own_api.user.IsWindow.return_value = True
        self.own_api.user.IsWindowVisible.return_value = True
        self.own_api.user.IsIconic.return_value = False
        self.own_api.user.GetAncestor.side_effect = lambda hwnd, flag: hwnd
        def owner_pid(hwnd, pointer):
            pointer._obj.value = self.mapping.own_stage.pid
            return 1
        self.own_api.user.GetWindowThreadProcessId.side_effect = owner_pid
        for name, value in (("_Gdi", lambda: self.api), ("_StageApi", lambda: self.own_api),
                           ("WeChatInput", lambda **kw: self.input),
                           ("stage_mapping_is_current", lambda *args: True)):
            patched = patch.object(module, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.port = module.WindowsWeChatPort(self.frame, self.mapping, lambda: True,
            max_evidence_age_ns=100_000, clock=self.clock)
        self.request = str(uuid4())
        self.port._request = self.request
        self.race = replace(self.ready, status="REJECTED", candidates=(),
            foreground_hwnd_at_start=777, foreground_hwnd_at_end=901,
            errors=("foreground_changed_during_query",))

    def test_unclassified_endpoint_gets_one_complete_read_before_any_ready_claim(self):
        with patch.object(module, "probe_screenshot_window", side_effect=[self.race, self.ready]) as query, \
                patch.object(module.time, "sleep") as sleep:
            result = self.port.native_snapshot(self.request, 10_000)
        self.assertEqual(result.instance.hwnd, 901)
        self.assertEqual(self.port._bound, result.instance)
        self.assertEqual(query.call_count, 2)
        sleep.assert_not_called()
        self.assertEqual(self.port.native_startup_transitions, {"startup_recheck_unclassified_endpoint": 1})
        self.assertEqual(self.port.last_native_probe, self.ready.to_dict())
        self.assertIsNone(self.port.last_native_error_code)
        self.assertEqual(self.input.method_calls, [])

    def test_foreign_missing_ambiguous_or_incomplete_recheck_never_waits_or_inputs(self):
        missing = replace(self.ready, status="NOT_FOUND", candidates=())
        foreign = replace(self.ready, candidates=(replace(self.ready.candidates[0], executable_path=r"C:\Other.exe"),))
        errors = replace(self.ready, status="REJECTED", errors=("enumeration_callback_failed",))
        ambiguous = replace(self.ready, status="AMBIGUOUS", candidates=self.ready.candidates*2)
        for bad in (missing, foreign, errors, ambiguous, self.race):
            with self.subTest(status=bad.status):
                self.port._native_creation_recheck_used = False
                with patch.object(module, "probe_screenshot_window", side_effect=[self.race, bad, self.ready]) as query, \
                        patch.object(module.time, "sleep") as sleep, self.assertRaises(ValueError):
                    self.port.native_snapshot(self.request, 10_000)
                self.assertEqual(query.call_count, 2)
                sleep.assert_not_called()
                self.assertIsNone(self.port._bound)
                self.assertEqual(self.port.last_native_error_code, "native_startup_creation_endpoint_not_verified")
        self.assertEqual(self.input.method_calls, [])

    def test_different_complete_endpoint_is_rejected_instead_of_rebound(self):
        other = replace(self.ready, candidates=(replace(self.ready.candidates[0], hwnd=902),),
            foreground_hwnd_at_start=902, foreground_hwnd_at_end=902)
        with patch.object(module, "probe_screenshot_window", side_effect=[self.race, other]) as query, \
                patch.object(module.time, "sleep") as sleep, self.assertRaises(ValueError):
            self.port.native_snapshot(self.request, 10_000)
        self.assertEqual(query.call_count, 2)
        sleep.assert_not_called()
        self.assertIsNone(self.port._bound)
        self.assertEqual(self.port.last_native_error_code, "native_startup_creation_endpoint_changed")

    def test_recheck_requires_fixed_version_and_original_deadline(self):
        self.api.fixed_version.return_value = (4, 1, 15, 14)
        with patch.object(module, "probe_screenshot_window", side_effect=[self.race, self.ready]) as query, \
                self.assertRaises(ValueError):
            self.port.native_snapshot(self.request, 10_000)
        self.assertEqual(query.call_count, 2)
        self.assertIsNone(self.port._bound)

        self.port._native_creation_recheck_used = False
        self.api.fixed_version.side_effect = lambda path: setattr(self.clock, "value", 10_000) or (4,1,15,13)
        with patch.object(module, "probe_screenshot_window", side_effect=[self.race, self.ready]) as query, \
                self.assertRaises(TimeoutError):
            self.port.native_snapshot(self.request, 10_000)
        self.assertEqual(query.call_count, 2)
        self.assertIsNone(self.port._bound)
        self.assertEqual(self.port.last_native_error_code, "native_readiness_deadline_expired")

    def test_unknown_start_does_not_qualify_for_creation_race_recheck(self):
        bad = replace(self.race, foreground_hwnd_at_start=999)
        with patch.object(module, "probe_screenshot_window", side_effect=[bad, self.ready]) as query, \
                self.assertRaises(ValueError):
            self.port.native_snapshot(self.request, 10_000)
        self.assertEqual(query.call_count, 1)
        self.assertFalse(self.port._native_creation_recheck_used)
        self.assertEqual(self.input.method_calls, [])

    def test_failed_creation_recheck_cannot_be_repeated_by_calling_snapshot_again(self):
        with patch.object(module, "probe_screenshot_window", side_effect=[self.race, self.race]) as query, \
                self.assertRaises(ValueError):
            self.port.native_snapshot(self.request, 10_000)
        self.assertEqual(query.call_count, 2)
        with patch.object(module, "probe_screenshot_window", side_effect=[self.race, self.ready]) as query, \
                self.assertRaises(ValueError):
            self.port.native_snapshot(self.request, 10_000)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(self.port.last_native_error_code, "native_startup_creation_recheck_already_used")
        self.assertEqual(self.input.method_calls, [])


if __name__ == "__main__":
    unittest.main()
