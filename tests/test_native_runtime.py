"""Fake runtime capability checks only; no real Win32, input, capture or GUI."""
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from screenqr import native_identity as identity


class NativeRuntimeTests(unittest.TestCase):
    def probe(self, api):
        with patch.object(identity.sys, "platform", "win32"), \
                patch.object(identity, "_Api", return_value=api), \
                patch.object(identity.ctypes, "set_last_error", create=True), \
                patch.object(identity.ctypes, "get_last_error", return_value=0, create=True):
            return identity.probe_screenshot_window()

    def fake_probe_api(self, runtime):
        return SimpleNamespace(
            is_app_container=runtime,
            user32=SimpleNamespace(
                GetThreadDpiAwarenessContext=Mock(return_value=42),
                GetAwarenessFromDpiAwarenessContext=Mock(return_value=2),
                GetForegroundWindow=Mock(return_value=123),
                EnumWindows=Mock(return_value=True)),
            screen=Mock(return_value=(1, (0, 0, 3200, 2000))),
            enum_callback_type=lambda callback: callback)

    def test_app_container_rejects_before_window_enumeration(self):
        api = self.fake_probe_api(Mock(return_value=True))
        result = self.probe(api)
        self.assertEqual((result.status, result.reason),
            ("REJECTED", "app_container_runtime_unsupported"))
        api.user32.EnumWindows.assert_not_called()
        api.user32.GetThreadDpiAwarenessContext.assert_not_called()
        self.assertEqual(result.counts, {})

    def test_unavailable_runtime_query_rejects_before_window_enumeration(self):
        for reason in ("runtime_query_unavailable", "runtime_token_handle_close_failed"):
            with self.subTest(reason=reason):
                api = self.fake_probe_api(Mock(side_effect=identity._MetadataError(reason)))
                result = self.probe(api)
                self.assertEqual((result.status, result.reason), ("REJECTED", reason))
                api.user32.EnumWindows.assert_not_called()

    def test_normal_runtime_preserves_existing_empty_enumeration_result(self):
        api = self.fake_probe_api(Mock(return_value=False))
        result = self.probe(api)
        self.assertEqual((result.status, result.reason),
            ("NOT_FOUND", "no_verified_exact_fullscreen_weixin_window"))
        api.user32.EnumWindows.assert_called_once()
        self.assertEqual(api.screen.call_count, 2)

    def token_api(self, value=0, query_ok=True, close_ok=True):
        api = object.__new__(identity._Api)
        api.kernel32 = SimpleNamespace(
            GetCurrentProcess=Mock(return_value=-1),
            CloseHandle=Mock(return_value=close_ok))

        def open_token(process, access, token):
            self.assertEqual((process, access), (-1, identity.TOKEN_QUERY))
            token._obj.value = 456
            return True

        def query(token, information_class, output, capacity, returned):
            self.assertEqual(information_class, 29)
            self.assertEqual(capacity, ctypes.sizeof(identity.wintypes.DWORD))
            output._obj.value = value
            returned._obj.value = capacity
            return query_ok

        advapi32 = SimpleNamespace(OpenProcessToken=Mock(side_effect=open_token),
            GetTokenInformation=Mock(side_effect=query))
        return api, advapi32

    def test_own_token_is_query_only_and_closed_for_both_runtime_values(self):
        for value in (0, 1):
            with self.subTest(value=value):
                api, advapi32 = self.token_api(value=value)
                with patch.object(identity.ctypes, "WinDLL", return_value=advapi32, create=True):
                    self.assertEqual(api.is_app_container(), bool(value))
                api.kernel32.CloseHandle.assert_called_once()
                self.assertEqual(api.kernel32.CloseHandle.call_args.args[0].value, 456)

    def test_failed_token_query_closes_handle_and_rejects(self):
        api, advapi32 = self.token_api(query_ok=False)
        with patch.object(identity.ctypes, "WinDLL", return_value=advapi32, create=True):
            with self.assertRaisesRegex(identity._MetadataError, "^runtime_query_unavailable$"):
                api.is_app_container()
        api.kernel32.CloseHandle.assert_called_once()

    def test_unsupported_token_api_rejects_without_open_handle(self):
        api, unused = self.token_api()
        with patch.object(identity.ctypes, "WinDLL", return_value=SimpleNamespace(), create=True):
            with self.assertRaisesRegex(identity._MetadataError, "^runtime_query_unavailable$"):
                api.is_app_container()
        api.kernel32.CloseHandle.assert_not_called()

    def test_token_cleanup_failure_rejects_even_after_successful_query(self):
        api, advapi32 = self.token_api(close_ok=False)
        with patch.object(identity.ctypes, "WinDLL", return_value=advapi32, create=True):
            with self.assertRaisesRegex(identity._MetadataError, "^runtime_token_handle_close_failed$"):
                api.is_app_container()


if __name__ == "__main__":
    unittest.main()
