"""Immutable raw frames and typed display topology, independent of decoding.

The companion capture_region_with_metadata API in screenqr.capture establishes
real acquisition facts. This module validates caller-supplied facts and freezes
RGB8 pixels; it performs no capture, native query, file I/O or application action.
Hashes describe the declared pixels/topology, not independent provenance proof.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from uuid import UUID

from PIL import Image

BBox = tuple[int, int, int, int]
TOPOLOGY_KIND = "physical-monitor-rect-dpi-primary-rotation-v1"


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_bbox(value: object, field_name: str) -> BBox:
    if not isinstance(value, tuple) or len(value) != 4 or not all(_integer(v) for v in value):
        raise ValueError(f"{field_name} must contain four integer physical-pixel coordinates")
    if any(v < -(2 ** 31) or v > 2 ** 31 - 1 for v in value):
        raise ValueError(f"{field_name} exceeds Win32 coordinate range")
    if value[2] <= value[0] or value[3] <= value[1]:
        raise ValueError(f"{field_name} must be a nonempty half-open rectangle")
    return value


def _validate_monotonic(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite nonnegative monotonic time")
    try:
        valid = math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{field_name} must be a finite nonnegative monotonic time")


@dataclass(frozen=True, slots=True)
class MonitorDescriptor:
    # Capture-layer device token, not a user/account identifier.
    device_name: str
    physical_bbox: BBox
    dpi_x: int
    dpi_y: int
    primary: bool
    rotation_degrees: int

    def __post_init__(self) -> None:
        if not isinstance(self.device_name, str) or not self.device_name or any(ord(c) < 32 for c in self.device_name):
            raise ValueError("device_name must be a nonempty capture-layer display token")
        _validate_bbox(self.physical_bbox, "monitor.physical_bbox")
        if not all(_integer(v) and v > 0 for v in (self.dpi_x, self.dpi_y)):
            raise ValueError("monitor DPI must be explicitly known positive integers")
        if not isinstance(self.primary, bool):
            raise ValueError("monitor primary flag must be boolean")
        if not _integer(self.rotation_degrees) or self.rotation_degrees not in (0, 90, 180, 270):
            raise ValueError("monitor rotation must be explicitly known")


@dataclass(frozen=True, slots=True)
class TopologyDescriptor:
    # This named schema describes exactly these fields, not every property of a
    # Windows display configuration. Only the acquisition layer establishes truth.
    kind: str
    virtual_bbox: BBox
    monitors: tuple[MonitorDescriptor, ...]

    def __post_init__(self) -> None:
        if self.kind != TOPOLOGY_KIND:
            raise ValueError("an explicit supported capture-layer topology kind is required")
        _validate_bbox(self.virtual_bbox, "topology.virtual_bbox")
        if not isinstance(self.monitors, tuple) or not self.monitors or not all(isinstance(m, MonitorDescriptor) for m in self.monitors):
            raise ValueError("topology requires a nonempty immutable tuple of typed monitors")
        names = [monitor.device_name.casefold() for monitor in self.monitors]
        if len(set(names)) != len(names):
            raise ValueError("topology contains duplicate display device tokens")
        if sum(m.primary for m in self.monitors) != 1:
            raise ValueError("topology must identify exactly one primary monitor")
        outer = (
            min(m.physical_bbox[0] for m in self.monitors),
            min(m.physical_bbox[1] for m in self.monitors),
            max(m.physical_bbox[2] for m in self.monitors),
            max(m.physical_bbox[3] for m in self.monitors),
        )
        if self.virtual_bbox != outer:
            raise ValueError("virtual_bbox differs from the monitor layout extent")

    def digest(self) -> str:
        # Enumeration order and ASCII device-name case are not topology changes.
        monitors = [
            dict(device_name=m.device_name.casefold(), physical_bbox=m.physical_bbox,
                 dpi_x=m.dpi_x, dpi_y=m.dpi_y, primary=m.primary,
                 rotation_degrees=m.rotation_degrees)
            for m in sorted(self.monitors, key=lambda item: item.device_name.casefold())
        ]
        canonical = json.dumps(
            dict(kind=self.kind, virtual_bbox=self.virtual_bbox, monitors=monitors),
            sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
        ).encode("ascii")
        return hashlib.sha256(canonical).hexdigest()


def clip_capture_bbox(requested_bbox: BBox, virtual_bbox: BBox) -> BBox:
    """Pure clipping in physical pixels; negative monitor coordinates are valid."""
    requested = _validate_bbox(requested_bbox, "requested_bbox")
    virtual = _validate_bbox(virtual_bbox, "virtual_bbox")
    actual = (max(requested[0], virtual[0]), max(requested[1], virtual[1]),
              min(requested[2], virtual[2]), min(requested[3], virtual[3]))
    return _validate_bbox(actual, "clipped_actual_bbox")


@dataclass(frozen=True, slots=True)
class FrozenFrame:
    frame_id: str
    generation: int
    requested_bbox: BBox
    actual_bbox: BBox
    width: int
    height: int
    rgb8: bytes = field(repr=False)
    capture_started_monotonic: float
    capture_ended_monotonic: float
    topology: TopologyDescriptor
    pixel_format: str = field(default="RGB8", init=False)
    topology_sha256: str = field(init=False)
    # SHA-256 of row-major, tightly packed RGB8 bytes ONLY. Dimensions, bounds and
    # identity remain separate required fields; this is not a provenance proof.
    capture_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.frame_id, str):
            raise ValueError("frame_id must be a caller-generated canonical UUID string")
        try:
            identifier = UUID(self.frame_id)
        except (ValueError, AttributeError, TypeError):
            raise ValueError("frame_id must be a caller-generated canonical UUID string") from None
        if identifier.int == 0 or str(identifier) != self.frame_id:
            raise ValueError("frame_id must be a nonzero canonical UUID string")
        if not _integer(self.generation) or self.generation < 0:
            raise ValueError("generation must be a nonnegative integer")
        if not isinstance(self.topology, TopologyDescriptor):
            raise TypeError("topology must be a capture-layer TopologyDescriptor")
        expected = clip_capture_bbox(self.requested_bbox, self.topology.virtual_bbox)
        _validate_bbox(self.actual_bbox, "actual_bbox")
        if self.actual_bbox != expected:
            raise ValueError("actual_bbox differs from the explicit requested/topology clip")
        if not all(_integer(v) and v > 0 for v in (self.width, self.height)):
            raise ValueError("frame dimensions must be positive integers")
        if (self.width, self.height) != (expected[2] - expected[0], expected[3] - expected[1]):
            raise ValueError("frame dimensions differ from actual_bbox")
        if type(self.rgb8) is not bytes:
            raise TypeError("rgb8 must be immutable bytes, not a mutable buffer")
        if len(self.rgb8) != self.width * self.height * 3:
            raise ValueError("RGB8 byte length differs from frame dimensions")
        _validate_monotonic(self.capture_started_monotonic, "capture_started_monotonic")
        _validate_monotonic(self.capture_ended_monotonic, "capture_ended_monotonic")
        if self.capture_ended_monotonic < self.capture_started_monotonic:
            raise ValueError("capture monotonic interval runs backwards")
        object.__setattr__(self, "topology_sha256", self.topology.digest())
        object.__setattr__(self, "capture_sha256", hashlib.sha256(self.rgb8).hexdigest())

    def metadata_only(self) -> dict:
        """Explicit logging representation; pixels/decoded contents are omitted."""
        return dict(
            frame_id=self.frame_id, generation=self.generation,
            requested_bbox=self.requested_bbox, actual_bbox=self.actual_bbox,
            width=self.width, height=self.height, pixel_format=self.pixel_format,
            capture_started_monotonic=self.capture_started_monotonic,
            capture_ended_monotonic=self.capture_ended_monotonic,
            topology_kind=self.topology.kind, topology_sha256=self.topology_sha256,
            capture_sha256=self.capture_sha256,
        )


def freeze_image(
    image: Image.Image, *, frame_id: str, generation: int,
    requested_bbox: BBox, actual_bbox: BBox,
    capture_started_monotonic: float, capture_ended_monotonic: float,
    topology_before: TopologyDescriptor, topology_after: TopologyDescriptor,
) -> FrozenFrame:
    """Freeze caller-provided pixels after validating explicitly supplied bounds.

    This converts an in-memory Pillow image to tightly packed RGB8. It does not
    capture a desktop or infer the real clipped rectangle from image dimensions.
    Decoder success is neither an input nor a condition. The caller owns image
    acquisition, UUID uniqueness, generation freshness and topology completeness.
    """
    if not isinstance(image, Image.Image):
        raise TypeError("image must be a caller-provided Pillow image")
    if not isinstance(topology_before, TopologyDescriptor) or not isinstance(topology_after, TopologyDescriptor):
        raise TypeError("both acquisition topology snapshots must use the explicit descriptor type")
    if topology_before.digest() != topology_after.digest():
        raise ValueError("display topology changed during acquisition")
    expected = clip_capture_bbox(requested_bbox, topology_before.virtual_bbox)
    _validate_bbox(actual_bbox, "actual_bbox")
    if actual_bbox != expected:
        raise ValueError("actual_bbox differs from the acquisition-layer clip")
    if image.size != (actual_bbox[2] - actual_bbox[0], actual_bbox[3] - actual_bbox[1]):
        raise ValueError("captured image dimensions differ from explicit actual_bbox")
    pixels = image.convert("RGB").tobytes()
    return FrozenFrame(
        frame_id=frame_id, generation=generation,
        requested_bbox=requested_bbox, actual_bbox=actual_bbox,
        width=image.width, height=image.height, rgb8=pixels,
        capture_started_monotonic=capture_started_monotonic,
        capture_ended_monotonic=capture_ended_monotonic,
        topology=topology_after,
    )
