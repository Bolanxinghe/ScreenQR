"""ScreenQR product inputs: one trigger, one drag, one gated QR click, cleanup.

No arbitrary app/keyboard API is exposed. Every batch is preceded by a live
foreground/source check. SendInput is not an atomic window transaction and its
return value does not establish application acceptance. Partial/raised/late
dispatch is UNKNOWN and never retried. Testing this product's UI is separate
from using this module as a general desktop-automation helper.

Win32 contracts: learn.microsoft.com/windows/win32/api/winuser/{nf-winuser-
sendinput,ns-winuser-mouseinput,nf-winuser-getasynckeystate}.
"""
from __future__ import annotations

from .profiles import DEFAULT_PROFILE, current_profile

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import os
import sys
import threading
import time
from typing import Callable
from uuid import UUID

from .handoff import CommitPermit, NativeInstance


class InputOutcomeUnknown(RuntimeError):
    pass


class InputBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class InputDispatchReport:
    """Immutable diagnostic metadata, never application/business acceptance."""
    operation: str
    error_code: str
    state: str
    send_input_calls: int
    operation_send_input_calls: int
    returned_event_count: int | None
    consumed_operations: tuple[str, ...]
    outcome_unknown: bool
    win32_error: int | None = None


_REPORT_CODES = frozenset((
    "invalid_request_identity", "invalid_trigger_identity",
    "input_already_consumed_or_outcome_unknown",
    "native_instance_changed_no_cross_instance_input",
    "dispatch_clock_invalid_or_regressed", "dispatch_deadline_expired",
    "dpi_awareness_mismatch", "desktop_profile_mismatch", "user_keys_active",
    "desktop_check_raised", "desktop_profile_or_user_input_changed",
    "trigger_own_foreground_changed", "trigger_own_foreground_check_raised",
    "source_or_native_validation_failed", "source_or_native_validation_raised",
    "complete_native_instance_and_bbox_required", "selection_outside_even_fixed_profile",
    "bound_native_instance_and_consumed_commit_permit_required",
    "candidate_outside_fixed_profile", "complete_native_instance_required",
    "dispatch_partial_or_late", "dispatch_raised_no_retry",
    "input_operation_reentry_blocked", "dispatch_send_api_unavailable",
))


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", _INPUTUNION)]


