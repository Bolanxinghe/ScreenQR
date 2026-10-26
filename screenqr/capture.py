"""Physical desktop pixel capture. No frame is written to disk or transmitted."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys

from PIL import Image, ImageGrab

from .frozen_frame import (
    FrozenFrame, MonitorDescriptor, TopologyDescriptor, TOPOLOGY_KIND,
    clip_capture_bbox, freeze_image,
)


def enable_dpi_awareness() -> None:
    """Call before creating Tk windows; coordinates then refer to physical pixels."""
    if sys.platform != "win32":
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        fn = user32.SetProcessDpiAwarenessContext
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = wintypes.BOOL
        if fn(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
        # A manifest may already have set process awareness. Do not downgrade it.
        if ctypes.get_last_error() == 5:
            return
    except AttributeError:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()


def get_virtual_bounds() -> tuple[int, int, int, int]:
    if sys.platform != "win32":
        raise OSError("ScreenQR desktop capture requires Windows 10 or Windows 11")
    user32 = ctypes.WinDLL("user32")
    return tuple(user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))


def capture_region(bbox: tuple[int, int, int, int]) -> Image.Image:
    """Capture the visible region, including layered application windows.

    Bounds can be negative on monitors left/above the primary monitor. Requests
    outside the virtual desktop are clipped rather than padded with stale pixels.
    """
    left, top, right, bottom = (int(value) for value in bbox)
    x, y, width, height = get_virtual_bounds()
    left, top = max(left, x), max(top, y)
    right, bottom = min(right, x + width), min(bottom, y + height)
    if right <= left or bottom <= top:
        raise ValueError("取景框位于屏幕之外，请将它移回屏幕。")
    return ImageGrab.grab(
        bbox=(left, top, right, bottom), all_screens=True,
        include_layered_windows=True,
    ).convert("RGB")


def exclude_from_capture(hwnd: int) -> bool:
    """Exclude one of this process's own top-level windows from screen capture.

    On older Windows versions exclusion can produce a black rectangle. Return
    False instead; the transparent viewfinder still permits reading its center.
    This is a visual recursion control, not a security guarantee.
    """
    if sys.platform != "win32" or sys.getwindowsversion().build < 19041:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    top = user32.GetAncestor(wintypes.HWND(hwnd), 2) or hwnd  # GA_ROOT
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId(wintypes.HWND(top), ctypes.byref(pid))
    import os
    if pid.value != os.getpid():
        return False
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    return bool(user32.SetWindowDisplayAffinity(wintypes.HWND(top), 0x11))


# Metadata-backed acquisition is intentionally separate from capture_region:
# legacy callers retain their existing behavior and Image return type.
class DisplayTopologyError(RuntimeError):
    """Physical display facts are unavailable, inconsistent or changed."""


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _POINTL(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _DEVMODE_DISPLAY(ctypes.Structure):
    _fields_ = [
        ("dmPosition", _POINTL),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
    ]


class _DEVMODE_PRINTER(ctypes.Structure):
    _fields_ = [
        (name, wintypes.SHORT) for name in (
            "dmOrientation", "dmPaperSize", "dmPaperLength", "dmPaperWidth",
            "dmScale", "dmCopies", "dmDefaultSource", "dmPrintQuality",
        )
    ]


class _DEVMODE_UNION1(ctypes.Union):
    _fields_ = [
        ("display", _DEVMODE_DISPLAY),
        ("printer", _DEVMODE_PRINTER),
    ]


class _DEVMODE_UNION2(ctypes.Union):
    _fields_ = [("dmDisplayFlags", wintypes.DWORD), ("dmNup", wintypes.DWORD)]


class _DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("mode", _DEVMODE_UNION1),
        ("dmColor", wintypes.SHORT),
        ("dmDuplex", wintypes.SHORT),
        ("dmYResolution", wintypes.SHORT),
        ("dmTTOption", wintypes.SHORT),
        ("dmCollate", wintypes.SHORT),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("flags", _DEVMODE_UNION2),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


class _DisplayApi:
    """Read-only bindings; construction occurs only when an API is called."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise DisplayTopologyError("Windows display metadata is required")
        if ctypes.sizeof(_MONITORINFOEXW) != 104 or ctypes.sizeof(_DEVMODEW) != 220:
            raise DisplayTopologyError("Unsupported Win32 metadata structure layout")
        try:
            self.user = ctypes.WinDLL("user32", use_last_error=True)
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.monitor_callback_type = ctypes.WINFUNCTYPE(
                wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
            )
            signatures = (
                (self.user, "EnumDisplayMonitors", [
                    wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                    self.monitor_callback_type, wintypes.LPARAM,
                ], wintypes.BOOL),
                (self.user, "GetMonitorInfoW", [
                    wintypes.HANDLE, ctypes.POINTER(_MONITORINFOEXW),
                ], wintypes.BOOL),
                (self.user, "EnumDisplaySettingsW", [
                    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DEVMODEW),
                ], wintypes.BOOL),
                (self.user, "GetSystemMetrics", [ctypes.c_int], ctypes.c_int),
                (self.user, "GetThreadDpiAwarenessContext", [], wintypes.HANDLE),
                (self.user, "GetWindowDpiAwarenessContext", [wintypes.HWND], wintypes.HANDLE),
                (self.user, "GetAwarenessFromDpiAwarenessContext", [wintypes.HANDLE], ctypes.c_int),
                (self.user, "AreDpiAwarenessContextsEqual", [
                    wintypes.HANDLE, wintypes.HANDLE,
                ], wintypes.BOOL),
                (self.user, "IsWindow", [wintypes.HWND], wintypes.BOOL),
                (self.user, "IsIconic", [wintypes.HWND], wintypes.BOOL),
                (self.user, "GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
                (self.user, "GetWindowThreadProcessId", [
                    wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
                ], wintypes.DWORD),
                (self.user, "MonitorFromWindow", [
                    wintypes.HWND, wintypes.DWORD,
                ], wintypes.HANDLE),
                (self.user, "GetDpiForWindow", [wintypes.HWND], wintypes.UINT),
                (self.kernel, "GetCurrentProcessId", [], wintypes.DWORD),
            )
            for library, name, args, result in signatures:
                function = getattr(library, name)
                function.argtypes, function.restype = args, result
        except (AttributeError, OSError):
            raise DisplayTopologyError("Required read-only Windows display APIs are unavailable") from None

    def require_thread_pm(self) -> int:
        context = self.user.GetThreadDpiAwarenessContext()
        if not context or self.user.GetAwarenessFromDpiAwarenessContext(context) != 2:
            raise DisplayTopologyError("Capture thread must already be per-monitor DPI aware")
        return int(context)

    def virtual_bbox(self) -> tuple[int, int, int, int]:
        x, y, width, height = (int(self.user.GetSystemMetrics(i)) for i in (76, 77, 78, 79))
        if width <= 0 or height <= 0:
            raise DisplayTopologyError("Virtual desktop bounds are unavailable")
        return x, y, x + width, y + height

    def own_window_dpi_evidence(self, hwnd: int) -> tuple[int, int]:
        """Read only our own existing PM-aware top-level window.

        GetDpiForMonitor is deliberately not called: Microsoft documents that it
        should not be used from a per-monitor-aware thread. GetDpiForWindow on a
        verified PM-aware window reports its monitor's effective DPI. A monitor
        lacking such explicit own-window evidence is unsupported, never guessed.
        """
        if not self.user.IsWindow(hwnd):
            raise DisplayTopologyError("DPI reference window does not exist")
        pid = wintypes.DWORD()
        if not self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            raise DisplayTopologyError("DPI reference owner is unavailable")
        if pid.value != self.kernel.GetCurrentProcessId():
            raise DisplayTopologyError("DPI references must belong to this process")
        if int(self.user.GetAncestor(hwnd, 2) or 0) != hwnd:  # GA_ROOT
            raise DisplayTopologyError("DPI references must be top-level windows")
        if self.user.IsIconic(hwnd):
            raise DisplayTopologyError("Minimized DPI reference windows are unsupported")
        context = self.user.GetWindowDpiAwarenessContext(hwnd)
        if not context or self.user.GetAwarenessFromDpiAwarenessContext(context) != 2:
            raise DisplayTopologyError("DPI reference window must be per-monitor DPI aware")
        monitor = int(self.user.MonitorFromWindow(hwnd, 0) or 0)  # MONITOR_DEFAULTTONULL
        dpi = int(self.user.GetDpiForWindow(hwnd))
        if not monitor or dpi <= 0:
            raise DisplayTopologyError("Explicit reference-window monitor DPI is unavailable")
        # Recheck this evidence; HWND/PID, context, monitor and DPI are queried
        # independently and do not form an atomic transaction.
        final_pid = wintypes.DWORD()
        if (
            not self.user.IsWindow(hwnd)
            or not self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(final_pid))
            or final_pid.value != pid.value
            or int(self.user.GetAncestor(hwnd, 2) or 0) != hwnd
            or self.user.IsIconic(hwnd)
            or int(self.user.MonitorFromWindow(hwnd, 0) or 0) != monitor
            or int(self.user.GetDpiForWindow(hwnd)) != dpi
        ):
            raise DisplayTopologyError("DPI reference window changed during query")
        final_context = self.user.GetWindowDpiAwarenessContext(hwnd)
        if not final_context or not self.user.AreDpiAwarenessContextsEqual(context, final_context):
            raise DisplayTopologyError("DPI reference context changed during query")
        return monitor, dpi


