"""Bounded read-only native payload and border-strip acquisition.

Only an independently enumerated, stably foreground WeChat screenshot client is
sampled. This first profile is deliberately fixed to 4.1.15.13, DPI 192 and a
single 3200x2000 display. Importing performs no Win32 query. No native HWND is
accepted from a caller, no desktop fallback exists, and no pixels are persisted
or returned. The result contains real hashes, geometry and numeric strips only.

This does not decide actual selection extent or authorize input. Separate
native_extent and handoff modules validate boundaries and evidence. The caller
must still discard stale session/stage/generation results. Synchronous Win32
calls cannot be interrupted, but late completion is rejected without retry.
"""
from __future__ import annotations

from .profiles import DEFAULT_PROFILE, current_profile

from collections.abc import Mapping
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import ntpath
import sys
import time
from uuid import UUID, uuid4

from PIL import Image

from .frozen_frame import FrozenFrame
from .native_identity import NativeWindowIdentity, WindowProbeResult, probe_screenshot_window
from .stage import GUARD_PHYSICAL_PX, WHITE_PADDING_PHYSICAL_PX, SUPPORTED_STAGE_PADDING_BY_GUARD

Rect = tuple[int, int, int, int]
EXPECTED_CLASS = DEFAULT_PROFILE.window_class
EXPECTED_DPI = DEFAULT_PROFILE.dpi
EXPECTED_FIXED_FILE_VERSION = DEFAULT_PROFILE.version
EXPECTED_NATIVE_RECT = DEFAULT_PROFILE.screen
EXPECTED_EXECUTABLE = DEFAULT_PROFILE.executable
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
HGDI_ERROR = ctypes.c_void_p(-1).value
# Bounded by the only supported native display, rather than a smaller hidden
# 1MP ceiling that rejected otherwise valid visible stages before acquisition.
MAX_CAPTURE_PIXELS = (EXPECTED_NATIVE_RECT[2] - EXPECTED_NATIVE_RECT[0]) * (EXPECTED_NATIVE_RECT[3] - EXPECTED_NATIVE_RECT[1])
MAX_OBSERVATION_NS = 2_000_000_000
OUTER_NORMAL_PX = 12
INNER_NORMAL_PX = 6
TANGENTIAL_THICKNESS_PX = 8


def planned_capture_pixels(frame: FrozenFrame) -> int:
    """Exact payload plus eight fixed border strips; no pixels or UI read."""
    return frame.width * frame.height + 8 * (OUTER_NORMAL_PX + INNER_NORMAL_PX) * TANGENTIAL_THICKNESS_PX


class _ObserverError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", wintypes.BYTE), ("rgbGreen", wintypes.BYTE),
        ("rgbRed", wintypes.BYTE), ("rgbReserved", wintypes.BYTE),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", _RGBQUAD * 1),
    ]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class _XFORM(ctypes.Structure):
    _fields_ = [
        ("eM11", ctypes.c_float), ("eM12", ctypes.c_float),
        ("eM21", ctypes.c_float), ("eM22", ctypes.c_float),
        ("eDx", ctypes.c_float), ("eDy", ctypes.c_float),
    ]


class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        (name, wintypes.DWORD) for name in (
            "dwSignature", "dwStrucVersion", "dwFileVersionMS",
            "dwFileVersionLS", "dwProductVersionMS", "dwProductVersionLS",
            "dwFileFlagsMask", "dwFileFlags", "dwFileOS", "dwFileType",
            "dwFileSubtype", "dwFileDateMS", "dwFileDateLS",
        )
    ]


