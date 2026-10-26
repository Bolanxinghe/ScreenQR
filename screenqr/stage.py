"""Own-window raw-pixel stage mapping and verification; no application input.

A mapping declares requested transport geometry. It does NOT prove the extent
of any WeChat selection. Only verify_frozen_stage reads pixels, bounded to the
caller's own stage; it returns hashes/metadata, never images. UI callers own Tk
access and stage epochs. Workers must revalidate through the UI before using a
receipt. Independent Win32 queries are not an atomic window transaction.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import hashlib
import re
import sys
import time
from uuid import UUID, uuid4

from PIL import Image

from .capture import capture_region_with_metadata, read_display_topology
from .frozen_frame import BBox, FrozenFrame

GUARD_PHYSICAL_PX = 8
WHITE_PADDING_PHYSICAL_PX = 24
LIVE_NATIVE_GUARD_PHYSICAL_PX = 16
LIVE_NATIVE_WHITE_PADDING_PHYSICAL_PX = 32
SUPPORTED_STAGE_PADDING_BY_GUARD = {
    GUARD_PHYSICAL_PX: WHITE_PADDING_PHYSICAL_PX,
    LIVE_NATIVE_GUARD_PHYSICAL_PX: LIVE_NATIVE_WHITE_PADDING_PHYSICAL_PX,
}


class StageValidationError(ValueError):
    """The own stage is unknown, changed, clipped or not pixel-exact."""


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_padding_profile(guard: int, padding: int) -> None:
    if (not _positive_int(guard) or not _positive_int(padding)
            or SUPPORTED_STAGE_PADDING_BY_GUARD.get(guard) != padding):
        raise StageValidationError("Only declared guard/white-padding pairs are supported")


def _bbox(value: object) -> BBox:
    if (
        not isinstance(value, tuple) or len(value) != 4
        or any(not isinstance(v, int) or isinstance(v, bool) for v in value)
        or any(v < -(2**31) or v > 2**31 - 1 for v in value)
        or value[2] <= value[0] or value[3] <= value[1]
    ):
        raise StageValidationError("A nonempty integer physical bbox is required")
    return value


def _inside(inner: BBox, outer: BBox) -> bool:
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


def even_transport_bbox(
    payload_bbox_physical: BBox, white_padding_bbox_physical: BBox,
    *, guard_physical_px: int = GUARD_PHYSICAL_PX,
) -> BBox:
    """Add explicit guard, then round outward; no source pixel is resized."""
    payload, white = _bbox(payload_bbox_physical), _bbox(white_padding_bbox_physical)
    if not _positive_int(guard_physical_px) or guard_physical_px not in SUPPORTED_STAGE_PADDING_BY_GUARD:
        raise StageValidationError("Only declared physical guard widths are supported")
    left, top, right, bottom = payload
    result = (
        (left - guard_physical_px) // 2 * 2,
        (top - guard_physical_px) // 2 * 2,
        (right + guard_physical_px + 1) // 2 * 2,
        (bottom + guard_physical_px + 1) // 2 * 2,
    )
    _bbox(result)
    if not _inside(result, white):
        raise StageValidationError("Even transport extent exceeds the explicit own white stage")
    return result


@dataclass(frozen=True, slots=True)
class OwnStageSnapshot:
    top_hwnd: int
    image_hwnd: int
    padding_hwnd: int
    pid: int
    top_window_bbox_physical: BBox
    top_client_bbox_physical: BBox
    image_bbox_physical: BBox
    padding_bbox_physical: BBox
    dpi: int

    def __post_init__(self) -> None:
        if not all(_positive_int(v) for v in (self.top_hwnd, self.image_hwnd, self.padding_hwnd, self.pid, self.dpi)):
            raise StageValidationError("Own window identity and DPI must be explicit")
        if len({self.top_hwnd, self.image_hwnd, self.padding_hwnd}) != 3:
            raise StageValidationError("Stage requires distinct top/image/padding windows")
        for rect in (
            self.top_window_bbox_physical, self.top_client_bbox_physical,
            self.image_bbox_physical, self.padding_bbox_physical,
        ):
            _bbox(rect)
        if not _inside(self.padding_bbox_physical, self.top_client_bbox_physical):
            raise StageValidationError("White stage lies outside the own preview client")
        if not _inside(self.image_bbox_physical, self.padding_bbox_physical):
            raise StageValidationError("Image lies outside its white stage")


@dataclass(frozen=True, slots=True)
class FrozenStageMapping:
    frame_id: str
    generation: int
    source_capture_sha256: str
    topology_sha256: str
    stage_id: str
    stage_epoch: int
    payload_width: int
    payload_height: int
    payload_bbox_physical: BBox
    transport_bbox_physical: BBox
    payload_offset: tuple[int, int]
    white_padding_bbox_physical: BBox
    own_stage: OwnStageSnapshot
    dpi_reference_hwnds: tuple[int, ...]
    described_monotonic_ns: int
    guard_physical_px: int = GUARD_PHYSICAL_PX
    white_padding_physical_px: int = WHITE_PADDING_PHYSICAL_PX

    def __post_init__(self) -> None:
        for value in (self.frame_id, self.stage_id):
            try:
                identifier = UUID(value) if isinstance(value, str) else None
            except (ValueError, TypeError, AttributeError):
                identifier = None
            if identifier is None or identifier.int == 0 or str(identifier) != value:
                raise StageValidationError("Frame/stage identifiers must be canonical nonzero UUIDs")
        for value in (self.generation, self.stage_epoch):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise StageValidationError("Generation and epoch must be nonnegative integers")
        for value in (self.source_capture_sha256, self.topology_sha256):
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise StageValidationError("Declared source/topology SHA-256 is invalid")
        if not all(_positive_int(v) for v in (self.payload_width, self.payload_height, self.described_monotonic_ns)):
            raise StageValidationError("Payload dimensions and observation time must be explicit")
        if not isinstance(self.own_stage, OwnStageSnapshot):
            raise TypeError("own_stage must be a typed snapshot")
        payload, white = _bbox(self.payload_bbox_physical), _bbox(self.white_padding_bbox_physical)
        if self.own_stage.image_bbox_physical != payload or self.own_stage.padding_bbox_physical != white:
            raise StageValidationError("Stage mapping differs from its own-window snapshot")
        if (payload[2] - payload[0], payload[3] - payload[1]) != (self.payload_width, self.payload_height):
            raise StageValidationError("Stage image does not have one physical pixel per source pixel")
        pad = self.white_padding_physical_px
        _validate_padding_profile(self.guard_physical_px, pad)
        if white != (payload[0] - pad, payload[1] - pad, payload[2] + pad, payload[3] + pad):
            raise StageValidationError("White container padding is not exact in physical pixels")
        expected = even_transport_bbox(payload, white, guard_physical_px=self.guard_physical_px)
        if self.transport_bbox_physical != expected:
            raise StageValidationError("Transport rectangle differs from guarded even extent")
        offset = payload[0] - expected[0], payload[1] - expected[1]
        if (not isinstance(self.payload_offset, tuple) or len(self.payload_offset) != 2
                or any(not _positive_int(v) for v in self.payload_offset)
                or self.payload_offset != offset
                or any(v not in (self.guard_physical_px, self.guard_physical_px + 1) for v in offset)):
            raise StageValidationError("Payload offset differs from guarded transport")
        if (
            not isinstance(self.dpi_reference_hwnds, tuple) or not self.dpi_reference_hwnds
            or any(not _positive_int(v) for v in self.dpi_reference_hwnds)
            or len(set(self.dpi_reference_hwnds)) != len(self.dpi_reference_hwnds)
            or self.own_stage.top_hwnd not in self.dpi_reference_hwnds
        ):
            raise StageValidationError("Stage requires explicit deduplicated own DPI references")


    def metadata_only(self) -> dict:
        """No pixel bytes; requested geometry is not native selection evidence."""
        return asdict(self)

    def to_dict(self) -> dict:
        return self.metadata_only()


@dataclass(frozen=True, slots=True)
class StagePixelVerification:
    mapping: FrozenStageMapping
    rendered_payload_sha256: str
    rendered_transport_sha256: str
    expected_transport_sha256: str
    captured_white_stage_sha256: str
    expected_white_stage_sha256: str
    verification_capture_frame_id: str
    capture_started_monotonic: float
    capture_ended_monotonic: float
    checked_monotonic_ns: int
    evidence_scope: str = "own displayed stage only; not native WeChat selection extent or input permission"

    def metadata_only(self) -> dict:
        """Historical pixel-check metadata, with no captured or source bytes."""
        return asdict(self)

    def to_dict(self) -> dict:
        return self.metadata_only()


class _StageApi:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise StageValidationError("Native own-stage mapping requires Windows")
        try:
            self.user = ctypes.WinDLL("user32", use_last_error=True)
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            signatures = (
                (self.user, "IsWindow", [wintypes.HWND], wintypes.BOOL),
                (self.user, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
                (self.user, "IsIconic", [wintypes.HWND], wintypes.BOOL),
                (self.user, "GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
                (self.user, "GetParent", [wintypes.HWND], wintypes.HWND),
                (self.user, "GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
                (self.user, "SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
                (self.user, "GetForegroundWindow", [], wintypes.HWND),
                (self.user, "GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
                (self.user, "GetClientRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
                (self.user, "ClientToScreen", [wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
                (self.user, "GetThreadDpiAwarenessContext", [], wintypes.HANDLE),
                (self.user, "GetWindowDpiAwarenessContext", [wintypes.HWND], wintypes.HANDLE),
                (self.user, "GetAwarenessFromDpiAwarenessContext", [wintypes.HANDLE], ctypes.c_int),
                (self.user, "AreDpiAwarenessContextsEqual", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
                (self.user, "GetDpiForWindow", [wintypes.HWND], wintypes.UINT),
                (self.kernel, "GetCurrentProcessId", [], wintypes.DWORD),
            )
            for library, name, args, result in signatures:
                function = getattr(library, name)
                function.argtypes, function.restype = args, result
        except (AttributeError, OSError):
            raise StageValidationError("Required read-only own-window APIs are unavailable") from None

    def pm_context(self) -> int:
        context = self.user.GetThreadDpiAwarenessContext()
        if not context or self.user.GetAwarenessFromDpiAwarenessContext(context) != 2:
            raise StageValidationError("Stage queries require an already PM-aware thread")
        return int(context)

    def rects(self, hwnd: int) -> tuple[BBox, BBox]:
        window, client = wintypes.RECT(), wintypes.RECT()
        if not self.user.GetWindowRect(hwnd, ctypes.byref(window)) or not self.user.GetClientRect(hwnd, ctypes.byref(client)):
            raise StageValidationError("Own stage bounds are unavailable")
        if (client.left, client.top) != (0, 0):
            raise StageValidationError("Own stage client origin is unknown")
        first, last = wintypes.POINT(0, 0), wintypes.POINT(client.right, client.bottom)
        if not self.user.ClientToScreen(hwnd, ctypes.byref(first)) or not self.user.ClientToScreen(hwnd, ctypes.byref(last)):
            raise StageValidationError("Own stage physical origin is unavailable")
        return (
            _bbox((int(window.left), int(window.top), int(window.right), int(window.bottom))),
            _bbox((int(first.x), int(first.y), int(last.x), int(last.y))),
        )

    def once(self, top: int, image: int, padding: int) -> OwnStageSnapshot:
        pid = int(self.kernel.GetCurrentProcessId())
        contexts = []
        dpis = []
        for hwnd in (top, image, padding):
            if not _positive_int(hwnd) or not self.user.IsWindow(hwnd):
                raise StageValidationError("Own stage window no longer exists")
            owner = wintypes.DWORD()
            if not self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner)) or owner.value != pid:
                raise StageValidationError("Stage queries are limited to this process")
            if int(self.user.GetAncestor(hwnd, 2) or 0) != top or not self.user.IsWindowVisible(hwnd):
                raise StageValidationError("Own stage is not visibly rooted in the expected preview")
            context = self.user.GetWindowDpiAwarenessContext(hwnd)
            if not context or self.user.GetAwarenessFromDpiAwarenessContext(context) != 2:
                raise StageValidationError("Own stage window is not per-monitor DPI aware")
            contexts.append(context)
            dpi = int(self.user.GetDpiForWindow(hwnd))
            if dpi <= 0:
                raise StageValidationError("Own stage DPI is unavailable")
            dpis.append(dpi)
        if self.user.IsIconic(top) or int(self.user.GetParent(image) or 0) != padding:
            raise StageValidationError("Stage image parent/visibility changed")
        if len(set(dpis)) != 1 or any(not self.user.AreDpiAwarenessContextsEqual(contexts[0], c) for c in contexts[1:]):
            raise StageValidationError("Own stage DPI contexts disagree")
        top_window, top_client = self.rects(top)
        image_window, image_client = self.rects(image)
        padding_window, padding_client = self.rects(padding)
        if image_window != image_client or padding_window != padding_client:
            raise StageValidationError("Image/padding widgets contain unknown nonclient borders")
        return OwnStageSnapshot(top, image, padding, pid, top_window, top_client, image_client, padding_client, dpis[0])

    def query(self, top: int, image: int, padding: int) -> OwnStageSnapshot:
        context = self.pm_context()
        first = self.once(top, image, padding)
        second = self.once(top, image, padding)
        final_context = self.pm_context()
        if first != second or not self.user.AreDpiAwarenessContextsEqual(context, final_context):
            raise StageValidationError("Own stage changed during metadata queries")
        return second


def focus_own_stage(top_hwnd: int, *, allowed_foreground_hwnds: tuple[int, ...] = ()) -> bool:
    """Request normal OS activation of our own stage; respect OS refusal.

    No thread attachment, synthetic keys or external-window activation. This
    is presentation setup, never input permission; the final input guard still
    independently requires our process to own the actual foreground window.
    """
    api = _StageApi()
    owner = wintypes.DWORD()
    if (not _positive_int(top_hwnd) or not api.user.IsWindow(top_hwnd)
            or not api.user.GetWindowThreadProcessId(top_hwnd, ctypes.byref(owner))
            or owner.value != api.kernel.GetCurrentProcessId()
            or int(api.user.GetAncestor(top_hwnd, 2) or 0) != top_hwnd):
        raise StageValidationError('Activation requires an own top-level window')
    foreground = int(api.user.GetForegroundWindow() or 0)
    if foreground == top_hwnd:
        return True
    # Preparation can finish after the user has switched applications. Do not
    # steal focus back from an unrelated window or rely on a stale source HWND.
    foreground_owner = wintypes.DWORD()
    if (foreground not in allowed_foreground_hwnds or not api.user.IsWindow(foreground)
            or not api.user.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_owner))
            or foreground_owner.value != owner.value
            or int(api.user.GetAncestor(foreground, 2) or 0) != foreground):
        return False
    if int(api.user.GetForegroundWindow() or 0) != foreground:
        return False
    if not api.user.SetForegroundWindow(top_hwnd):
        return False
    after_owner = wintypes.DWORD()
    return bool(api.user.IsWindow(top_hwnd)
        and api.user.GetWindowThreadProcessId(top_hwnd, ctypes.byref(after_owner))
        and after_owner.value == owner.value
        and int(api.user.GetAncestor(top_hwnd, 2) or 0) == top_hwnd
        and int(api.user.GetForegroundWindow() or 0) == top_hwnd)


def disable_own_stage_transitions(top_hwnd: int) -> None:
    """Disable show/hide animation for this process's one preview window only.

    Public DWM attribute, no global display setting. Call before showing the
    window. This is presentation setup, never evidence that pixels are ready.
    """
    api = _StageApi()
    if not _positive_int(top_hwnd) or not api.user.IsWindow(top_hwnd):
        raise StageValidationError("Own preview window is unavailable")
    owner = wintypes.DWORD()
    if (not api.user.GetWindowThreadProcessId(top_hwnd, ctypes.byref(owner))
            or owner.value != api.kernel.GetCurrentProcessId()
            or int(api.user.GetAncestor(top_hwnd, 2) or 0) != top_hwnd):
        raise StageValidationError("Presentation setup requires an own top-level window")
    try:
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        function = dwm.DwmSetWindowAttribute
        function.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        function.restype = wintypes.LONG
        disabled = wintypes.BOOL(True)
        if function(top_hwnd, 3, ctypes.byref(disabled), ctypes.sizeof(disabled)) != 0:
            raise StageValidationError("Own preview transitions could not be disabled")
    except (OSError, AttributeError):
        raise StageValidationError("Own preview presentation API is unavailable") from None


def _flush_stage_presentation() -> None:
    """Wait for this process's queued composition update, not all session work.

    Synchronous Win32 call cannot be interrupted; caller rejects a late return.
    It does not replace subsequent byte-exact physical pixel verification.
    """
    try:
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        function = dwm.DwmFlush
        function.argtypes = []
        function.restype = wintypes.LONG
        if function() != 0:
            raise StageValidationError("Own preview composition flush failed")
    except (OSError, AttributeError):
        raise StageValidationError("Own preview composition API is unavailable") from None


def _source_matches(frame: FrozenFrame, mapping: FrozenStageMapping) -> None:
    if not isinstance(frame, FrozenFrame) or not isinstance(mapping, FrozenStageMapping):
        raise TypeError("Typed frozen frame and stage mapping are required")
    if (
        frame.frame_id != mapping.frame_id or frame.generation != mapping.generation
        or frame.capture_sha256 != mapping.source_capture_sha256
        or frame.topology_sha256 != mapping.topology_sha256
        or (frame.width, frame.height) != (mapping.payload_width, mapping.payload_height)
    ):
        raise StageValidationError("Stage mapping no longer belongs to this immutable source frame")


def describe_frozen_stage(
    frame: FrozenFrame, *, stage_id: str, stage_epoch: int,
    top_hwnd: int, image_hwnd: int, padding_hwnd: int,
    dpi_reference_hwnds: tuple[int, ...],
    guard_physical_px: int = GUARD_PHYSICAL_PX,
    white_padding_physical_px: int = WHITE_PADDING_PHYSICAL_PX,
) -> FrozenStageMapping:
    """Query existing own widgets; caller obtains Tk handles on the UI thread."""
    if not isinstance(frame, FrozenFrame):
        raise TypeError("A raw FrozenFrame is required")
    _validate_padding_profile(guard_physical_px, white_padding_physical_px)
    api = _StageApi()
    own = api.query(top_hwnd, image_hwnd, padding_hwnd)
    topology = read_display_topology(dpi_reference_hwnds=dpi_reference_hwnds)
    if topology.digest() != frame.topology_sha256:
        raise StageValidationError("Display topology changed since the raw frame was frozen")
    if api.query(top_hwnd, image_hwnd, padding_hwnd) != own:
        raise StageValidationError("Own stage moved while describing the transport")
    payload, white = own.image_bbox_physical, own.padding_bbox_physical
    transport = even_transport_bbox(payload, white, guard_physical_px=guard_physical_px)
    return FrozenStageMapping(
        frame.frame_id, frame.generation, frame.capture_sha256, frame.topology_sha256,
        stage_id, stage_epoch, frame.width, frame.height, payload, transport,
        (payload[0] - transport[0], payload[1] - transport[1]),
        white, own, dpi_reference_hwnds, time.monotonic_ns(),
        guard_physical_px=guard_physical_px,
        white_padding_physical_px=white_padding_physical_px,
    )


def stage_mapping_is_current(frame: FrozenFrame, mapping: FrozenStageMapping) -> bool:
    """Read-only native check; UI must separately recheck its own epoch/tokens."""
    try:
        _source_matches(frame, mapping)
        own = mapping.own_stage
        api = _StageApi()
        if api.query(own.top_hwnd, own.image_hwnd, own.padding_hwnd) != own:
            return False
        return read_display_topology(dpi_reference_hwnds=mapping.dpi_reference_hwnds).digest() == mapping.topology_sha256
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def verify_frozen_stage(
    frame: FrozenFrame, mapping: FrozenStageMapping, *, deadline_monotonic_ns: int,
) -> StagePixelVerification:
    """Capture only the own white stage in the caller's actual worker process.

    No Tk call occurs here. A result remains unusable until the owning UI checks
    frame/generation/stage epoch again. This evidence does not validate a WeChat
    screenshot selection or authorize any input into another application.
    """
    _source_matches(frame, mapping)
    if not _positive_int(deadline_monotonic_ns):
        raise ValueError("An external monotonic deadline is required")

    def deadline() -> None:
        if time.monotonic_ns() >= deadline_monotonic_ns:
            raise TimeoutError("Own-stage verification deadline expired")

    deadline()
    own = mapping.own_stage
    api = _StageApi()
    if api.query(own.top_hwnd, own.image_hwnd, own.padding_hwnd) != own:
        raise StageValidationError("Own stage changed before pixel verification")
    _flush_stage_presentation()
    deadline()
    acquired = capture_region_with_metadata(
        mapping.white_padding_bbox_physical, generation=frame.generation,
        frame_id=str(uuid4()), dpi_reference_hwnds=mapping.dpi_reference_hwnds,
    )
    deadline()
    if (
        acquired.actual_bbox != mapping.white_padding_bbox_physical
        or acquired.requested_bbox != mapping.white_padding_bbox_physical
        or acquired.topology_sha256 != mapping.topology_sha256
    ):
        raise StageValidationError("Own-stage capture clipped or changed display topology")
    if api.query(own.top_hwnd, own.image_hwnd, own.padding_hwnd) != own:
        raise StageValidationError("Own stage changed across its pixel capture")
    white = mapping.white_padding_bbox_physical
    payload = mapping.payload_bbox_physical
    transport = mapping.transport_bbox_physical
    local_payload = (
        payload[0] - white[0], payload[1] - white[1],
        payload[2] - white[0], payload[3] - white[1],
    )
    local_transport = (
        transport[0] - white[0], transport[1] - white[1],
        transport[2] - white[0], transport[3] - white[1],
    )
    with Image.frombytes("RGB", (acquired.width, acquired.height), acquired.rgb8) as actual:
        with actual.crop(local_payload) as actual_payload:
            payload_bytes = actual_payload.tobytes()
        if payload_bytes != frame.rgb8:
            raise StageValidationError("Displayed payload pixels differ from the immutable source")
        payload_sha = hashlib.sha256(payload_bytes).hexdigest()
        del payload_bytes
        with Image.new("RGB", actual.size, (255, 255, 255)) as expected:
            with Image.frombytes("RGB", (frame.width, frame.height), frame.rgb8) as source:
                expected.paste(source, local_payload[:2])
            expected_bytes = expected.tobytes()
            if acquired.rgb8 != expected_bytes:
                raise StageValidationError("Explicit white stage padding is not entirely white")
            expected_stage_sha = hashlib.sha256(expected_bytes).hexdigest()
            del expected_bytes
            with expected.crop(local_transport) as expected_transport:
                expected_transport_sha = hashlib.sha256(expected_transport.tobytes()).hexdigest()
        with actual.crop(local_transport) as actual_transport:
            actual_transport_sha = hashlib.sha256(actual_transport.tobytes()).hexdigest()
    checked = time.monotonic_ns()
    deadline()
    return StagePixelVerification(
        mapping, payload_sha, actual_transport_sha, expected_transport_sha,
        acquired.capture_sha256, expected_stage_sha, acquired.frame_id,
        acquired.capture_started_monotonic, acquired.capture_ended_monotonic, checked,
    )

