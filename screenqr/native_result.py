"""Bounded new WeChatAppEx window correlation, never business success.

Only an exact current-user APPDATA host path is inspected. Titles are hashed in
memory; no title text, pixels, input, activation or close operation is returned
or performed. A login-only observer may inspect a fixed camera restriction in
one proven new foreground host. Raw page text is never returned or retained.
Importing this module does not load/query Windows.
An existing host can open a new H5 tab without creating a top-level window: that
case stays NOT_READY and needs a separate, future UIA tab/navigation observer.

The time/identity checks bind an observation to a consumed CommitPermit. They do
not prove that the input was delivered, that the request caused the new window,
or that its page loaded successfully. A title hash is an optional fixture check,
never a substitute for a unique, fresh, complete foreground window instance.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import hashlib
import ntpath
import os
import re
import sys
import threading
import time
from typing import Callable, Literal
from uuid import UUID

from .handoff import CommitPermit
from .native_identity import _Api, _MetadataError, PROCESS_QUERY_LIMITED_INFORMATION

ResultStatus = Literal["NEW_WINDOW_OBSERVED", "CAMERA_REQUIRED", "NOT_READY", "AMBIGUOUS", "STALE", "REJECTED"]
Rect = tuple[int, int, int, int]


def _canonical_uuid(value: object) -> bool:
    try:
        parsed = UUID(value) if isinstance(value, str) else None
        return bool(parsed and parsed.int and str(parsed) == value)
    except ValueError:
        return False


def _integer(value: object, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _path(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value))


def host_executable_path() -> str:
    """Resolve only the current user's configured host; never search other users."""
    appdata = os.environ.get("APPDATA", "")
    if not appdata or not ntpath.isabs(appdata):
        raise ValueError("current_user_appdata_unavailable")
    return ntpath.join(appdata, "Tencent", "xwechat", "xplugin", "plugins", "RadiumWMPF", "WeChatAppEx.exe")


@dataclass(frozen=True, slots=True)
class ResultWindowIdentity:
    hwnd: int
    pid: int
    process_created_filetime_100ns: int
    executable_path: str
    window_class: str
    window_rect: Rect
    visible: bool
    title_sha256: str

    def lifetime_key(self) -> tuple:
        # Changing an old window's title, visibility or position cannot make it
        # new. Same-process/same-class HWND reuse is conservatively not accepted.
        return (self.hwnd, self.pid, self.process_created_filetime_100ns,
                _path(self.executable_path), self.window_class)


@dataclass(frozen=True, slots=True)
class ResultWindowEnumeration:
    windows: tuple[ResultWindowIdentity, ...]
    foreground_before: int
    foreground_after: int
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NativeResultBaseline:
    request_id: str
    frame_id: str
    evidence_id: str
    expected_executable: str
    windows: tuple[ResultWindowIdentity, ...]
    started_ns: int
    finished_ns: int
    deadline_ns: int | None
    foreground_before: int
    foreground_after: int
    _observer_proof: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class NativeResultObservation:
    status: ResultStatus
    reason: str
    request_id: str | None = None
    frame_id: str | None = None
    baseline_evidence_id: str | None = None
    dispatch_id: str | None = None
    consumed_ns: int | None = None
    started_ns: int | None = None
    finished_ns: int | None = None
    elapsed_since_consumed_ns: int | None = None
    candidate: ResultWindowIdentity | None = None
    baseline: NativeResultBaseline | None = None
    business_success: bool = False
    reused_host_tab_supported: bool = False
    progress_sha256: str | None = None


