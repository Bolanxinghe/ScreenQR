"""Own-window activation guard seams only; no real Win32 or input calls."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from screenqr.stage import StageValidationError, focus_own_stage


class StageFocusTests(unittest.TestCase):
    HWND = 101
    PID = 123

    def api(self, *, owners=(123, 123), roots=(101, 101),
            foreground=(202, 202, 101), valid=(True, True), accepted=True,
            source_owner=123, source_root=202, source_valid=True):
        owners = iter(owners)
        roots, valid = iter(roots), iter(valid)
        def owner(hwnd, pointer):
            pointer._obj.value = source_owner if hwnd == 202 else next(owners)
            return True
        return SimpleNamespace(
            user=SimpleNamespace(
                IsWindow=Mock(side_effect=lambda hwnd: source_valid if hwnd == 202 else next(valid)),
                GetWindowThreadProcessId=Mock(side_effect=owner),
                GetAncestor=Mock(side_effect=lambda hwnd, flag: source_root if hwnd == 202 else next(roots)),
                GetForegroundWindow=Mock(side_effect=foreground),
                SetForegroundWindow=Mock(return_value=accepted)),
            kernel=SimpleNamespace(GetCurrentProcessId=Mock(return_value=self.PID)))

    def focus(self, api, hwnd=HWND):
        with patch('screenqr.stage._StageApi', return_value=api):
            return focus_own_stage(hwnd, allowed_foreground_hwnds=(202,))

    def test_foreign_nonroot_or_missing_window_never_receives_activation(self):
        for api in (self.api(owners=(999,)), self.api(roots=(999,)),
                    self.api(valid=(False,))):
            with self.subTest(api=api):
                with self.assertRaises(StageValidationError):
                    self.focus(api)
                api.user.SetForegroundWindow.assert_not_called()
                api.user.GetForegroundWindow.assert_not_called()
        for invalid in (0, -1, True, None):
            with self.subTest(hwnd=invalid):
                api = self.api()
                with self.assertRaises(StageValidationError):
                    self.focus(api, invalid)
                api.user.SetForegroundWindow.assert_not_called()

    def test_already_foreground_own_window_needs_no_activation(self):
        api = self.api(foreground=(self.HWND,))
        self.assertTrue(self.focus(api))
        api.user.SetForegroundWindow.assert_not_called()

    def test_os_refusal_is_false_without_retry_or_success_claim(self):
        api = self.api(accepted=False)
        self.assertFalse(self.focus(api))
        api.user.SetForegroundWindow.assert_called_once_with(self.HWND)
        self.assertEqual(api.user.GetWindowThreadProcessId.call_count, 2)
        self.assertEqual(api.user.GetForegroundWindow.call_count, 2)

    def test_reported_activation_without_actual_foreground_is_false(self):
        api = self.api(foreground=(202, 202, 303))
        self.assertFalse(self.focus(api))
        api.user.SetForegroundWindow.assert_called_once_with(self.HWND)
        self.assertEqual(api.user.GetWindowThreadProcessId.call_count, 3)

    def test_external_foreground_or_reused_source_never_receives_activation(self):
        for api in (self.api(foreground=(303,)), self.api(foreground=(202, 303)), self.api(source_owner=999),
                    self.api(source_root=999), self.api(source_valid=False)):
            with self.subTest(api=api):
                self.assertFalse(self.focus(api))
                api.user.SetForegroundWindow.assert_not_called()

    def test_owner_root_or_existence_change_after_activation_is_false(self):
        for api in (self.api(owners=(self.PID, 999)),
                    self.api(roots=(self.HWND, 999)),
                    self.api(valid=(True, False))):
            with self.subTest(api=api):
                self.assertFalse(self.focus(api))
                api.user.SetForegroundWindow.assert_called_once_with(self.HWND)

    def test_success_requires_post_activation_identity_and_actual_foreground(self):
        api = self.api()
        self.assertTrue(self.focus(api))
        api.user.SetForegroundWindow.assert_called_once_with(self.HWND)
        self.assertEqual(api.user.IsWindow.call_count, 3)
        self.assertEqual(api.user.GetWindowThreadProcessId.call_count, 3)
        self.assertEqual(api.user.GetAncestor.call_count, 3)
        self.assertEqual(api.user.GetForegroundWindow.call_count, 3)


if __name__ == '__main__':
    unittest.main()
