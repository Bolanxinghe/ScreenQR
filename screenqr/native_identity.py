"""Development-stage read-only metadata for a bounded WeChat screenshot candidate.

DLL binding and Win32 queries occur only in probe_screenshot_window(), never on
import. This module accepts no external HWND or opaque UI token; every HWND is
obtained from EnumWindows. It performs no capture, process-memory access, input,
activation, window mutation, application launch or third-party dependency call.

The implemented profile requires one primary monitor, an already per-monitor-
DPI-aware caller, exact fullscreen window/client geometry, the exact configured
installation path, and the exact title 'Weixin'. Unsupported or incomplete
metadata fails closed. This development profile is not a cross-version or
all-Windows compatibility claim; device/profile validation remains outstanding.

This module does not query or verify executable version or signature. Exact
path, process creation time and window metadata establish candidate identity
only. The upper layer must separately verify version/profile/session safeguards
and current foreground state. These non-atomic read-only queries provide no
action permission and no proof of QR recognition, page opening or business
success. No chat content, account identifiers or private pixel templates are used.
"""
from __future__ import annotations

from .profiles import DEFAULT_PROFILE, current_profile

from collections import Counter
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import ntpath
import sys
from typing import Literal

EXPECTED_EXECUTABLE = DEFAULT_PROFILE.executable
EXPECTED_SCREENSHOT_TITLE = DEFAULT_PROFILE.title
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_IS_APP_CONTAINER = 29
Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class NativeWindowIdentity:
    hwnd: int
    pid: int
    process_created_filetime_100ns: int
    executable_path: str
    title: str
    window_class: str
    window_rect: Rect
    client_screen_rect: Rect
    dpi: int
    foreground_match: bool
    hwnd_source: str = "EnumWindows; not a Sky window token"


@dataclass(frozen=True)
class WindowProbeResult:
    status: Literal["UNIQUE", "NOT_FOUND", "AMBIGUOUS", "REJECTED"]
    reason: str
    candidates: tuple[NativeWindowIdentity, ...]
    primary_screen_rect: Rect | None
    monitor_count: int | None
    thread_dpi_awareness: int | None
    foreground_hwnd_at_start: int | None
    foreground_hwnd_at_end: int | None
    counts: dict[str, int]
    errors: tuple[str, ...]
    evidence_scope: str = "read-only path/instance/window metadata; no signature, UI-control or business-success claim"

    def to_dict(self) -> dict:
        return asdict(self)


class _MetadataError(Exception):
    pass