def _validate_dpi_reference_hwnds(value: object) -> tuple[int, ...]:
    if (
        not isinstance(value, tuple) or not value
        or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("dpi_reference_hwnds must be a nonempty tuple of unique own-window handles")
    if len(value) > 64:
        raise ValueError("Too many DPI reference windows")
    return value


def _read_display_topology(
    api: _DisplayApi, dpi_reference_hwnds: tuple[int, ...],
) -> TopologyDescriptor:
    context_before = api.require_thread_pm()
    virtual_before = api.virtual_bbox()
    count_before = int(api.user.GetSystemMetrics(80))  # SM_CMONITORS
    if not 0 < count_before <= 64:
        raise DisplayTopologyError("Display-monitor count is unavailable or unsupported")
    references_before = {
        hwnd: api.own_window_dpi_evidence(hwnd) for hwnd in dpi_reference_hwnds
    }
    monitor_dpis: dict[int, int] = {}
    for monitor, dpi in references_before.values():
        if monitor in monitor_dpis and monitor_dpis[monitor] != dpi:
            raise DisplayTopologyError("Own-window DPI evidence disagrees on one monitor")
        monitor_dpis[monitor] = dpi
    descriptors: list[MonitorDescriptor] = []
    handles: set[int] = set()
    errors: list[str] = []

    def visit(raw_monitor: int, unused_dc: int, callback_rect: object, unused_data: int) -> bool:
        try:
            monitor = int(raw_monitor or 0)
            if not monitor or monitor in handles:
                raise DisplayTopologyError("Invalid or duplicate monitor enumeration")
            handles.add(monitor)
            info = _MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(info)
            if not api.user.GetMonitorInfoW(monitor, ctypes.byref(info)):
                raise DisplayTopologyError("GetMonitorInfoW failed")
            rect = (
                int(info.rcMonitor.left), int(info.rcMonitor.top),
                int(info.rcMonitor.right), int(info.rcMonitor.bottom),
            )
            if not callback_rect:
                raise DisplayTopologyError("Monitor enumeration rectangle is unavailable")
            callback_value = callback_rect.contents
            if rect != (
                int(callback_value.left), int(callback_value.top),
                int(callback_value.right), int(callback_value.bottom),
            ):
                raise DisplayTopologyError("Monitor rectangle changed during enumeration")
            if info.dwFlags & ~1:  # MONITORINFOF_PRIMARY is the only defined flag.
                raise DisplayTopologyError("Unknown monitor-info flags")
            name = str(info.szDevice)
            if not name:
                raise DisplayTopologyError("Display device token is unavailable")
            mode = _DEVMODEW()
            mode.dmSize = ctypes.sizeof(mode)
            mode.dmDriverExtra = 0
            if not api.user.EnumDisplaySettingsW(name, 0xFFFFFFFF, ctypes.byref(mode)):
                raise DisplayTopologyError("Current physical display mode is unavailable")
            # DM_POSITION | DM_DISPLAYORIENTATION | DM_PELSWIDTH | DM_PELSHEIGHT.
            # Missing fields are unknown, not assumed to be position/rotation 0.
            required = 0x20 | 0x80 | 0x80000 | 0x100000
            if mode.dmFields & required != required:
                raise DisplayTopologyError("Current display position/rotation/size fields are unknown")
            orientation = int(mode.mode.display.dmDisplayOrientation)
            if orientation not in (0, 1, 2, 3):
                raise DisplayTopologyError("Display rotation is unknown")
            mode_rect = (
                int(mode.mode.display.dmPosition.x),
                int(mode.mode.display.dmPosition.y),
                int(mode.mode.display.dmPosition.x + mode.dmPelsWidth),
                int(mode.mode.display.dmPosition.y + mode.dmPelsHeight),
            )
            if mode_rect != rect:
                raise DisplayTopologyError("Physical display mode differs from monitor bounds")
            if monitor not in monitor_dpis:
                raise DisplayTopologyError("A monitor lacks explicit own-window DPI evidence")
            dpi = monitor_dpis[monitor]
            descriptors.append(MonitorDescriptor(
                device_name=name, physical_bbox=rect, dpi_x=dpi, dpi_y=dpi,
                primary=bool(info.dwFlags & 1), rotation_degrees=orientation * 90,
            ))
            return True
        except DisplayTopologyError as exc:
            errors.append(str(exc))
            return False
        except Exception:
            errors.append("Monitor-enumeration callback failed")
            return False

    callback = api.monitor_callback_type(visit)  # Keep alive across EnumDisplayMonitors.
    completed = bool(api.user.EnumDisplayMonitors(None, None, callback, 0))
    if not completed or errors:
        raise DisplayTopologyError(errors[0] if errors else "Monitor enumeration was incomplete")
    if len(descriptors) != count_before or set(monitor_dpis) - handles:
        raise DisplayTopologyError("Monitor enumeration and explicit DPI references are inconsistent")
    if {
        hwnd: api.own_window_dpi_evidence(hwnd) for hwnd in dpi_reference_hwnds
    } != references_before:
        raise DisplayTopologyError("Own-window DPI evidence changed during topology query")
    if api.virtual_bbox() != virtual_before or int(api.user.GetSystemMetrics(80)) != count_before:
        raise DisplayTopologyError("Display geometry changed during topology query")
    context_after = api.require_thread_pm()
    if not api.user.AreDpiAwarenessContextsEqual(context_before, context_after):
        raise DisplayTopologyError("Capture-thread DPI context changed during topology query")
    try:
        return TopologyDescriptor(
            kind=TOPOLOGY_KIND, virtual_bbox=virtual_before,
            monitors=tuple(sorted(descriptors, key=lambda m: m.device_name.casefold())),
        )
    except (TypeError, ValueError) as exc:
        raise DisplayTopologyError("Windows display facts failed topology validation") from exc


def read_display_topology(
    *, dpi_reference_hwnds: tuple[int, ...],
) -> TopologyDescriptor:
    """Query physical layout and effective DPI without creating/changing windows.

    Supply existing PM-aware top-level HWNDs from this process. Every enumerated
    monitor requires corresponding real window DPI evidence. Other layouts fail
    explicitly; this is not a claim of completed multi-monitor compatibility.
    Queries are bracketed and validated but are not an atomic Windows snapshot.
    """
    references = _validate_dpi_reference_hwnds(dpi_reference_hwnds)
    return _read_display_topology(_DisplayApi(), references)


def capture_region_with_metadata(
    requested_bbox: tuple[int, int, int, int],
    *,
    generation: int,
    frame_id: str,
    dpi_reference_hwnds: tuple[int, ...],
) -> FrozenFrame:
    """Acquire and freeze actual desktop pixels independently of QR decoding.

    actual_bbox is selected ONCE against the before-topology and that exact tuple
    goes to ImageGrab. Later clipping calculations are pure consistency checks,
    never a fresh acquisition rectangle. Caller owns UUID uniqueness and must
    recheck generation/session freshness when its worker result is delivered.

    ImageGrab may use transient desktop-sized internal storage on Windows; only
    its explicit crop becomes this in-memory RGB8 frame. Nothing is saved or
    uploaded. Protected/occluded pixels are not proven readable by metadata.
    """
    from uuid import UUID
    import time

    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("generation must be a nonnegative integer")
    try:
        identifier = UUID(frame_id) if isinstance(frame_id, str) else None
    except (ValueError, AttributeError, TypeError):
        identifier = None
    if identifier is None or identifier.int == 0 or str(identifier) != frame_id:
        raise ValueError("frame_id must be a nonzero canonical UUID string")
    references = _validate_dpi_reference_hwnds(dpi_reference_hwnds)
    api = _DisplayApi()
    context_before = api.require_thread_pm()
    topology_before = _read_display_topology(api, references)
    actual_bbox = clip_capture_bbox(requested_bbox, topology_before.virtual_bbox)
    started = time.monotonic()
    with ImageGrab.grab(
        bbox=actual_bbox, all_screens=True, include_layered_windows=True,
    ) as acquired:
        image = acquired.convert("RGB")
    ended = time.monotonic()
    try:
        topology_after = _read_display_topology(api, references)
        context_after = api.require_thread_pm()
        if not api.user.AreDpiAwarenessContextsEqual(context_before, context_after):
            raise DisplayTopologyError("Capture-thread DPI context changed across acquisition")
        if topology_before.digest() != topology_after.digest():
            raise DisplayTopologyError("Display topology changed during acquisition")
        return freeze_image(
            image, frame_id=frame_id, generation=generation,
            requested_bbox=requested_bbox, actual_bbox=actual_bbox,
            capture_started_monotonic=started, capture_ended_monotonic=ended,
            topology_before=topology_before, topology_after=topology_after,
        )
    finally:
        image.close()