def _valid_enumeration(data: object, expected: str) -> bool:
    if (not isinstance(data, ResultWindowEnumeration) or not isinstance(data.windows, tuple)
            or not isinstance(data.errors, tuple) or data.errors
            or not _integer(data.foreground_before, 1) or not _integer(data.foreground_after, 1)):
        return False
    seen: set[int] = set()
    for window in data.windows:
        if (not isinstance(window, ResultWindowIdentity)
                or not all(_integer(v, 1) for v in (window.hwnd, window.pid, window.process_created_filetime_100ns))
                or not isinstance(window.executable_path, str) or _path(window.executable_path) != _path(expected)
                or not isinstance(window.window_class, str) or not window.window_class
                or not isinstance(window.visible, bool)
                or not isinstance(window.title_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", window.title_sha256) is None
                or not isinstance(window.window_rect, tuple) or len(window.window_rect) != 4
                or not all(isinstance(v, int) and not isinstance(v, bool) and -(2**31) <= v < 2**31 for v in window.window_rect)
                or window.window_rect[2] < window.window_rect[0] or window.window_rect[3] < window.window_rect[1]
                or window.hwnd in seen):
            return False
        seen.add(window.hwnd)
    return True


def _enumerate_host_windows(expected: str) -> ResultWindowEnumeration:
    """Enumerate target metadata only. All handles originate in EnumWindows."""
    if sys.platform != "win32":
        return ResultWindowEnumeration((), 0, 0, ("windows_required",))
    windows: list[ResultWindowIdentity] = []
    errors: list[str] = []
    try:
        api = _Api()
        api.user32.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
        api.user32.GetWindowTextLengthW.restype = ctypes.c_int
        context = api.user32.GetThreadDpiAwarenessContext()
        if not context or int(api.user32.GetAwarenessFromDpiAwarenessContext(context)) != 2:
            return ResultWindowEnumeration((), 0, 0, ("caller_must_already_be_per_monitor_dpi_aware",))
        before = int(api.user32.GetForegroundWindow() or 0)
    except (AttributeError, OSError):
        return ResultWindowEnumeration((), 0, 0, ("read_only_win32_api_unavailable",))

    def title_hash(hwnd: int) -> str:
        ctypes.set_last_error(0)
        length = int(api.user32.GetWindowTextLengthW(hwnd))
        if length < 0 or length > 4096 or (not length and ctypes.get_last_error()):
            raise _MetadataError("host_title_length_unavailable_or_over_limit")
        buffer = ctypes.create_unicode_buffer(length + 2)
        ctypes.set_last_error(0)
        copied = int(api.user32.GetWindowTextW(hwnd, buffer, len(buffer)))
        if copied > length or (not copied and ctypes.get_last_error()):
            raise _MetadataError("host_title_changed_or_unavailable")
        return hashlib.sha256(buffer.value.encode("utf-8")).hexdigest()

    seen: set[int] = set()

    def visit(raw_hwnd: int, unused: int) -> bool:
        try:
            hwnd = int(raw_hwnd or 0)
            if not hwnd or hwnd in seen:
                return True
            seen.add(hwnd)
            try:
                pid = api.process_id(hwnd)
            except _MetadataError:
                if hwnd == before:
                    errors.append("foreground_process_unavailable")
                return True
            process = api.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not process:
                if hwnd == before:
                    errors.append("foreground_process_query_unavailable")
                return True
            try:
                path = api.image_path(process)
                if path is None:
                    if hwnd == before:
                        errors.append("foreground_process_path_unavailable")
                    return True
                if _path(path) != _path(expected):
                    return True
                created = api.creation_time(process)
                visible = bool(api.user32.IsWindowVisible(hwnd))
                rect, window_class, digest = api.window_rect(hwnd), api.window_class(hwnd), title_hash(hwnd)
                if (not api.user32.IsWindow(hwnd) or api.process_id(hwnd) != pid
                        or api.creation_time(process) != created or api.image_path(process) != path
                        or bool(api.user32.IsWindowVisible(hwnd)) != visible
                        or api.window_rect(hwnd) != rect or api.window_class(hwnd) != window_class
                        or title_hash(hwnd) != digest):
                    raise _MetadataError("host_instance_changed_during_query")
                windows.append(ResultWindowIdentity(hwnd, pid, created, expected, window_class, rect, visible, digest))
            except _MetadataError as exc:
                errors.append(str(exc))
            finally:
                if not api.kernel32.CloseHandle(process):
                    errors.append("process_query_handle_close_failed")
            return True
        except Exception:
            errors.append("host_enumeration_callback_failed")
            return False

    callback = api.enum_callback_type(visit)
    ctypes.set_last_error(0)
    if not api.user32.EnumWindows(callback, 0):
        errors.append(f"enum_windows_incomplete_winerror_{ctypes.get_last_error()}")
    after = int(api.user32.GetForegroundWindow() or 0)
    foreground = next((window for window in windows if window.hwnd == after), None)
    if foreground is not None:
        # Reopen the already-enumerated foreground instance at the end of the
        # whole query, so a numeric HWND alone never completes the observation.
        process = None
        try:
            pid = api.process_id(foreground.hwnd)
            process = api.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if (not process or pid != foreground.pid
                    or api.creation_time(process) != foreground.process_created_filetime_100ns
                    or _path(api.image_path(process) or "") != _path(foreground.executable_path)
                    or not api.user32.IsWindow(foreground.hwnd)
                    or bool(api.user32.IsWindowVisible(foreground.hwnd)) != foreground.visible
                    or api.window_rect(foreground.hwnd) != foreground.window_rect
                    or api.window_class(foreground.hwnd) != foreground.window_class
                    or title_hash(foreground.hwnd) != foreground.title_sha256
                    or api.process_id(foreground.hwnd) != foreground.pid):
                errors.append("foreground_host_instance_changed_at_query_end")
        except _MetadataError:
            errors.append("foreground_host_identity_recheck_failed")
        finally:
            if process and not api.kernel32.CloseHandle(process):
                errors.append("foreground_process_query_handle_close_failed")
    final_foreground = int(api.user32.GetForegroundWindow() or 0)
    if final_foreground != after:
        errors.append("foreground_changed_during_final_identity_check")
    return ResultWindowEnumeration(tuple(windows), before, final_foreground, tuple(errors))


class NativeResultObserver:
    """One live baseline and one consumed dispatch, with read-only polling.

    Injected enumerators/clocks are a test seam, not evidence of a Windows run.
    The caller must supply the actual CommitPermit after consuming the gate.
    No result here permits input or closes a window. Deadline is fixed when the
    baseline is acquired; callers cannot extend it while polling.
    """

    def __init__(self, *, enumerator: Callable[[str], ResultWindowEnumeration] | None = None,
                 clock: Callable[[], int] = time.monotonic_ns,
                 inspect_login_notice: bool = False, notice_reader=None,
                 max_observation_ns: int | None = None) -> None:
        if type(inspect_login_notice) is not bool or (notice_reader is not None and not callable(notice_reader)):
            raise TypeError("typed_notice_scope_and_optional_reader_required")
        self._enumerator = enumerator or _enumerate_host_windows
        if max_observation_ns is not None and (type(max_observation_ns) is not int or not 0<max_observation_ns<=10_000_000_000):
            raise ValueError('result_observation_budget_invalid')
        self._max_observation_ns=max_observation_ns
        self._inspect_login_notice = inspect_login_notice
        self._notice_reader = notice_reader
        self._notice_candidate = None
        self._clock = clock
        self._lock = threading.Lock()
        self._proof = object()
        self._baseline: NativeResultBaseline | None = None
        self._dispatch: tuple[str, int] | None = None
        self._completed = False
        self._last_ns = -1

    def _now(self) -> int:
        value = self._clock()
        if not _integer(value) or value < self._last_ns:
            raise ValueError("monotonic_clock_invalid_or_moved_backwards")
        self._last_ns = value
        return value

    def capture_baseline(self, request_id: str, frame_id: str, evidence_id: str, *, deadline_ns: int | None) -> NativeResultObservation:
        with self._lock:
            self._baseline, self._dispatch, self._completed = None, None, False
            self._notice_candidate = None
            if not all(_canonical_uuid(v) for v in (request_id, frame_id, evidence_id)) or (deadline_ns is not None and not _integer(deadline_ns, 1)):
                return NativeResultObservation("REJECTED", "canonical_request_frame_evidence_and_deadline_required")
            try:
                expected, start = host_executable_path(), self._now()
                if deadline_ns is not None and start >= deadline_ns:
                    return NativeResultObservation("STALE", "baseline_deadline_elapsed")
                data = self._enumerator(expected)
                end = self._now()
            except Exception:
                return NativeResultObservation("REJECTED", "baseline_acquisition_failed")
            if deadline_ns is not None and end >= deadline_ns:
                return NativeResultObservation("STALE", "baseline_acquisition_exceeded_deadline")
            if not _valid_enumeration(data, expected):
                return NativeResultObservation("REJECTED", "baseline_metadata_incomplete")
            if data.foreground_before != data.foreground_after:
                return NativeResultObservation("STALE", "foreground_changed_during_baseline")
            baseline = NativeResultBaseline(request_id, frame_id, evidence_id, expected, data.windows,
                start, end, deadline_ns, data.foreground_before, data.foreground_after, self._proof)
            self._baseline = baseline
            return NativeResultObservation("NOT_READY", "baseline_captured_no_result_claim", request_id,
                frame_id, evidence_id, started_ns=start, finished_ns=end, baseline=baseline)

    def observe_new_window(self, baseline: NativeResultBaseline, permit: CommitPermit, *, deadline_ns: int | None,
                           expected_title_sha256: str | None = None) -> NativeResultObservation:
        with self._lock:
            if (not isinstance(baseline, NativeResultBaseline) or baseline is not self._baseline
                    or baseline._observer_proof is not self._proof or self._completed):
                return NativeResultObservation("REJECTED", "baseline_not_current_or_already_observed")
            if (not isinstance(permit, CommitPermit) or permit.request_id != baseline.request_id
                    or permit.frame_id != baseline.frame_id or not _canonical_uuid(permit.dispatch_id)
                    or not _integer(permit.consumed_ns) or permit.consumed_ns <= baseline.finished_ns
                    or deadline_ns != baseline.deadline_ns
                    or (expected_title_sha256 is not None and (not isinstance(expected_title_sha256, str)
                        or re.fullmatch(r"[0-9a-f]{64}", expected_title_sha256) is None))):
                return NativeResultObservation("REJECTED", "commit_request_frame_time_or_fixed_deadline_mismatch")
            binding = (permit.dispatch_id, permit.consumed_ns)
            if self._dispatch is not None and self._dispatch != binding:
                return NativeResultObservation("REJECTED", "baseline_already_bound_to_different_dispatch")
            self._dispatch = binding

            # 只读观察从已消耗的一次提交开始计时，不重置基线或授予新输入。
            if self._max_observation_ns is not None:
                bounded=permit.consumed_ns+self._max_observation_ns
                deadline_ns=bounded if deadline_ns is None else min(deadline_ns,bounded)

            progress_sha256 = None

            def result(status: ResultStatus, reason: str, start: int | None = None,
                       end: int | None = None, candidate: ResultWindowIdentity | None = None) -> NativeResultObservation:
                return NativeResultObservation(status, reason, baseline.request_id, baseline.frame_id,
                    baseline.evidence_id, permit.dispatch_id, permit.consumed_ns, start, end,
                    end-permit.consumed_ns if end is not None else None, candidate,
                    progress_sha256=progress_sha256)

            try:
                start = self._now()
                if start <= permit.consumed_ns:
                    return result("STALE", "observation_not_after_consumed_commit", start)
                if deadline_ns is not None and start >= deadline_ns:
                    return result("STALE", "result_deadline_elapsed", start)
                data = self._enumerator(baseline.expected_executable)
                end = self._now()
            except Exception:
                return result("REJECTED", "result_acquisition_failed")
            if deadline_ns is not None and end >= deadline_ns:
                return result("STALE", "result_acquisition_exceeded_deadline", start, end)
            if not _valid_enumeration(data, baseline.expected_executable):
                return result("REJECTED", "result_metadata_incomplete", start, end)
            if data.foreground_before != data.foreground_after:
                return result("STALE", "foreground_changed_during_result_query", start, end)
            # Hash only already-authorized window metadata. No title text,
            # page pixels or decoded content is exposed by this progress key.
            progress_sha256 = hashlib.sha256(repr((data.foreground_before,
                data.foreground_after, tuple(sorted((window.hwnd, window.pid,
                    window.process_created_filetime_100ns, window.window_rect,
                    window.visible, window.title_sha256) for window in data.windows))))
                .encode("utf-8")).hexdigest()
            old = {window.lifetime_key() for window in baseline.windows}
            fresh = tuple(window for window in data.windows if window.visible and window.lifetime_key() not in old
                          and window.window_rect[2] > window.window_rect[0] and window.window_rect[3] > window.window_rect[1])
            if len(fresh) > 1:
                return result("AMBIGUOUS", "multiple_new_visible_host_instances", start, end)
            if not fresh:
                return result("NOT_READY", "no_new_top_level_host_instance_reused_tabs_unobserved", start, end)
            candidate = fresh[0]
            if candidate.hwnd != data.foreground_before:
                return result("NOT_READY", "new_host_instance_not_foreground", start, end)
            if expected_title_sha256 is not None and candidate.title_sha256 != expected_title_sha256:
                return result("REJECTED", "new_instance_does_not_match_fixture_title_hash", start, end)
            if self._inspect_login_notice:
                if self._notice_candidate is not None and candidate.lifetime_key() != self._notice_candidate:
                    return result("REJECTED", "login_result_window_changed", start, end)
                self._notice_candidate = candidate.lifetime_key()
                try:
                    from .native_notice import NoticeResult, read_notice

                    notice = (self._notice_reader or read_notice)(candidate.hwnd)
                    after = self._enumerator(baseline.expected_executable)
                    end = self._now()
                except Exception:
                    return result("REJECTED", "login_notice_acquisition_failed", start, end)
                if deadline_ns is not None and end >= deadline_ns:
                    return result("STALE", "result_acquisition_exceeded_deadline", start, end)
                if (not _valid_enumeration(after, baseline.expected_executable)
                        or after.foreground_before != candidate.hwnd or after.foreground_after != candidate.hwnd
                        or tuple(window for window in after.windows if window.hwnd == candidate.hwnd) != (candidate,)
                        or tuple(window for window in after.windows if window.visible
                            and window.lifetime_key() not in old) != (candidate,)):
                    return result("STALE", "login_notice_window_changed_during_read", start, end)
                if (not isinstance(notice, NoticeResult)
                        or notice.status not in ("CAMERA_REQUIRED", "UNCONFIRMED", "UNAVAILABLE")
                        or (notice.progress_sha256 is not None and (not isinstance(notice.progress_sha256, str)
                            or re.fullmatch(r"[0-9a-f]{64}", notice.progress_sha256) is None))):
                    return result("REJECTED", "login_notice_reader_invalid_result", start, end)
                progress_sha256 = hashlib.sha256((progress_sha256 + (notice.progress_sha256 or ""))
                                                .encode("ascii")).hexdigest()
                if notice.status == "CAMERA_REQUIRED":
                    self._completed = True
                    return result("CAMERA_REQUIRED", "login_requires_camera_scan", start, end, candidate)
                if notice.status == "UNCONFIRMED":
                    return result("NOT_READY", "login_notice_not_confirmed", start, end, candidate)
                # Provider unavailable is an honest limit, not a camera verdict.
                self._completed = True
                return result("NEW_WINDOW_OBSERVED", "login_notice_reader_unavailable", start, end, candidate)
            self._completed = True
            return result("NEW_WINDOW_OBSERVED", "unique_new_foreground_host_metadata_only", start, end, candidate)