class _Gdi:
    """All resource use stays on the calling thread; no async handoff."""

    def __init__(self) -> None:
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.version = ctypes.WinDLL("version", use_last_error=True)
        signatures = (
            (self.user, "GetDC", [wintypes.HWND], wintypes.HDC),
            (self.user, "ReleaseDC", [wintypes.HWND, wintypes.HDC], ctypes.c_int),
            (self.gdi, "CreateCompatibleDC", [wintypes.HDC], wintypes.HDC),
            (self.gdi, "DeleteDC", [wintypes.HDC], wintypes.BOOL),
            (self.gdi, "CreateDIBSection", [
                wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
                ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
            ], wintypes.HANDLE),
            (self.gdi, "SelectObject", [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
            (self.gdi, "DeleteObject", [wintypes.HANDLE], wintypes.BOOL),
            (self.gdi, "BitBlt", [
                wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                wintypes.DWORD,
            ], wintypes.BOOL),
            (self.gdi, "GdiFlush", [], wintypes.BOOL),
            (self.gdi, "GetMapMode", [wintypes.HDC], ctypes.c_int),
            (self.gdi, "GetWindowOrgEx", [wintypes.HDC, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            (self.gdi, "GetViewportOrgEx", [wintypes.HDC, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            (self.gdi, "GetWindowExtEx", [wintypes.HDC, ctypes.POINTER(_SIZE)], wintypes.BOOL),
            (self.gdi, "GetViewportExtEx", [wintypes.HDC, ctypes.POINTER(_SIZE)], wintypes.BOOL),
            (self.gdi, "GetDCOrgEx", [wintypes.HDC, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            (self.gdi, "GetGraphicsMode", [wintypes.HDC], ctypes.c_int),
            (self.gdi, "GetWorldTransform", [wintypes.HDC, ctypes.POINTER(_XFORM)], wintypes.BOOL),
            (self.gdi, "GetLayout", [wintypes.HDC], wintypes.DWORD),
            (self.gdi, "LPtoDP", [wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.c_int], wintypes.BOOL),
            (self.version, "GetFileVersionInfoSizeW", [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (self.version, "GetFileVersionInfoW", [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            (self.version, "VerQueryValueW", [
                ctypes.c_void_p, wintypes.LPCWSTR,
                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT),
            ], wintypes.BOOL),
        )
        for library, name, args, result in signatures:
            function = getattr(library, name)
            function.argtypes, function.restype = args, result

    @staticmethod
    def require(value: object, name: str) -> None:
        if not value:
            # Zero means the API supplied no last-error detail, not success.
            raise _ObserverError(f"{name}_failed_winerror_{ctypes.get_last_error()}")

    def verify_identity_mapping(self, dc: int, *, source: bool) -> None:
        # These are getters only. Do not mutate a possibly class/private target DC.
        if self.gdi.GetMapMode(dc) != 1:  # MM_TEXT
            raise _ObserverError("gdi_mapping_not_mm_text")
        for getter in ("GetWindowOrgEx", "GetViewportOrgEx"):
            point = wintypes.POINT()
            self.require(getattr(self.gdi, getter)(dc, ctypes.byref(point)), getter)
            if (point.x, point.y) != (0, 0):
                raise _ObserverError("gdi_nonzero_logical_origin")
        for getter in ("GetWindowExtEx", "GetViewportExtEx"):
            size = _SIZE()
            self.require(getattr(self.gdi, getter)(dc, ctypes.byref(size)), getter)
            if (size.cx, size.cy) != (1, 1):
                raise _ObserverError("gdi_nonunit_extents")
        origin = wintypes.POINT()
        self.require(self.gdi.GetDCOrgEx(dc, ctypes.byref(origin)), "GetDCOrgEx")
        if source and (origin.x, origin.y) != current_profile().screen[:2]:
            raise _ObserverError("gdi_unknown_device_origin")
        mode = int(self.gdi.GetGraphicsMode(dc))
        if mode == 2:  # GM_ADVANCED: verify the complete world transform.
            transform = _XFORM()
            self.require(self.gdi.GetWorldTransform(dc, ctypes.byref(transform)), "GetWorldTransform")
            if tuple(getattr(transform, f) for f, _ in _XFORM._fields_) != (1, 0, 0, 1, 0, 0):
                raise _ObserverError("gdi_nonidentity_world_transform")
        elif mode != 1:  # GM_COMPATIBLE has no active nonidentity world transform.
            raise _ObserverError("gdi_unknown_graphics_mode")
        if int(self.gdi.GetLayout(dc)) != 0:
            raise _ObserverError("gdi_unknown_or_rtl_layout")
        points = (wintypes.POINT * 2)(
            wintypes.POINT(0, 0), wintypes.POINT(current_profile().width, current_profile().height),
        )
        self.require(self.gdi.LPtoDP(dc, points, 2), "LPtoDP")
        if ((points[0].x, points[0].y), (points[1].x, points[1].y)) != ((0, 0), (current_profile().width, current_profile().height)):
            raise _ObserverError("gdi_logical_device_mapping_not_identity")

    def fixed_version(self, path: str) -> tuple[int, int, int, int]:
        unused = wintypes.DWORD()
        size = int(self.version.GetFileVersionInfoSizeW(path, ctypes.byref(unused)))
        if not 0 < size <= 1_048_576:
            raise _ObserverError("fixed_version_resource_unavailable_or_oversize")
        buffer = ctypes.create_string_buffer(size)
        self.require(self.version.GetFileVersionInfoW(path, 0, size, buffer), "GetFileVersionInfoW")
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        self.require(self.version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)), "VerQueryValueW")
        if not pointer.value or length.value < ctypes.sizeof(_VS_FIXEDFILEINFO):
            raise _ObserverError("fixed_version_resource_invalid")
        base_address = ctypes.addressof(buffer)
        if not base_address <= pointer.value <= base_address + size - ctypes.sizeof(_VS_FIXEDFILEINFO):
            raise _ObserverError("fixed_version_pointer_outside_own_buffer")
        # This points into our own version-resource buffer, not another process.
        info = ctypes.cast(pointer, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
        if info.dwSignature != 0xFEEF04BD:
            raise _ObserverError("fixed_version_resource_signature_invalid")
        return (
            info.dwFileVersionMS >> 16, info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16, info.dwFileVersionLS & 0xFFFF,
        )


def _identity_key(identity: NativeWindowIdentity) -> tuple:
    return (
        identity.hwnd, identity.pid, identity.process_created_filetime_100ns,
        ntpath.normcase(identity.executable_path), identity.title, identity.window_class,
        identity.window_rect, identity.client_screen_rect, identity.dpi,
    )


def _require_eligible_probe(probe: object) -> NativeWindowIdentity:
    if not isinstance(probe, WindowProbeResult):
        raise _ObserverError("typed_native_probe_required")
    if probe.status != "UNIQUE" or len(probe.candidates) != 1 or probe.errors:
        raise _ObserverError("native_identity_not_unique_or_complete")
    identity = probe.candidates[0]
    if not isinstance(identity, NativeWindowIdentity):
        raise _ObserverError("typed_native_identity_required")
    if (
        any(not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in
            (identity.hwnd, identity.pid, identity.process_created_filetime_100ns))
        or ntpath.normcase(identity.executable_path) != ntpath.normcase(current_profile().executable)
        or identity.title != current_profile().title
        or identity.hwnd_source != "EnumWindows; not a Sky window token"
        or probe.monitor_count != 1 or probe.primary_screen_rect != current_profile().screen
        or probe.thread_dpi_awareness != 2
        or identity.window_rect != current_profile().screen
        or identity.client_screen_rect != current_profile().screen
        or identity.dpi != current_profile().dpi or identity.window_class != current_profile().window_class
    ):
        raise _ObserverError("native_identity_outside_fixed_profile")
    if identity.foreground_match is not True or (
        probe.foreground_hwnd_at_start != identity.hwnd
        or probe.foreground_hwnd_at_end != identity.hwnd
    ):
        raise _ObserverError("verified_native_window_not_stably_foreground")
    return identity


class _SelectionObservationError(Exception):
    pass


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise _SelectionObservationError(code)


def _canonical_uuid(value: object, code: str) -> str:
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except (ValueError, TypeError, AttributeError):
        parsed = None
    _require(parsed is not None and parsed.int != 0 and str(parsed) == value, code)
    return value


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _rect(value: object, name: str) -> Rect:
    _require(isinstance(value, (tuple, list)) and len(value) == 4,
             name + "_requires_four_physical_edges")
    _require(all(_integer(edge) for edge in value), name + "_requires_integer_edges")
    result = tuple(value)
    _require(result[0] < result[2] and result[1] < result[3], name + "_must_be_nonempty")
    _require(all(-(2**31) <= edge < 2**31 for edge in result), name + "_outside_win32_range")
    return result


def _contains(outer: Rect, inner: Rect) -> bool:
    return (outer[0] <= inner[0] < inner[2] <= outer[2]
            and outer[1] <= inner[1] < inner[3] <= outer[3])


def _intersects(first: Rect, second: Rect) -> bool:
    return (min(first[2], second[2]) > max(first[0], second[0])
            and min(first[3], second[3]) > max(first[1], second[1]))


def _area(rect: Rect) -> int:
    return (rect[2] - rect[0]) * (rect[3] - rect[1])


@dataclass(frozen=True)
class _Plan:
    frame_id: str
    generation: int
    source_capture_sha256: str
    topology_sha256: str
    stage_id: str
    stage_epoch: int
    payload: Rect
    transport_requested: Rect
    white_container: Rect
    guard_physical_px: int = GUARD_PHYSICAL_PX
    white_padding_physical_px: int = WHITE_PADDING_PHYSICAL_PX

    def metadata_only(self) -> dict:
        return dict(
            frame_id=self.frame_id, generation=self.generation,
            source_capture_sha256=self.source_capture_sha256,
            topology_sha256=self.topology_sha256,
            stage_id=self.stage_id, stage_epoch=self.stage_epoch,
            payload_bbox_physical=self.payload,
            transport_requested_bbox_physical=self.transport_requested,
            white_padding_bbox_physical=self.white_container,
            guard_physical_px=self.guard_physical_px,
            white_padding_physical_px=self.white_padding_physical_px,
            geometry_scope="requested stage geometry; not observed native selection extent",
        )


def _plan(frame: FrozenFrame, mapping: Mapping, generation: int) -> _Plan:
    _require(isinstance(frame, FrozenFrame), "typed_frozen_frame_required")
    _require(isinstance(mapping, Mapping), "stage_mapping_required")
    required = (
        "frame_id", "generation", "source_capture_sha256", "topology_sha256",
        "stage_id", "stage_epoch", "payload_bbox_physical",
        "transport_bbox_physical", "white_padding_bbox_physical",
        "guard_physical_px", "white_padding_physical_px",
    )
    for key in required:
        _require(key in mapping, "missing_stage_mapping_field:" + key)
    _require(_integer(generation) and generation >= 0, "invalid_generation")
    _require(_integer(mapping["generation"]) and mapping["generation"] == generation == frame.generation,
             "frame_mapping_generation_mismatch")
    _require(mapping["frame_id"] == frame.frame_id, "frame_mapping_id_mismatch")
    _require(mapping["source_capture_sha256"] == frame.capture_sha256,
             "frame_mapping_pixel_digest_mismatch")
    _require(mapping["topology_sha256"] == frame.topology_sha256,
             "frame_mapping_topology_digest_mismatch")
    stage_id = mapping["stage_id"]
    _canonical_uuid(stage_id, "invalid_stage_id")
    epoch = mapping["stage_epoch"]
    _require(_integer(epoch) and epoch >= 0, "invalid_stage_epoch")
    guard, pad = mapping["guard_physical_px"], mapping["white_padding_physical_px"]
    _require(_integer(guard) and guard in SUPPORTED_STAGE_PADDING_BY_GUARD,
             "unknown_guard_profile")
    _require(_integer(pad) and pad == SUPPORTED_STAGE_PADDING_BY_GUARD[guard],
             "unknown_white_padding_profile")
    payload = _rect(mapping["payload_bbox_physical"], "payload")
    transport = _rect(mapping["transport_bbox_physical"], "transport_requested")
    white = _rect(mapping["white_padding_bbox_physical"], "white_container")
    _require((payload[2] - payload[0], payload[3] - payload[1]) == (frame.width, frame.height),
             "payload_size_differs_from_frozen_pixels")
    expected_transport = (
        ((payload[0] - guard) // 2) * 2,
        ((payload[1] - guard) // 2) * 2,
        -((-(payload[2] + guard)) // 2) * 2,
        -((-(payload[3] + guard)) // 2) * 2,
    )
    _require(transport == expected_transport, "transport_not_even_outward_guard_mapping")
    _require(_contains(current_profile().screen, white) and _contains(white, transport)
             and _contains(transport, payload), "stage_geometry_outside_verified_client")
    _require(
        payload[0] - white[0] >= pad
        and payload[1] - white[1] >= pad
        and white[2] - payload[2] >= pad
        and white[3] - payload[3] >= pad,
        "actual_white_container_padding_too_small",
    )
    _require(transport[2] - transport[0] >= 128 and transport[3] - transport[1] >= 128,
             "small_transport_cross_sections_unadapted")
    topology = frame.topology
    _require(topology.virtual_bbox == current_profile().screen and len(topology.monitors) == 1,
             "frame_outside_fixed_single_monitor_profile")
    monitor = topology.monitors[0]
    _require(monitor.physical_bbox == current_profile().screen
             and monitor.dpi_x == current_profile().dpi and monitor.dpi_y == current_profile().dpi
             and monitor.primary and monitor.rotation_degrees == 0,
             "frame_outside_fixed_monitor_dpi_rotation_profile")
    return _Plan(frame.frame_id, generation, frame.capture_sha256,
                 frame.topology_sha256, stage_id, epoch, payload, transport, white, guard, pad)


@dataclass(frozen=True)
class _Section:
    side: str
    quarter: int
    bbox: Rect
    normal_axis: str
    requested_edge_coordinate: int


def _sections(plan: _Plan) -> tuple[_Section, ...]:
    left, top, right, bottom = plan.transport_requested
    half = TANGENTIAL_THICKNESS_PX // 2
    sections: list[_Section] = []
    for quarter in (1, 3):
        x = left + (right - left) * quarter // 4
        y = top + (bottom - top) * quarter // 4
        sections.extend((
            _Section("top", quarter, (x - half, top - OUTER_NORMAL_PX,
                     x + half, top + INNER_NORMAL_PX), "y", top),
            _Section("bottom", quarter, (x - half, bottom - INNER_NORMAL_PX,
                     x + half, bottom + OUTER_NORMAL_PX), "y", bottom),
            _Section("left", quarter, (left - OUTER_NORMAL_PX, y - half,
                     left + INNER_NORMAL_PX, y + half), "x", left),
            _Section("right", quarter, (right - INNER_NORMAL_PX, y - half,
                     right + OUTER_NORMAL_PX, y + half), "x", right),
        ))
    for section in sections:
        _require(_contains(plan.white_container, section.bbox),
                 "cross_section_outside_actual_white_container")
        _require(not _intersects(section.bbox, plan.payload),
                 "cross_section_would_include_payload_pixels")
    return tuple(sections)


def _capture_rect(api: _Gdi, identity: NativeWindowIdentity, rect: Rect,
                  approved_container: Rect) -> Image.Image:
    """Independent bounded shape helper; does not change old _capture_band."""
    _require(_contains(approved_container, rect) and _contains(current_profile().screen, rect),
             "capture_rectangle_outside_approved_stage_container")
    _require(0 < _area(rect) <= (current_profile().width * current_profile().height), "capture_rectangle_exceeds_budget")
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    source_dc = memory_dc = bitmap = old_object = None
    image: Image.Image | None = None
    failure: Exception | None = None
    cleanup_errors: list[str] = []
    try:
        ctypes.set_last_error(0)
        source_dc = api.user.GetDC(identity.hwnd)
        api.require(source_dc, "GetDC")
        api.verify_identity_mapping(source_dc, source=True)
        memory_dc = api.gdi.CreateCompatibleDC(source_dc)
        api.require(memory_dc, "CreateCompatibleDC")
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth, info.bmiHeader.biHeight = width, -height
        info.bmiHeader.biPlanes, info.bmiHeader.biBitCount = 1, 32
        info.bmiHeader.biCompression = 0
        info.bmiHeader.biSizeImage = width * height * 4
        bits = ctypes.c_void_p()
        bitmap = api.gdi.CreateDIBSection(source_dc, ctypes.byref(info), 0,
                                         ctypes.byref(bits), None, 0)
        api.require(bitmap, "CreateDIBSection")
        _require(bool(bits.value), "own_dib_pointer_unavailable")
        old_object = api.gdi.SelectObject(memory_dc, bitmap)
        if not old_object or old_object == HGDI_ERROR:
            old_object = None
            raise _SelectionObservationError("select_own_dib_failed")
        api.verify_identity_mapping(memory_dc, source=False)
        api.require(api.gdi.BitBlt(memory_dc, 0, 0, width, height,
                                  source_dc, left, top, SRCCOPY | CAPTUREBLT), "BitBlt")
        api.require(api.gdi.GdiFlush(), "GdiFlush")
        raw = ctypes.string_at(bits.value, width * height * 4)
        image = Image.frombytes("RGB", (width, height), raw, "raw", "BGRX")
        del raw
    except (_ObserverError, _SelectionObservationError) as exc:
        failure = exc
    except Exception:
        failure = _SelectionObservationError("bounded_gdi_capture_internal_error")
    finally:
        if memory_dc and old_object:
            restored = api.gdi.SelectObject(memory_dc, old_object)
            if not restored or restored == HGDI_ERROR:
                cleanup_errors.append("restore_selected_object_failed")
        if memory_dc and not api.gdi.DeleteDC(memory_dc):
            cleanup_errors.append("delete_memory_dc_failed")
        if bitmap and not api.gdi.DeleteObject(bitmap):
            cleanup_errors.append("delete_own_dib_failed")
        if source_dc and api.user.ReleaseDC(identity.hwnd, source_dc) != 1:
            cleanup_errors.append("release_source_dc_failed")
    if cleanup_errors:
        if image is not None:
            image.close()
        raise _SelectionObservationError("gdi_cleanup:" + ",".join(cleanup_errors))
    if failure is not None:
        if image is not None:
            image.close()
        raise failure
    _require(image is not None, "capture_returned_no_pixels")
    return image


def _profile(image: Image.Image, section: _Section) -> dict:
    """Compute tiny strip statistics with RGB8 bytes; never infer a boundary."""
    _require(image.mode == "RGB" and image.size ==
             (section.bbox[2] - section.bbox[0], section.bbox[3] - section.bbox[1]),
             "strip_capture_shape_or_mode_changed")
    raw = image.tobytes()
    rows: list[dict] = []
    count = image.height if section.normal_axis == "y" else image.width
    tangent = image.width if section.normal_axis == "y" else image.height
    origin = section.bbox[1] if section.normal_axis == "y" else section.bbox[0]
    for index in range(count):
        offsets = [((index * image.width + t) if section.normal_axis == "y"
                    else (t * image.width + index)) * 3 for t in range(tangent)]
        channels = [[raw[offset + channel] for offset in offsets] for channel in range(3)]
        rows.append(dict(
            physical_pixel_coordinate=origin + index,
            tangential_pixel_count=tangent,
            rgb_mean=[round(sum(values) / tangent, 6) for values in channels],
            rgb_min=[min(values) for values in channels],
            rgb_max=[max(values) for values in channels],
            exact_source_white_pixel_count=sum(raw[offset:offset + 3] == b"\xff\xff\xff"
                                               for offset in offsets),
        ))
    return dict(
        side=section.side, quarter_numerator=section.quarter, quarter_denominator=4,
        captured_bbox_physical=section.bbox, width=image.width, height=image.height,
        normal_axis=section.normal_axis,
        requested_edge_coordinate=section.requested_edge_coordinate,
        requested_edge_scope="search reference from stage mapping; NOT observed native extent",
        captured_rgb_sha256=hashlib.sha256(raw).hexdigest(), normal_profiles=rows,
        profile_scope="numeric white-guard cross-sections; no boundary inference",
    )


def _payload_numeric_difference(image: Image.Image, expected_rgb8: bytes, observed_sha256: str) -> dict:
    """Compare ephemeral RGB8 using an any-channel mask, returning numbers only.

    The captured hash is reused from the same payload acquisition. RGB.getbbox
    is deliberately avoided: the union of the three L-mode differences detects
    a red-, green- or blue-only change. All owned temporary images are closed.
    """
    from PIL import ImageChops
    _require(image.mode == "RGB" and type(expected_rgb8) is bytes
             and len(expected_rgb8) == image.width*image.height*3,
             "diagnostic_expected_rgb8_requires_exact_payload_bytes")
    with ExitStack() as owned:
        def close_later(value: Image.Image) -> Image.Image:
            owned.callback(value.close)
            return value
        expected = close_later(Image.frombytes("RGB", image.size, expected_rgb8))
        difference = close_later(ImageChops.difference(image, expected))
        channels = tuple(close_later(channel) for channel in difference.split())
        maxima = [channel.getextrema()[1] for channel in channels]
        first_union = close_later(ImageChops.lighter(channels[0], channels[1]))
        union = close_later(ImageChops.lighter(first_union, channels[2]))
        count = image.width*image.height-union.histogram()[0]
        bbox = union.getbbox()
    return dict(diagnostic_not_authorization=True, different_pixel_count=count,
        difference_bbox_payload_relative=bbox, channel_max_abs_delta=maxima,
        channels_equal=[maximum == 0 for maximum in maxima], all_pixels_equal=count == 0,
        expected_rgb_sha256=hashlib.sha256(expected_rgb8).hexdigest(), observed_rgb_sha256=observed_sha256,
        comparison_scope="caller-supplied RGB8 numeric difference only; no tolerance, colour fit, receipt or business proof")


def observe_native_selection_pixels(
    frame: FrozenFrame,
    *,
    stage_mapping: Mapping[str, object],
    session_id: str,
    generation: int,
    deadline_monotonic_ns: int,
    diagnostic_expected_rgb8: bytes | None = None,
) -> dict:
    """Observe payload bytes and eight white-guard cross-sections once.

    The caller must have established the public stage's own-window geometry and
    must discard a result if its current session/generation/stage epoch changed.
    This function cannot establish that caller state. Win32 queries/captures are
    synchronous and non-atomic; deadlines reject late results but cannot abort a
    blocking Win32 call. There is no loop, retry, input or selection receipt.

    Optional caller-supplied expected RGB8 enables numeric differences only,
    never another predictor, tolerance, receipt or authorization. The default
    performs no extra comparison. Neither supplied nor acquired pixels escape.
    """
    started = time.monotonic_ns()
    output = dict(
        schema_version=1, evidence_id=str(uuid4()),
        status="REJECTED", reason="not_started",
        session_id=session_id, generation=generation,
        frame_id=None, stage_id=None, stage_epoch=None,
        source_capture_sha256=None, topology_sha256=None,
        stage_mapping=None,
        native_identity_before=None, native_identity_after=None,
        version_metadata=None, payload_comparison=None, edge_observations=[],
        started_monotonic_ns=started, capture_completed_monotonic_ns=None,
        finished_monotonic_ns=None, deadline_monotonic_ns=deadline_monotonic_ns,
        capture_budget_pixels=(current_profile().width * current_profile().height), planned_capture_pixels=None,
        selection_extent_status="UNVERIFIED",
        observed_selection_bbox_physical=None,
        extent_verification_method=None,
        read_only=True, raw_pixels_persisted=False, native_input_sent=False,
        errors=[],
        limitations=[
            "No native selection extent is inferred or verified by this module.",
            "Pixel equality proves only the captured payload region equals the frame; it does not prove native selection bounds.",
            "Path/fixed version/window metadata are not Authenticode or complete loaded-build identity.",
            "Pre/post checks are not atomic and do not prove caller session/generation remains current.",
        ],
    )
    identity: NativeWindowIdentity | None = None
    api: _Gdi | None = None
    captured_pixels = 0

    def deadline_check() -> None:
        if time.monotonic_ns() >= deadline_monotonic_ns:
            raise _SelectionObservationError("observation_deadline_expired")

    try:
        _require(sys.platform == "win32", "windows_required")
        _canonical_uuid(session_id, "invalid_session_id")
        _require(_integer(deadline_monotonic_ns)
                 and started < deadline_monotonic_ns <= started + MAX_OBSERVATION_NS,
                 "deadline_must_be_future_and_at_most_two_seconds")
        plan = _plan(frame, stage_mapping, generation)
        if diagnostic_expected_rgb8 is not None:
            _require(type(diagnostic_expected_rgb8) is bytes
                     and len(diagnostic_expected_rgb8) == frame.width*frame.height*3,
                     "diagnostic_expected_rgb8_requires_exact_payload_bytes")
        sections = _sections(plan)
        planned_pixels = _area(plan.payload) + sum(_area(s.bbox) for s in sections)
        output['planned_capture_pixels'] = planned_pixels
        _require(planned_pixels <= (current_profile().width * current_profile().height), "total_capture_exceeds_pixel_budget")
        output.update(
            frame_id=plan.frame_id, stage_id=plan.stage_id, stage_epoch=plan.stage_epoch,
            source_capture_sha256=plan.source_capture_sha256,
            topology_sha256=plan.topology_sha256,
            stage_mapping=plan.metadata_only(), planned_capture_pixels=planned_pixels,
        )
        deadline_check()
        pre_probe = probe_screenshot_window()
        output["native_identity_before"] = pre_probe.to_dict()
        identity = _require_eligible_probe(pre_probe)
        api = _Gdi()
        version = api.fixed_version(identity.executable_path)
        output["version_metadata"] = dict(
            fixed_file_version=list(version),
            expected_fixed_file_version=list(current_profile().version),
            authenticode_verified=False,
            source="fixed VERSIONINFO resource of independently verified executable path",
        )
        _require(version == current_profile().version, "unknown_fixed_file_version_unadapted")
        deadline_check()
        begin = time.monotonic_ns()
        with _capture_rect(api, identity, plan.payload, plan.white_container) as image:
            end = time.monotonic_ns()
            captured_pixels += _area(plan.payload)
            payload_pixels = image.tobytes()
            observed_digest = hashlib.sha256(payload_pixels).hexdigest()
            exact_equal = payload_pixels == frame.rgb8
            del payload_pixels
            output["payload_comparison"] = dict(
                captured_bbox_physical=plan.payload,
                width=image.width, height=image.height,
                captured_rgb_sha256=observed_digest,
                expected_rgb_sha256=frame.capture_sha256,
                exact_rgb_bytes_equal=exact_equal,
                capture_started_monotonic_ns=begin,
                capture_ended_monotonic_ns=end,
                comparison_scope="captured pixels at requested payload location; not native selection extent",
            )
            if diagnostic_expected_rgb8 is not None:
                output["payload_difference_diagnostic"] = _payload_numeric_difference(
                    image, diagnostic_expected_rgb8, observed_digest)
        deadline_check()
        for section in sections:
            deadline_check()
            begin = time.monotonic_ns()
            with _capture_rect(api, identity, section.bbox, plan.white_container) as image:
                end = time.monotonic_ns()
                captured_pixels += _area(section.bbox)
                observation = _profile(image, section)
                observation.update(capture_started_monotonic_ns=begin,
                                   capture_ended_monotonic_ns=end)
                output["edge_observations"].append(observation)
            deadline_check()
        output["capture_completed_monotonic_ns"] = time.monotonic_ns()
        output["status"] = "PAYLOAD_MATCH_EXTENT_UNVERIFIED" if exact_equal else "PAYLOAD_MISMATCH"
        output["reason"] = (
            "payload_bytes_equal_native_selection_extent_unverified" if exact_equal
            else "captured_payload_bytes_differ_from_frame"
        )
    except _SelectionObservationError as exc:
        code = str(exc)
        output["errors"].append(code)
        output["status"] = "STALE" if code == "observation_deadline_expired" else "REJECTED"
        output["reason"] = code
    except _ObserverError as exc:
        output["errors"].append(exc.code)
        output["status"], output["reason"] = "REJECTED", exc.code
    except Exception as exc:
        output["errors"].append("selection_observer_internal_exception:" + type(exc).__name__)
        output["status"], output["reason"] = "ERROR", "selection_observer_internal_exception"
    finally:
        if identity is not None:
            try:
                post_probe = probe_screenshot_window()
                output["native_identity_after"] = post_probe.to_dict()
                after = _require_eligible_probe(post_probe)
                _require(_identity_key(after) == _identity_key(identity),
                         "native_identity_changed_across_observation")
                if api is not None and output["version_metadata"] is not None:
                    version_after = api.fixed_version(after.executable_path)
                    output["version_metadata"]["fixed_file_version_after"] = list(version_after)
                    _require(version_after == current_profile().version,
                             "fixed_file_version_changed_across_observation")
            except (_SelectionObservationError, _ObserverError) as exc:
                code = exc.code if isinstance(exc, _ObserverError) else str(exc)
                output["errors"].append(code)
                output["status"], output["reason"] = "REJECTED", code
            except Exception as exc:
                output["errors"].append("post_identity_exception:" + type(exc).__name__)
                output["status"], output["reason"] = "ERROR", "post_identity_query_failed"
        output["captured_pixels"] = captured_pixels
        finished = time.monotonic_ns()
        output["finished_monotonic_ns"] = finished
        if _integer(deadline_monotonic_ns) and finished >= deadline_monotonic_ns:
            output["status"], output["reason"] = "STALE", "observation_deadline_expired"
        # The observer intentionally has no code path to VERIFIED or an actual
        # selection rectangle, including successful payload comparisons.
    return output
