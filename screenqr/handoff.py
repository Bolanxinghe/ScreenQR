"""Pure single-request handoff gates; no window, capture, input, or success API.

Receipts carry adapter-supplied observations. This module validates their values,
causal order, identity and hashes; it cannot prove that a caller really queried
Windows or acquired the supplied bytes. The real adapters remain responsible for
that provenance. A read-only visual candidate is never a dispatch permit.

consume_commit_once is the only permit-producing operation. It consumes the
request BEFORE external input and leaves OUTCOME_UNKNOWN even if input fails.
There is deliberately no ENTRY_VERIFIED, native-acceptance or automatic retry API.
"""
from __future__ import annotations

from .profiles import DEFAULT_PROFILE, current_profile

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import ntpath
import re
import threading
import time
from typing import Callable, Mapping
from uuid import UUID, uuid4

from .frozen_frame import FrozenFrame

BBox = tuple[int, int, int, int]
_PIXEL_PROOF = object()
_DISPLAY_PROOF = object()
EXACT_SOURCE_RGB = "exact_source_rgb"
NATIVE_DISPLAY_PROFILE_A = "native_display_profile_A"
PROFILE_A_ID = DEFAULT_PROFILE.display_id
_PROFILE_A_VERSION = DEFAULT_PROFILE.version
_PROFILE_A_SCREEN = DEFAULT_PROFILE.screen
_PROFILE_A_TABLE = bytes(max(value-1, 0) for value in range(256))
_EXTENT_PROOF = object()
_STAGE_PROOF = object()
_PROBE_PROOF = object()
_ABSENCE_PROOF = object()
_OBSERVATION_PROOF = object()