class _Api:
    """All pointer widths, callback convention and return values are explicit."""

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.enum_callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM,
        )
        signatures = (
            (self.user32, "EnumWindows", [self.enum_callback_type, wintypes.LPARAM], wintypes.BOOL),
            (self.user32, "IsWindow", [wintypes.HWND], wintypes.BOOL),
            (self.user32, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
            (self.user32, "GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (self.user32, "GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            (self.user32, "GetClientRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            (self.user32, "ClientToScreen", [wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            (self.user32, "GetDpiForWindow", [wintypes.HWND], wintypes.UINT),
            (self.user32, "GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user32, "GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user32, "GetForegroundWindow", [], wintypes.HWND),
            (self.user32, "GetSystemMetrics", [ctypes.c_int], ctypes.c_int),
            (self.user32, "GetThreadDpiAwarenessContext", [], wintypes.HANDLE),
            (self.user32, "GetAwarenessFromDpiAwarenessContext", [wintypes.HANDLE], ctypes.c_int),
            (self.kernel32, "OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            (self.kernel32, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            (self.kernel32, "QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self.kernel32, "GetProcessTimes", [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)], wintypes.BOOL),
        )
        for library, name, arguments, result in signatures:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    def is_app_container(self) -> bool:
        """Read only our token; unknown runtime capability must stop the probe.

        An AppContainer can enumerate windows while being unable to query the
        target process. Treating those filtered windows as genuine absence would
        make NOT_FOUND misleading. Never open or modify another process token.
        """
        token = wintypes.HANDLE()
        try:
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            signatures = (
                (self.kernel32, "GetCurrentProcess", [], wintypes.HANDLE),
                (advapi32, "OpenProcessToken", [wintypes.HANDLE, wintypes.DWORD,
                    ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL),
                (advapi32, "GetTokenInformation", [wintypes.HANDLE, ctypes.c_int,
                    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            )
            for library, name, arguments, result in signatures:
                function = getattr(library, name)
                function.argtypes, function.restype = arguments, result
            process = self.kernel32.GetCurrentProcess()  # Pseudo-handle: do not close.
            if not process or not advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
                raise _MetadataError("runtime_query_unavailable")
            if not token.value:
                raise _MetadataError("runtime_query_unavailable")
            value, returned = wintypes.DWORD(), wintypes.DWORD()
            if not advapi32.GetTokenInformation(token, TOKEN_IS_APP_CONTAINER,
                    ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(returned)):
                raise _MetadataError("runtime_query_unavailable")
            if returned.value != ctypes.sizeof(value) or value.value not in (0, 1):
                raise _MetadataError("runtime_query_unavailable")
            return bool(value.value)
        except _MetadataError:
            raise
        except Exception:
            raise _MetadataError("runtime_query_unavailable") from None
        finally:
            if token.value:
                try:
                    closed = bool(self.kernel32.CloseHandle(token))
                except Exception:
                    closed = False
                if not closed:
                    raise _MetadataError("runtime_token_handle_close_failed")

    def process_id(self, hwnd: int) -> int:
        pid = wintypes.DWORD()
        tid = self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not tid or not pid.value:
            raise _MetadataError("window_process_id_unavailable")
        return int(pid.value)

    def image_path(self, process: int) -> str | None:
        # Fixed bounded buffer. Failed/truncated queries never count as approval.
        capacity = 32768
        buffer = ctypes.create_unicode_buffer(capacity)
        length = wintypes.DWORD(capacity)
        if not self.kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(length)):
            return None
        if not 0 < length.value < capacity:
            return None
        return buffer.value

    def window_rect(self, hwnd: int) -> Rect:
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise _MetadataError("window_rect_unavailable")
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)

    def client_screen_rect(self, hwnd: int) -> Rect:
        rect = wintypes.RECT()
        if not self.user32.GetClientRect(hwnd, ctypes.byref(rect)):
            raise _MetadataError("client_rect_unavailable")
        first, last = wintypes.POINT(rect.left, rect.top), wintypes.POINT(rect.right, rect.bottom)
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(first)):
            raise _MetadataError("client_origin_unavailable")
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(last)):
            raise _MetadataError("client_extent_unavailable")
        return int(first.x), int(first.y), int(last.x), int(last.y)

    def creation_time(self, process: int) -> int:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.kernel32.GetProcessTimes(process, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            raise _MetadataError("process_creation_time_unavailable")
        value = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        if value <= 0:
            raise _MetadataError("process_creation_time_invalid")
        return value

    def exact_title(self, hwnd: int) -> bool:
        # Read at most len(expected)+1 characters, not an arbitrary chat title.
        # The spare character distinguishes the exact title from 'Weixin...'.
        buffer = ctypes.create_unicode_buffer(len(current_profile().title) + 2)
        ctypes.set_last_error(0)
        copied = self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        if copied == 0 and ctypes.get_last_error() != 0:
            raise _MetadataError("fullscreen_title_unavailable")
        return copied == len(current_profile().title) and buffer.value == current_profile().title

    def window_class(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        copied = self.user32.GetClassNameW(hwnd, buffer, len(buffer))
        if copied <= 0 or copied >= len(buffer) - 1:
            raise _MetadataError("window_class_unavailable_or_truncated")
        return buffer.value

    def screen(self) -> tuple[int, Rect]:
        metric = self.user32.GetSystemMetrics
        count, width, height = metric(80), metric(0), metric(1)  # SM_CMONITORS/CXSCREEN/CYSCREEN
        if count != 1:
            raise _MetadataError("single_primary_monitor_profile_required")
        if width <= 0 or height <= 0:
            raise _MetadataError("primary_screen_dimensions_unavailable")
        # One primary monitor should exactly equal the complete virtual desktop.
        if (metric(76), metric(77), metric(78), metric(79)) != (0, 0, width, height):
            raise _MetadataError("single_primary_screen_geometry_inconsistent")
        return int(count), (0, 0, int(width), int(height))


def probe_screenshot_window() -> WindowProbeResult:
    """Enumerate and report one eligible native window, without controlling it.

    The caller must arrange its already authorized public test screenshot session
    through its normal UI-control tool. This function accepts no guessed HWND and
    never maps an opaque Sky identifier to a Win32 handle.

    Window titles/classes are read ONLY after the full process path is verified,
    visibility confirmed and exact primary-screen window/client coverage checked.
    Unverified process paths are discarded without reading their window titles.
    Ambiguous/unknown eligible-window metadata is rejected. A UNIQUE result means
    exactly one verified candidate; foreground_match must still be checked by a
    future controller, together with its own version/profile/session safeguards.
    """
    counts: Counter[str] = Counter()
    errors: list[str] = []
    candidates: list[NativeWindowIdentity] = []
    monitor_count: int | None = None
    primary: Rect | None = None
    awareness: int | None = None
    foreground_start: int | None = None
    foreground_end: int | None = None

    def result(status: str, reason: str) -> WindowProbeResult:
        return WindowProbeResult(
            status, reason, tuple(candidates), primary, monitor_count, awareness,
            foreground_start, foreground_end, dict(counts), tuple(errors),
        )

    if sys.platform != "win32":
        return result("REJECTED", "windows_required")
    try:
        api = _Api()
    except (AttributeError, OSError):
        return result("REJECTED", "required_read_only_win32_api_unavailable")
    try:
        if api.is_app_container():
            return result("REJECTED", "app_container_runtime_unsupported")
    except _MetadataError as exc:
        return result("REJECTED", str(exc))
    try:
        context = api.user32.GetThreadDpiAwarenessContext()
        awareness = int(api.user32.GetAwarenessFromDpiAwarenessContext(context)) if context else -1
        if awareness != 2:  # DPI_AWARENESS_PER_MONITOR_AWARE
            return result("REJECTED", "caller_must_already_be_per_monitor_dpi_aware")
        monitor_count, primary = api.screen()
        foreground_start = int(api.user32.GetForegroundWindow() or 0)
        if not foreground_start:
            return result("REJECTED", "foreground_window_unavailable")
    except _MetadataError as exc:
        return result("REJECTED", str(exc))

    seen: set[int] = set()

    def visit(raw_hwnd: int, unused_lparam: int) -> bool:
        # A callback must not let exceptions cross the Win32 callback boundary.
        try:
            hwnd = int(raw_hwnd or 0)
            if not hwnd or hwnd in seen:
                return True
            seen.add(hwnd)
            counts["enumerated_windows"] += 1
            try:
                pid = api.process_id(hwnd)
            except _MetadataError:
                counts["unverified_process_windows"] += 1
                return True
            process = api.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not process:
                counts["unverified_process_windows"] += 1
                return True
            try:
                path = api.image_path(process)
                if path is None or ntpath.normcase(path) != ntpath.normcase(current_profile().executable):
                    counts["discarded_non_whitelisted_process_windows"] += 1
                    return True
                counts["verified_process_windows"] += 1
                if not api.user32.IsWindowVisible(hwnd):
                    counts["discarded_nonvisible_windows"] += 1
                    return True
                window_rect = api.window_rect(hwnd)
                if window_rect != primary:
                    counts["discarded_nonfullscreen_windows"] += 1
                    return True
                client_rect = api.client_screen_rect(hwnd)
                if client_rect != primary:
                    counts["discarded_nonfullscreen_clients"] += 1
                    return True
                # The title has still not been read at this point.
                dpi = int(api.user32.GetDpiForWindow(hwnd))
                if dpi <= 0:
                    raise _MetadataError("eligible_window_dpi_unavailable")
                created = api.creation_time(process)
                if not api.exact_title(hwnd):
                    counts["discarded_other_fullscreen_titles"] += 1
                    return True
                window_class = api.window_class(hwnd)
                # Check lifetime/geometry again while the process handle remains
                # held. No claim is made that separate Win32 calls are atomic.
                if not api.user32.IsWindow(hwnd) or api.process_id(hwnd) != pid:
                    raise _MetadataError("eligible_window_identity_changed_during_query")
                if not api.user32.IsWindowVisible(hwnd):
                    raise _MetadataError("eligible_window_became_invisible")
                if api.window_rect(hwnd) != window_rect or api.client_screen_rect(hwnd) != client_rect:
                    raise _MetadataError("eligible_window_geometry_changed_during_query")
                if int(api.user32.GetDpiForWindow(hwnd)) != dpi:
                    raise _MetadataError("eligible_window_dpi_changed_during_query")
                if not api.exact_title(hwnd) or api.window_class(hwnd) != window_class:
                    raise _MetadataError("eligible_window_title_or_class_changed_during_query")
                candidates.append(NativeWindowIdentity(
                    hwnd=hwnd, pid=pid, process_created_filetime_100ns=created,
                    executable_path=current_profile().executable, title=current_profile().title,
                    window_class=window_class, window_rect=window_rect,
                    client_screen_rect=client_rect, dpi=dpi,
                    foreground_match=hwnd == foreground_start,
                ))
            except _MetadataError as exc:
                # This code can describe only a whitelisted process. No title
                # or arbitrary process path is copied into diagnostics.
                errors.append(str(exc))
            finally:
                if not api.kernel32.CloseHandle(process):
                    errors.append("process_query_handle_close_failed")
            return True
        except Exception:
            errors.append("enumeration_callback_failed")
            return False

    callback = api.enum_callback_type(visit)  # Keep a strong reference until EnumWindows returns.
    ctypes.set_last_error(0)
    succeeded = bool(api.user32.EnumWindows(callback, 0))
    if not succeeded:
        code = ctypes.get_last_error()
        errors.append(f"enum_windows_incomplete_winerror_{code}")
    foreground_end = int(api.user32.GetForegroundWindow() or 0)
    try:
        final_count, final_primary = api.screen()
    except _MetadataError as exc:
        errors.append(str(exc))
    else:
        if (final_count, final_primary) != (monitor_count, primary):
            errors.append("display_topology_changed_during_query")
    if not foreground_end or foreground_start != foreground_end:
        errors.append("foreground_changed_during_query")
    if errors:
        return result("REJECTED", "incomplete_or_changed_metadata")
    if len(candidates) > 1:
        return result("AMBIGUOUS", "multiple_verified_fullscreen_weixin_windows")
    if not candidates:
        return result("NOT_FOUND", "no_verified_exact_fullscreen_weixin_window")
    return result("UNIQUE", "one_verified_native_metadata_candidate_only")