def _absolute_point(x: int, y: int) -> tuple[int, int]:
    """Center of a physical pixel in this explicitly supported virtual desktop."""
    if type(x) is not int or type(y) is not int or not (0 <= x < current_profile().width and 0 <= y < current_profile().height):
        raise ValueError("point outside the fixed physical profile")
    return ((2*x+1)*65536//(2 * current_profile().width), (2*y+1)*65536//(2 * current_profile().height))


def _key(vk: int, *, up: bool = False) -> _INPUT:
    result = _INPUT(type=1)
    result.ki = _KEYBDINPUT(vk, 0, 2 if up else 0, 0, 0)
    return result


def _mouse(x: int, y: int, flags: int) -> _INPUT:
    result = _INPUT(type=0)
    # MOVE | MOVE_NOCOALESCE | VIRTUALDESK | ABSOLUTE. Retain the held
    # movement in the single drag batch; acceptance still needs observation.
    result.mi = _MOUSEINPUT(*_absolute_point(x, y), 0, flags | 0xE001, 0, 0)
    return result


class _Api:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise InputBlocked("windows_required")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        for name, args, restype in (
            ("SendInput", [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int], wintypes.UINT),
            ("GetAsyncKeyState", [ctypes.c_int], ctypes.c_short),
            ("GetForegroundWindow", [], wintypes.HWND),
            ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            ("GetSystemMetrics", [ctypes.c_int], ctypes.c_int),
            ("GetThreadDpiAwarenessContext", [], wintypes.HANDLE),
            ("GetAwarenessFromDpiAwarenessContext", [wintypes.HANDLE], ctypes.c_int),
        ):
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, restype
        expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        if ctypes.sizeof(_INPUT) != expected_size:
            raise InputBlocked("unexpected_input_abi")

    def own_foreground(self) -> bool:
        hwnd = self.user.GetForegroundWindow()
        pid = wintypes.DWORD()
        return bool(hwnd and self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    and pid.value == os.getpid())

    def idle_fixed_desktop(self) -> bool:
        self.last_idle_error_code = "desktop_check_raised"
        context = self.user.GetThreadDpiAwarenessContext()
        if not context or self.user.GetAwarenessFromDpiAwarenessContext(context) != 2:
            self.last_idle_error_code = "dpi_awareness_mismatch"
            return False
        metric = self.user.GetSystemMetrics
        if tuple(metric(i) for i in (80, 76, 77, 78, 79)) != (1, 0, 0, current_profile().width, current_profile().height):
            self.last_idle_error_code = "desktop_profile_mismatch"
            return False
        # Check high bits only: the historical "pressed since last query" bit is
        # not reliable. Never release or alter a key the user is holding.
        if any(self.user.GetAsyncKeyState(vk) & 0x8000
               for vk in (1, 2, 4, 5, 6, 16, 17, 18, 27, 65, 91, 92)):
            self.last_idle_error_code = "user_keys_active"
            return False
        self.last_idle_error_code = "ok"
        return True

    def send(self, events: tuple[_INPUT, ...]) -> int:
        self.last_send_win32_error = None
        array = (_INPUT * len(events))(*events)
        ctypes.set_last_error(0)
        try:
            count = self.user.SendInput(len(events), array, ctypes.sizeof(_INPUT))
        finally:
            # Cache immediately, before another Win32 call can replace it.
            # This value alone cannot establish why an input was rejected.
            self.last_send_win32_error = ctypes.get_last_error()
        return int(count)


class WeChatInput:
    """One product request. No activation, background input or retry methods."""
    def __init__(self, *, clock: Callable[[], int] = time.monotonic_ns,
                 _api: object | None = None) -> None:
        self._api = _api if _api is not None else _Api()
        self._clock = clock
        self._request_id: str | None = None
        self._used: set[str] = set()
        self._unknown = False
        self._lock = threading.RLock()
        self._bound_instance: NativeInstance | None = None
        self._last_ns = -1
        self._send_input_calls = 0
        self._returned_event_count: int | None = None
        self._win32_error: int | None = None
        self._last_report = InputDispatchReport("none", "not_attempted", "NOT_DISPATCHED",
                                               0, 0, None, (), False)
        self._active_operation = False

    @property
    def last_report(self) -> InputDispatchReport:
        with self._lock:
            return self._last_report

    def metadata_only(self) -> dict:
        """Detached snapshot: no pixels, input events, paths or request IDs."""
        with self._lock:
            return {"metadata_only": True, "application_acceptance_verified": False,
                    **asdict(self._last_report)}

    @contextmanager
    def _operation(self, operation: str):
        # Covers public argument checks too, before _claim can be entered.
        with self._lock:
            if self._active_operation:
                raise InputBlocked("input_operation_reentry_blocked")
            self._active_operation = True
            before = self._send_input_calls
            self._returned_event_count = None
            self._win32_error = None
            try:
                yield
            except BaseException as exc:
                dispatched = self._send_input_calls != before
                if dispatched:
                    self._unknown = True
                code = str(exc) if isinstance(exc, (InputBlocked, InputOutcomeUnknown)) else ""
                if code not in _REPORT_CODES:
                    code = "dispatch_raised_no_retry" if dispatched else "dispatch_precheck_raised"
                self._last_report = InputDispatchReport(operation, code,
                    "OUTCOME_UNKNOWN" if dispatched else "NOT_DISPATCHED",
                    self._send_input_calls, self._send_input_calls-before,
                    self._returned_event_count, tuple(sorted(self._used)), self._unknown,
                    self._win32_error)
                raise
            else:
                dispatched = self._send_input_calls != before
                self._last_report = InputDispatchReport(operation,
                    "dispatch_returned" if dispatched else "dispatch_not_reached",
                    "DISPATCH_RETURNED" if dispatched else "NOT_DISPATCHED",
                    self._send_input_calls, self._send_input_calls-before,
                    self._returned_event_count, tuple(sorted(self._used)), self._unknown,
                    self._win32_error)
            finally:
                self._active_operation = False

    @staticmethod
    def _uuid(value: object, error: str) -> None:
        try:
            parsed = UUID(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if parsed is None or not parsed.int or str(parsed) != value:
            raise InputBlocked(error)

    def _time(self) -> int:
        now = self._clock()
        if type(now) is not int or now < self._last_ns or now < 0:
            raise InputBlocked("dispatch_clock_invalid_or_regressed")
        self._last_ns = now
        return now

    def _claim(self, operation: str, request_id: str, deadline_ns: int,
               validate: Callable[[], bool], events: tuple[_INPUT, ...],
               *, instance: NativeInstance | None = None, own_foreground: bool = False) -> None:
        # The controller owns cancellation/source linearization. This separate
        # lock prevents concurrent callers from dispatching one operation twice.
        # validate must not re-enter this dispatcher or wait on the UI thread.
        with self._lock:
            self._uuid(request_id, "invalid_request_identity")
            if self._request_id is None:
                self._request_id = request_id
            if self._request_id != request_id or operation in self._used or self._unknown:
                raise InputBlocked("input_already_consumed_or_outcome_unknown")
            if instance is not None and self._bound_instance is not None and instance != self._bound_instance:
                raise InputBlocked("native_instance_changed_no_cross_instance_input")
            if type(deadline_ns) is not int or self._time() >= deadline_ns:
                raise InputBlocked("dispatch_deadline_expired")
            # Last live validation follows potentially slow desktop/key queries.
            try:
                idle = self._api.idle_fixed_desktop()
            except Exception:
                raise InputBlocked("desktop_check_raised") from None
            if idle is not True:
                code = getattr(self._api, "last_idle_error_code", None)
                if code not in {"dpi_awareness_mismatch", "desktop_profile_mismatch", "user_keys_active"}:
                    code = "desktop_profile_or_user_input_changed"
                raise InputBlocked(code)
            if own_foreground:
                try:
                    foreground = self._api.own_foreground()
                except Exception:
                    raise InputBlocked("trigger_own_foreground_check_raised") from None
                if foreground is not True:
                    raise InputBlocked("trigger_own_foreground_changed")
            try:
                valid = validate()
            except Exception:
                raise InputBlocked("source_or_native_validation_raised") from None
            if valid is not True:
                raise InputBlocked("source_or_native_validation_failed")
            if self._time() >= deadline_ns:
                raise InputBlocked("dispatch_deadline_expired")
            send = getattr(self._api, "send", None)
            if not callable(send):
                raise InputBlocked("dispatch_send_api_unavailable")
            if instance is not None:
                self._bound_instance = instance
            self._used.add(operation)  # Claim BEFORE the sole external dispatch.
            try:
                self._send_input_calls += 1  # Count attempted calls, including raises.
                try:
                    count = send(events)
                finally:
                    error = getattr(self._api, "last_send_win32_error", None)
                    self._win32_error = error if type(error) is int else None
                self._returned_event_count = count if type(count) is int else None
                if type(count) is not int or count != len(events) or self._time() >= deadline_ns:
                    raise InputOutcomeUnknown("dispatch_partial_or_late")
            except Exception as exc:
                self._unknown = True
                if isinstance(exc, InputOutcomeUnknown):
                    raise
                raise InputOutcomeUnknown("dispatch_raised_no_retry") from None

    def trigger_once(self, request_id: str, trigger_id: str, deadline_ns: int,
                     validate: Callable[[], bool]) -> None:
        with self._operation("trigger"):
            self._uuid(trigger_id, "invalid_trigger_identity")
            self._claim("trigger", request_id, deadline_ns, validate,
                        (_key(18), _key(65), _key(65, up=True), _key(18, up=True)),
                        own_foreground=True)

    def select_once(self, instance: NativeInstance, bbox: tuple[int, int, int, int],
                    request_id: str, deadline_ns: int, validate: Callable[[], bool]) -> None:
        with self._operation("select"):
            if not isinstance(instance, NativeInstance) or not isinstance(bbox, tuple) or len(bbox) != 4 or any(type(v) is not int for v in bbox):
                raise InputBlocked("complete_native_instance_and_bbox_required")
            left, top, right, bottom = bbox
            if not (0 <= left < right < current_profile().width and 0 <= top < bottom < current_profile().height) or any(v % 2 for v in bbox):
                raise InputBlocked("selection_outside_even_fixed_profile")
            # Requested coordinates never establish actual native selection.
            self._claim("select", request_id, deadline_ns, validate,
                        (_mouse(left, top, 0), _mouse(left, top, 2),
                         _mouse(right, bottom, 0), _mouse(right, bottom, 4)), instance=instance)

    def click_once(self, instance: NativeInstance, permit: CommitPermit,
                   deadline_ns: int, validate: Callable[[], bool]) -> None:
        with self._operation("commit"):
            if not isinstance(instance, NativeInstance) or not isinstance(permit, CommitPermit):
                raise InputBlocked("bound_native_instance_and_consumed_commit_permit_required")
            left, top, right, bottom = permit.candidate_bbox_physical
            if not (0 <= left < right <= current_profile().width and 0 <= top < bottom <= current_profile().height):
                raise InputBlocked("candidate_outside_fixed_profile")
            x, y = (left+right)//2, (top+bottom)//2
            self._claim("commit", permit.request_id, deadline_ns, validate,
                        (_mouse(x, y, 0), _mouse(x, y, 2), _mouse(x, y, 4)), instance=instance)

    def cancel_once(self, instance: NativeInstance, request_id: str,
                    deadline_ns: int, validate: Callable[[], bool]) -> None:
        with self._operation("cleanup"):
            if not isinstance(instance, NativeInstance):
                raise InputBlocked("complete_native_instance_required")
            # UNKNOWN latches all further input, including cleanup input.
            self._claim("cleanup", request_id, deadline_ns, validate,
                        (_key(27), _key(27, up=True)), instance=instance)