def _integer(value: object, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _rect(value: object) -> BBox:
    if (not isinstance(value, tuple) or len(value) != 4
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
            or any(not -(2**31) <= v < 2**31 for v in value)
            or value[2] <= value[0] or value[3] <= value[1]):
        raise ValueError("a nonempty physical-pixel rectangle is required")
    return value


def _inside(inner: BBox, outer: BBox) -> bool:
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


def _uuid(value: object) -> str:
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or not parsed.int or str(parsed) != value:
        raise ValueError("a nonzero canonical UUID is required")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("a lowercase SHA-256 is required")
    return value


def _interval(start: int, end: int) -> None:
    if not _integer(start) or not _integer(end) or end < start:
        raise ValueError("a monotonic-nanosecond interval is required")


class State(str, Enum):
    CREATED = "CREATED"
    STAGE_BOUND = "STAGE_BOUND"
    BASELINE_ABSENT = "BASELINE_ABSENT"
    TRIGGER_ATTEMPTED = "TRIGGER_ATTEMPTED"
    CAPTURE_BOUND = "CAPTURE_BOUND"
    SELECTION_VERIFIED = "SELECTION_VERIFIED"
    OBSERVING = "OBSERVING"
    OBSERVATION_STABLE = "OBSERVATION_STABLE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    TIMED_OUT = "TIMED_OUT"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class GateRejected(ValueError):
    pass


class NativeDisplayPredictionMismatch(ValueError):
    """All other display-factory checks passed; exact fixed F did not match.

    This is not a claim that drawing will settle or that the content is valid.
    No receipt/permission is produced. A controller may only take another fresh
    read within the original request deadline, preserving every strict check.
    """


@dataclass(frozen=True, slots=True)
class Policy:
    # Callers must select and record measured timing limits explicitly. Supplying
    # limits is not evidence of meeting the product's original latency targets.
    request_budget_ns: int | None
    max_evidence_age_ns: int
    max_observation_gap_ns: int
    max_commit_visual_age_ns: int
    expected_executable: str = field(default_factory=lambda: current_profile().executable)
    expected_file_version: tuple[int, int, int, int] = field(default_factory=lambda: current_profile().version)
    expected_window_class: str = field(default_factory=lambda: current_profile().window_class)
    expected_screen: BBox = field(default_factory=lambda: current_profile().screen)
    expected_dpi: int = field(default_factory=lambda: current_profile().dpi)
    selection_evidence_mode: str = EXACT_SOURCE_RGB
    # None selects a job with no wall-clock expiry. Individual native calls are
    # still short, and receipts keep the same strict freshness/identity gates.
    operation_budget_ns: int = 2_000_000_000
    max_pending_observations: int = 32

    def __post_init__(self) -> None:
        if ((self.request_budget_ns is not None and not _integer(self.request_budget_ns, 1))
                or not all(_integer(v, 1) for v in (self.max_evidence_age_ns,
                self.max_observation_gap_ns, self.max_commit_visual_age_ns,
                self.operation_budget_ns, self.max_pending_observations))):
            raise ValueError("explicit positive timing limits are required")
        if self.max_commit_visual_age_ns >= self.max_evidence_age_ns:
            raise ValueError("commit visual age must be explicitly tighter than general evidence age")
        _rect(self.expected_screen)
        if not _integer(self.expected_dpi, 1) or not ntpath.isabs(self.expected_executable):
            raise ValueError("explicit native profile is required")
        if (not isinstance(self.expected_file_version, tuple) or len(self.expected_file_version) != 4
                or not all(_integer(v) for v in self.expected_file_version)):
            raise ValueError("explicit fixed file version is required")
        if self.selection_evidence_mode not in (EXACT_SOURCE_RGB, NATIVE_DISPLAY_PROFILE_A):
            raise ValueError("an explicit supported selection evidence mode is required")
        if self.selection_evidence_mode == NATIVE_DISPLAY_PROFILE_A and (
                self.expected_file_version != current_profile().version or self.expected_dpi != current_profile().dpi
                or self.expected_screen != current_profile().screen or self.expected_window_class != current_profile().window_class):
            raise ValueError("display profile A is limited to WeChat 4.1.15.13/DPI192/3200x2000")


@dataclass(frozen=True, slots=True)
class FrameBinding:
    frame_id: str
    frame_sha256: str
    generation: int
    topology_sha256: str
    width: int
    height: int

    @classmethod
    def from_frame(cls, frame: FrozenFrame) -> FrameBinding:
        if not isinstance(frame, FrozenFrame):
            raise TypeError("a FrozenFrame is required")
        return cls(frame.frame_id, frame.capture_sha256, frame.generation,
                   frame.topology_sha256, frame.width, frame.height)


@dataclass(frozen=True, slots=True)
class StageReceipt:
    source: FrameBinding
    stage_id: str
    stage_epoch: int
    payload_bbox_physical: BBox
    transport_bbox_physical: BBox
    transport_sha256: str
    checked_ns: int
    _own_stage_proof: object = field(default=None, repr=False, compare=False)

    @classmethod
    def from_stage_verification(cls, frame: FrozenFrame, verification: object) -> StageReceipt:
        """Consume stage.py's actual-own-pixels verification, not a visible flag."""
        source = FrameBinding.from_frame(frame)
        mapping = verification.mapping
        if (mapping.frame_id != source.frame_id or mapping.source_capture_sha256 != source.frame_sha256
                or mapping.generation != source.generation or mapping.topology_sha256 != source.topology_sha256
                or verification.rendered_payload_sha256 != source.frame_sha256
                or verification.rendered_transport_sha256 != verification.expected_transport_sha256):
            raise ValueError("stage verification does not match the frozen source/transport")
        payload, transport = _rect(mapping.payload_bbox_physical), _rect(mapping.transport_bbox_physical)
        if (payload[2]-payload[0], payload[3]-payload[1]) != (source.width, source.height) or not _inside(payload, transport):
            raise ValueError("stage payload bounds differ from frozen source dimensions")
        _uuid(mapping.stage_id)
        if not _integer(mapping.stage_epoch) or not _integer(verification.checked_monotonic_ns):
            raise ValueError("stage epoch/time is invalid")
        return cls(source, mapping.stage_id, mapping.stage_epoch, payload, transport,
                   _sha(verification.rendered_transport_sha256), verification.checked_monotonic_ns, _STAGE_PROOF)


@dataclass(frozen=True, slots=True)
class NativeInstance:
    hwnd: int
    pid: int
    process_created_filetime_100ns: int
    executable_path: str
    title: str
    window_class: str
    window_rect: BBox
    client_screen_rect: BBox
    dpi: int
    fixed_file_version: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        if (not all(_integer(v, 1) for v in (self.hwnd, self.pid, self.process_created_filetime_100ns, self.dpi))
                or not isinstance(self.executable_path, str) or not ntpath.isabs(self.executable_path)
                or not isinstance(self.title, str) or not isinstance(self.window_class, str)
                or not isinstance(self.fixed_file_version, tuple) or len(self.fixed_file_version) != 4
                or not all(_integer(v) for v in self.fixed_file_version)):
            raise ValueError("complete native instance metadata is required")
        _rect(self.window_rect); _rect(self.client_screen_rect)

    def matches(self, policy: Policy) -> bool:
        return bool(
            all(_integer(v, 1) for v in (self.hwnd, self.pid, self.process_created_filetime_100ns, self.dpi))
            and isinstance(self.executable_path, str)
            and ntpath.normcase(ntpath.normpath(self.executable_path)) == ntpath.normcase(ntpath.normpath(policy.expected_executable))
            and self.title == "Weixin" and self.window_class == policy.expected_window_class
            and self.window_rect == policy.expected_screen and self.client_screen_rect == policy.expected_screen
            and self.dpi == policy.expected_dpi and self.fixed_file_version == policy.expected_file_version
        )


@dataclass(frozen=True, slots=True)
class NativeSnapshot:
    instance: NativeInstance
    started_ns: int
    finished_ns: int
    foreground_before: int
    foreground_after: int
    monitor_count: int
    primary_screen_rect: BBox
    thread_dpi_awareness: int
    _metadata_probe_proof: object = field(default=None, repr=False, compare=False)

    @classmethod
    def from_probe(cls, probe: object, *, started_ns: int, finished_ns: int,
                   fixed_file_version: tuple[int, int, int, int]) -> NativeSnapshot:
        """Adapter supplies bracket times and an independently read file version."""
        data = probe.to_dict() if hasattr(probe, "to_dict") else probe
        if (not isinstance(data, Mapping) or data.get("status") != "UNIQUE"
                or not isinstance(data.get("errors"), (tuple, list)) or data["errors"]):
            raise ValueError("an error-free unique native probe is required")
        candidates = data.get("candidates")
        if not isinstance(candidates, (tuple, list)) or len(candidates) != 1:
            raise ValueError("native instance is not unique")
        item = candidates[0]
        if not isinstance(item, Mapping) or item.get("foreground_match") is not True:
            raise ValueError("native instance was not foreground")
        instance = NativeInstance(
            item["hwnd"], item["pid"], item["process_created_filetime_100ns"],
            item["executable_path"], item["title"], item["window_class"],
            tuple(item["window_rect"]), tuple(item["client_screen_rect"]),
            item["dpi"], fixed_file_version,
        )
        _interval(started_ns, finished_ns)
        if data.get("foreground_hwnd_at_start") != instance.hwnd or data.get("foreground_hwnd_at_end") != instance.hwnd:
            raise ValueError("native foreground changed across the probe")
        return cls(instance, started_ns, finished_ns, instance.hwnd, instance.hwnd,
                   data.get("monitor_count"), tuple(data.get("primary_screen_rect") or ()),
                   data.get("thread_dpi_awareness"), _PROBE_PROOF)


@dataclass(frozen=True, slots=True)
class AbsenceReceipt:
    request_id: str
    evidence_id: str
    started_ns: int
    finished_ns: int
    _absent_probe_proof: object = field(default=None, repr=False, compare=False)

    @classmethod
    def from_probe(cls, request_id: str, probe: object, *, started_ns: int, finished_ns: int) -> AbsenceReceipt:
        data = probe.to_dict() if hasattr(probe, "to_dict") else probe
        if (not isinstance(data, Mapping) or data.get("status") != "NOT_FOUND"
                or not isinstance(data.get("candidates"), (tuple, list))
                or len(data["candidates"]) != 0
                or not isinstance(data.get("errors"), (tuple, list)) or data["errors"]):
            raise ValueError("a complete error-free absent baseline is required")
        _interval(started_ns, finished_ns)
        return cls(_uuid(request_id), str(uuid4()), started_ns, finished_ns, _ABSENCE_PROOF)


@dataclass(frozen=True, slots=True)
class CaptureReceipt:
    request_id: str
    absence_evidence_id: str
    trigger_id: str
    snapshot: NativeSnapshot


@dataclass(frozen=True, slots=True)
class SelectionExtentReceipt:
    # Produced by a real border/extent observer. It must NOT be constructed from
    # the drag coordinates alone. This pure model cannot attest that provenance.
    request_id: str
    evidence_id: str
    native_instance: NativeInstance
    actual_selection_bbox_physical: BBox
    measured_ns: int
    method: str
    _observed_boundary_proof: object = field(default=None, repr=False, compare=False)

    @classmethod
    def from_native_extent_evidence(cls, *, stage: StageReceipt, native_instance: NativeInstance,
                                    evidence: Mapping) -> SelectionExtentReceipt:
        """Require independently VERIFIED edges; never copy requested to observed.

        The current native_selection_observer always reports UNVERIFIED/None,
        which this factory MUST reject. A future actual boundary observer must
        supply this explicit schema; this factory itself detects no border.
        """
        if (not isinstance(evidence, Mapping) or evidence.get("schema_version") != 1
                or evidence.get("selection_extent_status") != "VERIFIED"
                or evidence.get("observed_selection_bbox_physical") is None
                or evidence.get("extent_verification_method") != "native-client-observed-boundaries-v1"):
            raise ValueError("independent native selection extent remains unverified")
        if (evidence.get("frame_id") != stage.source.frame_id
                or evidence.get("stage_id") != stage.stage_id or evidence.get("stage_epoch") != stage.stage_epoch
                or evidence.get("generation") != stage.source.generation
                or not _integer(evidence.get("stage_epoch")) or not _integer(evidence.get("generation"))
                or evidence.get("source_capture_sha256") != stage.source.frame_sha256
                or evidence.get("topology_sha256") != stage.source.topology_sha256):
            raise ValueError("extent evidence belongs to a different stage/source")
        started, finished = evidence.get("started_monotonic_ns"), evidence.get("finished_monotonic_ns")
        _interval(started, finished)
        observed = _rect(tuple(evidence["observed_selection_bbox_physical"]))
        if observed != stage.transport_bbox_physical:
            raise ValueError("independently observed extent differs from requested transport")
        for key in ("native_identity_before", "native_identity_after"):
            snapshot = NativeSnapshot.from_probe(evidence.get(key), started_ns=started,
                finished_ns=finished, fixed_file_version=native_instance.fixed_file_version)
            if snapshot.instance != native_instance:
                raise ValueError("extent native instance changed")
            if (snapshot.monitor_count != 1 or snapshot.primary_screen_rect != native_instance.window_rect
                    or snapshot.thread_dpi_awareness != 2):
                raise ValueError("extent native display/DPI profile changed")
        return cls(_uuid(evidence.get("session_id")), _uuid(evidence.get("evidence_id")),
                   native_instance, observed, finished, evidence["extent_verification_method"], _EXTENT_PROOF)


@dataclass(frozen=True, slots=True)
class SelectionReceipt:
    request_id: str
    stage: StageReceipt
    extent: SelectionExtentReceipt
    capture_started_ns: int
    capture_finished_ns: int
    acquired_transport_sha256: str | None
    acquired_payload_sha256: str
    _computed_pixel_proof: object = field(default=None, repr=False, compare=False)

    @classmethod
    def from_acquired_pixels(cls, *, stage: StageReceipt, extent: SelectionExtentReceipt,
                             native_instance: NativeInstance, acquired_bbox_physical: BBox,
                             rgb8: bytes, capture_started_ns: int, capture_finished_ns: int) -> SelectionReceipt:
        """Hash actual native selection pixels; accept no content_verified flag.

        Adapter must acquire these bytes from the verified screenshot instance,
        and obtain extent from actual border pixels. Source payload must remain
        exact; native decoration may differ only outside that payload rectangle.
        Bytes are hashed synchronously and are not retained in this receipt.
        """
        if not isinstance(stage, StageReceipt) or not isinstance(extent, SelectionExtentReceipt):
            raise TypeError("typed stage and independently measured extent are required")
        _uuid(extent.request_id); _uuid(extent.evidence_id)
        _interval(capture_started_ns, capture_finished_ns)
        if (extent._observed_boundary_proof is not _EXTENT_PROOF
                or extent.method != "native-client-observed-boundaries-v1" or not _integer(extent.measured_ns)
                or extent.native_instance != native_instance
                or _rect(extent.actual_selection_bbox_physical) != stage.transport_bbox_physical
                or _rect(acquired_bbox_physical) != stage.transport_bbox_physical):
            raise ValueError("native instance, measured extent and actual captured rectangle must match the stage")
        left, top, right, bottom = acquired_bbox_physical
        width, height = right-left, bottom-top
        if type(rgb8) is not bytes or len(rgb8) != width*height*3:
            raise ValueError("immutable tightly packed acquired RGB8 pixels are required")
        p = stage.payload_bbox_physical
        digest = hashlib.sha256()
        for y in range(p[1]-top, p[3]-top):
            offset = (y*width + p[0]-left)*3
            digest.update(memoryview(rgb8)[offset:offset+(p[2]-p[0])*3])
        payload_sha = digest.hexdigest()
        if payload_sha != stage.source.frame_sha256:
            raise ValueError("actual native selection payload differs from the immutable source")
        return cls(extent.request_id, stage, extent, capture_started_ns, capture_finished_ns,
                   hashlib.sha256(rgb8).hexdigest(), payload_sha, _PIXEL_PROOF)

    @classmethod
    def from_native_selection_evidence(cls, *, stage: StageReceipt, native_instance: NativeInstance,
                                       evidence: Mapping) -> SelectionReceipt:
        """Consume the native helper's computed exact-byte/hash comparison.

        This avoids returning/reacquiring private pixels. The native helper is
        the provenance boundary; matching a bare caller bbox or boolean alone
        is insufficient. Its current UNVERIFIED output is rejected first.
        """
        extent = SelectionExtentReceipt.from_native_extent_evidence(
            stage=stage, native_instance=native_instance, evidence=evidence)
        comparison = evidence.get("payload_comparison")
        if (not isinstance(comparison, Mapping) or comparison.get("exact_rgb_bytes_equal") is not True
                or comparison.get("captured_rgb_sha256") != stage.source.frame_sha256
                or comparison.get("expected_rgb_sha256") != stage.source.frame_sha256
                or tuple(comparison.get("captured_bbox_physical") or ()) != stage.payload_bbox_physical
                or comparison.get("width") != stage.source.width or comparison.get("height") != stage.source.height):
            raise ValueError("native helper did not verify exact source payload bytes and dimensions")
        started, finished = evidence["started_monotonic_ns"], evidence["finished_monotonic_ns"]
        completed = evidence.get("capture_completed_monotonic_ns")
        if not _integer(completed) or not started <= completed <= finished:
            raise ValueError("native selection capture interval is incomplete")
        # This helper acquires the payload and edge strips rather than the whole
        # transport. The payload digest is recorded twice; no transport digest is
        # invented. A separately acquired transport digest is optional evidence.
        return cls(extent.request_id, stage, extent, started, finished, None,
                   comparison["captured_rgb_sha256"], _PIXEL_PROOF)


@dataclass(frozen=True, slots=True)
class ProfiledDisplayReceipt:
    """Strict predicted native display, deliberately not original stored bytes.

    F(v)=max(v-1,0) is non-injective (0 and 1 collide). The frozen source stays
    unchanged. A real pixel producer supplies the observed hash; this pure factory
    binds its facts, never attests acquisition provenance or inverts actual pixels.
    Only an explicit A policy accepts this receipt. No business success is implied.
    """
    request_id: str
    evidence_id: str
    stage: StageReceipt
    extent: SelectionExtentReceipt
    capture_started_ns: int
    capture_finished_ns: int
    source_rgb_sha256: str
    expected_display_sha256: str
    observed_display_sha256: str
    payload_bbox_physical: BBox
    width: int
    height: int
    snapshot_before: NativeSnapshot
    snapshot_after: NativeSnapshot
    checked_ns: int
    evidence_deadline_ns: int
    profile_id: str = field(default_factory=lambda: current_profile().display_id)
    raw_payload_verified: bool = field(default=False, init=False)
    evidence_scope: str = field(default="strict expected native display; non-injective F; not original stored bytes", init=False)
    _profiled_display_proof: object = field(default=None, repr=False, compare=False)

    @classmethod
    def from_native_display_evidence(cls, *, frame: FrozenFrame, stage: StageReceipt,
            extent: SelectionExtentReceipt, native_instance: NativeInstance, evidence: Mapping,
            profile_id: str | None = None, clock: Callable[[], int] = time.monotonic_ns) -> ProfiledDisplayReceipt:
        profile_id = current_profile().display_id if profile_id is None else profile_id
        if profile_id != current_profile().display_id:
            raise ValueError("unknown display profile")
        if (not isinstance(frame, FrozenFrame) or not isinstance(stage, StageReceipt)
                or stage._own_stage_proof is not _STAGE_PROOF or stage.source != FrameBinding.from_frame(frame)
                or not isinstance(extent, SelectionExtentReceipt) or extent._observed_boundary_proof is not _EXTENT_PROOF
                or extent.method != "native-client-observed-boundaries-v1"
                or extent.native_instance != native_instance
                or extent.actual_selection_bbox_physical != stage.transport_bbox_physical):
            raise ValueError("display requires the matching frozen source/stage and independent extent")
        topology = frame.topology
        if (topology.virtual_bbox != current_profile().screen or len(topology.monitors) != 1
                or topology.monitors[0].physical_bbox != current_profile().screen
                or (topology.monitors[0].dpi_x, topology.monitors[0].dpi_y) != (current_profile().dpi, current_profile().dpi)
                or topology.monitors[0].rotation_degrees != 0):
            raise ValueError("frozen source topology is outside display profile A")
        if (not isinstance(evidence, Mapping) or evidence.get("schema_version") != 1
                or evidence.get("status") not in ("PAYLOAD_MISMATCH", "PAYLOAD_MATCH_EXTENT_UNVERIFIED")
                or evidence.get("errors") not in ([], ()) or evidence.get("read_only") is not True
                or evidence.get("native_input_sent") is not False or evidence.get("raw_pixels_persisted") is not False):
            raise ValueError("a completed read-only native payload observation is required")
        if (evidence.get("frame_id") != frame.frame_id or evidence.get("stage_id") != stage.stage_id
                or not _integer(evidence.get("generation")) or evidence["generation"] != frame.generation
                or not _integer(evidence.get("stage_epoch")) or evidence["stage_epoch"] != stage.stage_epoch
                or evidence.get("source_capture_sha256") != frame.capture_sha256
                or evidence.get("topology_sha256") != frame.topology_sha256
                or _uuid(evidence.get("session_id")) != extent.request_id
                or _uuid(evidence.get("evidence_id")) != extent.evidence_id):
            raise ValueError("display observation and independently observed extent have different bindings")
        started, finished = evidence.get("started_monotonic_ns"), evidence.get("finished_monotonic_ns")
        completed, deadline = evidence.get("capture_completed_monotonic_ns"), evidence.get("deadline_monotonic_ns")
        _interval(started, finished)
        if (not _integer(completed) or not _integer(deadline) or not _integer(extent.measured_ns)
                or not stage.checked_ns <= started <= completed <= finished < deadline
                or deadline-started > 2_000_000_000 or not started <= extent.measured_ns <= finished):
            raise ValueError("display observation/extent timing is incomplete or expired")
        now = clock()
        if not _integer(now) or not finished <= now < deadline:
            raise ValueError("display observation is not current")
        versions = evidence.get("version_metadata")
        if not isinstance(versions, Mapping):
            raise ValueError("display native version is missing")
        for key in ("fixed_file_version", "expected_fixed_file_version"):
            value = versions.get(key)
            if (not isinstance(value, (tuple, list)) or not all(_integer(v) for v in value)
                    or tuple(value) != current_profile().version):
                raise ValueError("native version is outside display profile A")
        snapshots = []
        for key in ("native_identity_before", "native_identity_after"):
            snapshot = NativeSnapshot.from_probe(evidence.get(key), started_ns=started,
                finished_ns=finished, fixed_file_version=current_profile().version)
            native = snapshot.instance
            if (native != native_instance or native.fixed_file_version != current_profile().version or native.dpi != current_profile().dpi
                    or native.window_rect != current_profile().screen or native.client_screen_rect != current_profile().screen
                    or native.window_class != current_profile().window_class or native.title != current_profile().title
                    or snapshot.monitor_count != 1 or snapshot.primary_screen_rect != current_profile().screen
                    or snapshot.thread_dpi_awareness != 2):
                raise ValueError("native instance/display changed or is outside display profile A")
            snapshots.append(snapshot)
        comparison = evidence.get("payload_comparison")
        if not isinstance(comparison, Mapping):
            raise ValueError("native payload comparison is missing")
        observed = _sha(comparison.get("captured_rgb_sha256"))
        pixel_start, pixel_end = comparison.get("capture_started_monotonic_ns"), comparison.get("capture_ended_monotonic_ns")
        _interval(pixel_start, pixel_end)
        if (comparison.get("expected_rgb_sha256") != frame.capture_sha256
                or type(comparison.get("width")) is not int or comparison["width"] != frame.width
                or type(comparison.get("height")) is not int or comparison["height"] != frame.height
                or tuple(comparison.get("captured_bbox_physical") or ()) != stage.payload_bbox_physical
                or not started <= pixel_start <= pixel_end <= completed
                or type(comparison.get("exact_rgb_bytes_equal")) is not bool
                or comparison["exact_rgb_bytes_equal"] != (observed == frame.capture_sha256)):
            raise ValueError("native payload dimensions/bounds/source/times are inconsistent")
        # A forward prediction only: never modify frame.rgb8 or transform observed pixels.
        predicted = hashlib.sha256(frame.rgb8.translate(_PROFILE_A_TABLE)).hexdigest()
        checked = clock()
        if not _integer(checked) or not now <= checked < deadline:
            raise ValueError("display observation expired during verification")
        if observed != predicted:
            raise NativeDisplayPredictionMismatch("native display differs from the exact preregistered forward prediction")
        return cls(extent.request_id, extent.evidence_id, stage, extent, started, finished,
            frame.capture_sha256, predicted, observed, stage.payload_bbox_physical, frame.width,
            frame.height, snapshots[0], snapshots[1], checked, deadline,
            _profiled_display_proof=_DISPLAY_PROOF)


@dataclass(frozen=True, slots=True)
class ObservationReceipt:
    request_id: str
    generation: int
    observation_id: int
    snapshot_before: NativeSnapshot
    snapshot_after: NativeSnapshot
    selection_bbox_physical: BBox
    candidate_bbox_physical: BBox | None
    started_ns: int
    finished_ns: int
    status: str
    _native_observation_proof: object = field(default=None, repr=False, compare=False)
    # No click-permission field exists. Real observer adapter also validates its
    # frozen calibration/profile, source provenance and independent pre/post probe.

    @classmethod
    def from_native_observation(cls, observation: object, *, observation_id: int) -> ObservationReceipt:
        data = observation.to_dict() if hasattr(observation, "to_dict") else observation
        if (not isinstance(data, Mapping) or data.get("read_only") is not True
                or not isinstance(data.get("errors"), (tuple, list)) or data["errors"]
                or data.get("status") not in ("READ_ONLY_CANDIDATE", "NOT_READY", "AMBIGUOUS")):
            raise ValueError("an eligible read-only native observation is required")
        started, finished = data.get("started_ns"), data.get("finished_ns")
        _interval(started, finished)
        version_metadata = data.get("version_metadata")
        version = version_metadata.get("fixed_file_version") if isinstance(version_metadata, Mapping) else None
        if not isinstance(version, (tuple, list)):
            raise ValueError("native observation has no fixed version metadata")
        # The native observer does not expose individual probe bracket times.
        # These are honestly enclosing observation intervals, not invented
        # exact probe timestamps; its pre/post order comes from that adapter.
        before = NativeSnapshot.from_probe(data.get("identity_before"), started_ns=started,
            finished_ns=finished, fixed_file_version=tuple(version))
        after = NativeSnapshot.from_probe(data.get("identity_after"), started_ns=started,
            finished_ns=finished, fixed_file_version=tuple(version))
        candidate = data.get("candidate_bbox_physical")
        return cls(_uuid(data.get("session_id")), data.get("generation"), observation_id,
                   before, after, tuple(data.get("selection_bbox_physical") or ()),
                   tuple(candidate) if candidate is not None else None, started, finished, data["status"], _OBSERVATION_PROOF)


@dataclass(frozen=True, slots=True)
class CommitCheck:
    request_id: str
    current_source: FrameBinding
    current_stage_id: str
    current_stage_epoch: int
    current_transport_bbox_physical: BBox
    current_generation: int
    current_topology_sha256: str
    latest_selection: SelectionReceipt | ProfiledDisplayReceipt
    latest_visual: ObservationReceipt
    latest_native: NativeSnapshot
    candidate_bbox_physical: BBox


@dataclass(frozen=True, slots=True)
class CommitPermit:
    request_id: str
    frame_id: str
    dispatch_id: str
    consumed_ns: int
    candidate_bbox_physical: BBox


class HandoffSession:
    """One serialized request. No receipt itself performs an application action."""

    def __init__(self, frame: FrozenFrame, *, policy: Policy,
                 clock: Callable[[], int] = time.monotonic_ns) -> None:
        if not isinstance(policy, Policy):
            raise TypeError("an explicit Policy is required")
        self.request_id = str(uuid4())
        self.source = FrameBinding.from_frame(frame)  # Does not retain pixel bytes.
        self.policy, self._clock = policy, clock
        self._lock = threading.Lock()
        self._started_ns = self._last_now = clock()
        if not _integer(self._started_ns):
            raise ValueError("clock must return monotonic nanoseconds")
        self.deadline_ns = (self._started_ns + policy.request_budget_ns
                            if policy.request_budget_ns is not None else None)
        self._state, self._reason = State.CREATED, "created"
        self._stage = self._absence = self._capture = self._selection = None
        self._trigger_id = None
        self._trigger_ns = None
        self._observations: list[ObservationReceipt] = []
        self._last_observation_id = self._last_observation_finished_ns = None
        self._commit_attempts = 0

    def _fail(self, reason: str) -> None:
        if self._state not in (State.CANCELLED, State.REJECTED, State.TIMED_OUT, State.OUTCOME_UNKNOWN):
            self._state = State.REJECTED
        self._reason = reason
        self._observations.clear()
        raise GateRejected(reason)

    def _now(self) -> int:
        now = self._clock()
        if not _integer(now) or now < self._last_now:
            self._fail("monotonic_clock_invalid_or_regressed")
        self._last_now = now
        if self._state in (State.CANCELLED, State.REJECTED, State.TIMED_OUT, State.OUTCOME_UNKNOWN):
            raise GateRejected("request_is_terminal:" + self._state.value)
        if self.deadline_ns is not None and now >= self.deadline_ns:
            self._state = State.TIMED_OUT
            self._fail("request_deadline_expired")
        return now

    def _expect(self, *states: State) -> None:
        if self._state not in states:
            self._fail("unexpected_state:" + self._state.value)

    def _fresh(self, start: int, end: int, now: int, *, after: int) -> None:
        if (not _integer(start) or not _integer(end) or not after <= start <= end <= now
                or now-end > self.policy.max_evidence_age_ns):
            self._fail("evidence_out_of_order_future_or_stale")

    def _native(self, snapshot: NativeSnapshot, now: int, *, after: int) -> None:
        if (not isinstance(snapshot, NativeSnapshot) or snapshot._metadata_probe_proof is not _PROBE_PROOF
                or not snapshot.instance.matches(self.policy)):
            self._fail("native_instance_outside_profile")
        if snapshot.foreground_before != snapshot.instance.hwnd or snapshot.foreground_after != snapshot.instance.hwnd:
            self._fail("native_instance_not_stably_foreground")
        if (snapshot.monitor_count != 1 or snapshot.primary_screen_rect != self.policy.expected_screen
                or snapshot.thread_dpi_awareness != 2):
            self._fail("native_display_or_dpi_profile_changed")
        self._fresh(snapshot.started_ns, snapshot.finished_ns, now, after=after)

    def bind_stage(self, receipt: StageReceipt) -> None:
        with self._lock:
            now = self._now(); self._expect(State.CREATED)
            if (not isinstance(receipt, StageReceipt) or receipt._own_stage_proof is not _STAGE_PROOF
                    or receipt.source != self.source):
                self._fail("stage_source_mismatch")
            try:
                _uuid(receipt.stage_id); _sha(receipt.transport_sha256)
                _rect(receipt.payload_bbox_physical); _rect(receipt.transport_bbox_physical)
            except ValueError:
                self._fail("stage_fields_invalid")
            if (not _integer(receipt.stage_epoch) or any(v % 2 for v in receipt.transport_bbox_physical)
                    or not _inside(receipt.transport_bbox_physical, self.policy.expected_screen)
                    or not _inside(receipt.payload_bbox_physical, receipt.transport_bbox_physical)
                    or (receipt.payload_bbox_physical[2]-receipt.payload_bbox_physical[0], receipt.payload_bbox_physical[3]-receipt.payload_bbox_physical[1]) != (self.source.width, self.source.height)):
                self._fail("stage_extent_outside_current_even_profile")
            self._fresh(receipt.checked_ns, receipt.checked_ns, now, after=self._started_ns)
            self._stage, self._state = receipt, State.STAGE_BOUND

    def record_absence(self, receipt: AbsenceReceipt) -> None:
        with self._lock:
            now = self._now(); self._expect(State.STAGE_BOUND)
            if (not isinstance(receipt, AbsenceReceipt) or receipt._absent_probe_proof is not _ABSENCE_PROOF
                    or receipt.request_id != self.request_id):
                self._fail("absence_request_mismatch")
            try:
                _uuid(receipt.evidence_id)
            except ValueError:
                self._fail("absence_evidence_id_invalid")
            self._fresh(receipt.started_ns, receipt.finished_ns, now, after=self._stage.checked_ns)
            self._absence, self._state = receipt, State.BASELINE_ABSENT

    def mark_trigger_attempt(self) -> str:
        with self._lock:
            now = self._now(); self._expect(State.BASELINE_ABSENT)
            self._fresh(self._absence.started_ns, self._absence.finished_ns, now, after=self._stage.checked_ns)
            self._trigger_id, self._trigger_ns = str(uuid4()), now
            self._state = State.TRIGGER_ATTEMPTED
            return self._trigger_id

    def bind_capture(self, receipt: CaptureReceipt) -> None:
        with self._lock:
            now = self._now(); self._expect(State.TRIGGER_ATTEMPTED)
            if (not isinstance(receipt, CaptureReceipt) or receipt.request_id != self.request_id
                    or receipt.absence_evidence_id != self._absence.evidence_id or receipt.trigger_id != self._trigger_id):
                self._fail("capture_causal_chain_mismatch")
            # A visible instance first observed after an absent baseline/trigger,
            # not a claim that Windows exposed an HWND creation timestamp.
            self._native(receipt.snapshot, now, after=self._trigger_ns)
            if receipt.snapshot.started_ns <= self._trigger_ns:
                self._fail("native_instance_was_not_first_observed_after_trigger")
            self._capture, self._state = receipt, State.CAPTURE_BOUND

    def bind_selection(self, receipt: SelectionReceipt | ProfiledDisplayReceipt) -> None:
        with self._lock:
            now = self._now(); self._expect(State.CAPTURE_BOUND)
            self._selection_evidence(receipt, now, after=self._capture.snapshot.finished_ns)
            self._selection, self._state = receipt, State.SELECTION_VERIFIED

    def _selection_evidence(self, receipt: SelectionReceipt | ProfiledDisplayReceipt,
                            now: int, *, after: int) -> None:
        if self.policy.selection_evidence_mode == EXACT_SOURCE_RGB:
            proof = (isinstance(receipt, SelectionReceipt) and receipt._computed_pixel_proof is _PIXEL_PROOF
                     and receipt.acquired_payload_sha256 == self.source.frame_sha256)
        else:
            proof = (isinstance(receipt, ProfiledDisplayReceipt) and receipt._profiled_display_proof is _DISPLAY_PROOF
                     and receipt.profile_id == current_profile().display_id and receipt.raw_payload_verified is False
                     and receipt.source_rgb_sha256 == self.source.frame_sha256
                     and receipt.expected_display_sha256 == receipt.observed_display_sha256
                     and receipt.payload_bbox_physical == self._stage.payload_bbox_physical
                     and (receipt.width, receipt.height) == (self.source.width, self.source.height))
        if (not proof or receipt.request_id != self.request_id or receipt.stage != self._stage
                or receipt.extent.native_instance != self._capture.snapshot.instance
                or receipt.extent._observed_boundary_proof is not _EXTENT_PROOF
                or receipt.extent.actual_selection_bbox_physical != self._stage.transport_bbox_physical):
            self._fail("selection_lacks_matching_native_extent_and_computed_pixel_proof"
                       if self.policy.selection_evidence_mode == EXACT_SOURCE_RGB
                       else "selection_lacks_matching_native_extent_and_profiled_display_proof")
        self._fresh(receipt.extent.measured_ns, receipt.extent.measured_ns, now, after=after)
        self._fresh(receipt.capture_started_ns, receipt.capture_finished_ns, now, after=after)
        if receipt.extent.measured_ns > receipt.capture_finished_ns:
            self._fail("extent_was_measured_after_the_selection_receipt")
        if isinstance(receipt, ProfiledDisplayReceipt):
            self._fresh(receipt.checked_ns, receipt.checked_ns, now, after=receipt.capture_finished_ns)
            if now >= receipt.evidence_deadline_ns:
                self._fail("profiled_display_evidence_expired")
            for snapshot in (receipt.snapshot_before, receipt.snapshot_after):
                self._native(snapshot, now, after=after)
                if snapshot.instance != self._capture.snapshot.instance:
                    self._fail("profiled_display_native_instance_changed")

    def observe(self, receipt: ObservationReceipt) -> bool:
        with self._lock:
            now = self._now(); self._expect(State.SELECTION_VERIFIED, State.OBSERVING, State.OBSERVATION_STABLE)
            if (not isinstance(receipt, ObservationReceipt) or receipt._native_observation_proof is not _OBSERVATION_PROOF
                    or receipt.request_id != self.request_id
                    or not _integer(receipt.generation) or receipt.generation != self.source.generation
                    or not _integer(receipt.observation_id)
                    or (self._last_observation_id is not None and receipt.observation_id <= self._last_observation_id)
                    or receipt.selection_bbox_physical != self._stage.transport_bbox_physical):
                self._fail("observation_identity_generation_extent_or_replay_mismatch")
            self._fresh(receipt.started_ns, receipt.finished_ns, now, after=self._selection.capture_finished_ns)
            self._native(receipt.snapshot_before, now, after=self._selection.capture_finished_ns)
            self._native(receipt.snapshot_after, now, after=receipt.started_ns)
            if (receipt.snapshot_before.instance != self._capture.snapshot.instance
                    or receipt.snapshot_after.instance != self._capture.snapshot.instance
                    or not receipt.started_ns <= receipt.snapshot_before.started_ns <= receipt.snapshot_before.finished_ns <= receipt.finished_ns
                    or not receipt.started_ns <= receipt.snapshot_after.started_ns <= receipt.snapshot_after.finished_ns <= receipt.finished_ns
                    or (self._last_observation_finished_ns is not None
                        and receipt.started_ns <= self._last_observation_finished_ns)):
                self._fail("observation_native_instance_or_temporal_sequence_changed")
            previous = self._last_observation_finished_ns
            self._last_observation_id, self._last_observation_finished_ns = receipt.observation_id, receipt.finished_ns
            if previous is not None and receipt.started_ns-previous > self.policy.max_observation_gap_ns:
                self._observations.clear()
            if receipt.status in ("NOT_READY", "AMBIGUOUS"):
                self._observations.clear(); self._state = State.OBSERVING
                return False
            if receipt.status != "READ_ONLY_CANDIDATE" or receipt.candidate_bbox_physical is None:
                self._fail("observation_not_eligible")
            try:
                _rect(receipt.candidate_bbox_physical)
            except ValueError:
                self._fail("candidate_bbox_invalid")
            if not _inside(receipt.candidate_bbox_physical, self.policy.expected_screen):
                self._fail("candidate_outside_native_client")
            if self._observations and any(r.candidate_bbox_physical != receipt.candidate_bbox_physical for r in self._observations):
                self._observations.clear()
            self._observations.append(receipt)
            self._observations = self._observations[-3:]
            ready = len(self._observations) == 3
            self._state = State.OBSERVATION_STABLE if ready else State.OBSERVING
            return ready

    def consume_commit_once(self, check: CommitCheck) -> CommitPermit:
        with self._lock:
            now = self._now(); self._expect(State.OBSERVATION_STABLE)
            if (not isinstance(check, CommitCheck) or check.request_id != self.request_id
                    or check.current_source != self.source or check.current_stage_id != self._stage.stage_id
                    or check.current_stage_epoch != self._stage.stage_epoch
                    or check.current_transport_bbox_physical != self._stage.transport_bbox_physical
                    or not _integer(check.current_stage_epoch)
                    or not _integer(check.current_generation) or check.current_generation != self.source.generation
                    or check.current_topology_sha256 != self.source.topology_sha256
                    or check.candidate_bbox_physical != self._observations[-1].candidate_bbox_physical
                    or self._commit_attempts != 0):
                self._fail("commit_live_request_stage_source_or_candidate_mismatch")
            last = self._observations[-1]
            self._fresh(last.started_ns, last.finished_ns, now, after=self._selection.capture_finished_ns)
            self._selection_evidence(check.latest_selection, now, after=self._capture.snapshot.finished_ns)
            visual = check.latest_visual
            if (not isinstance(visual, ObservationReceipt) or visual._native_observation_proof is not _OBSERVATION_PROOF
                    or visual.request_id != self.request_id or not _integer(visual.generation)
                    or visual.generation != self.source.generation or not _integer(visual.observation_id)
                    or visual.observation_id < self._last_observation_id
                    or (visual.observation_id == self._last_observation_id and visual != last)
                    or visual.status != "READ_ONLY_CANDIDATE"
                    or visual.selection_bbox_physical != self._stage.transport_bbox_physical
                    or visual.candidate_bbox_physical != last.candidate_bbox_physical):
                self._fail("commit_visual_not_matching_fresh_candidate")
            # The third stable frame may itself be the final visual receipt if
            # it follows the latest selection proof and meets the tighter age.
            # Do not mandate another GDI acquisition solely because count==3.
            after = check.latest_selection.capture_finished_ns
            if visual.observation_id > self._last_observation_id:
                after = max(after, last.finished_ns)
            self._fresh(visual.started_ns, visual.finished_ns, now, after=after)
            if now-visual.finished_ns > self.policy.max_commit_visual_age_ns:
                self._fail("commit_visual_expired")
            for snapshot in (visual.snapshot_before, visual.snapshot_after):
                self._native(snapshot, now, after=visual.started_ns)
                if (snapshot.instance != self._capture.snapshot.instance
                        or not visual.started_ns <= snapshot.started_ns <= snapshot.finished_ns <= visual.finished_ns):
                    self._fail("commit_visual_native_instance_or_interval_changed")
            self._native(check.latest_native, now,
                         after=max(visual.finished_ns, check.latest_selection.capture_finished_ns))
            if check.latest_native.instance != self._capture.snapshot.instance:
                self._fail("commit_native_instance_changed")
            permit = CommitPermit(self.request_id, self.source.frame_id, str(uuid4()), now, check.candidate_bbox_physical)
            self._commit_attempts = 1
            self._state, self._reason = State.OUTCOME_UNKNOWN, "commit_permit_consumed_before_external_input"
            self._observations.clear()
            return permit

    def cancel(self) -> str:
        with self._lock:
            if self._state == State.OUTCOME_UNKNOWN:
                return "TOO_LATE"
            if self._state == State.CANCELLED:
                return "ALREADY_CANCELLED"
            if self._state in (State.REJECTED, State.TIMED_OUT):
                return "ALREADY_TERMINAL"
            self._state, self._reason = State.CANCELLED, "cancelled_before_commit"
            self._observations.clear()
            return "CANCELLED"

    def snapshot(self) -> dict:
        """Metadata only; no payload bytes or decoded content are retained."""
        with self._lock:
            return dict(request_id=self.request_id, frame_id=self.source.frame_id,
                        generation=self.source.generation, state=self._state.value,
                        selection_evidence_mode=self.policy.selection_evidence_mode,
                        reason=self._reason, deadline_ns=self.deadline_ns,
                        stable_observations=len(self._observations), commit_attempts=self._commit_attempts,
                        native_instance=asdict(self._capture.snapshot.instance) if self._capture else None)
