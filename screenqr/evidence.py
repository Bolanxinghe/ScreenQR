"""Internal read-only grouping of content, rendering and geometry observations.

No native query, capture, file I/O, input, receipt/permit factory or state machine
is used. This adapter groups declared facts from existing producers; it cannot
attest their provenance or replace current-window/stage checks. Its result is
research metadata and is deliberately unusable as strict handoff evidence.

Exact own-stage/source bytes, forward rendering relationships and independently
observed selection geometry are different claims. No rendered relationship,
including identity, establishes the native scanner's original input bytes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import ntpath
import re
from typing import Mapping
from uuid import UUID

from .frozen_frame import FrozenFrame
from .handoff import FrameBinding, NativeInstance, NativeSnapshot, StageReceipt
from .stage import StagePixelVerification


class EvidenceBindingError(ValueError):
    """The supplied observations cannot be grouped as one fresh request."""


def _need(value: bool, reason: str) -> None:
    if not value:
        raise EvidenceBindingError(reason)


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _sha(value: object) -> str:
    _need(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
          "missing or invalid evidence digest")
    return value


def _rect(value: object) -> tuple[int, int, int, int]:
    _need(isinstance(value, (tuple, list)) and len(value) == 4
          and all(_integer(v) or (isinstance(v, int) and not isinstance(v, bool)) for v in value),
          "missing physical evidence rectangle")
    result = tuple(value)
    _need(result[0] < result[2] and result[1] < result[3], "empty evidence rectangle")
    return result


def _instance_key(native: NativeInstance) -> tuple:
    return (native.hwnd, native.pid, native.process_created_filetime_100ns,
            ntpath.normcase(native.executable_path), native.title,
            native.window_class, native.window_rect, native.dpi)


@dataclass(frozen=True, slots=True)
class ObservationContext:
    source: FrameBinding
    session_id: str
    observation_id: str
    stage_id: str
    stage_epoch: int
    native_instance: NativeInstance
    started_ns: int
    finished_ns: int
    deadline_ns: int


@dataclass(frozen=True, slots=True)
class OriginalContentEvidence:
    source_rgb_sha256: str
    own_stage_payload_sha256: str
    own_stage_transport_sha256: str
    own_stage_white_sha256: str
    native_content_status: str = field(default="UNPROVEN", init=False)
    native_export_status: str = field(default="NOT_PROVIDED", init=False)
    raw_payload_verified: bool = field(default=False, init=False)


@dataclass(frozen=True, slots=True)
class RenderingCandidate:
    candidate_id: str
    predicted_rgb_sha256: str
    exact_forward_match: bool
    injective_on_uint8_channels: bool
    diagnostic_only: bool = field(default=True, init=False)


@dataclass(frozen=True, slots=True)
class RenderedRelationshipEvidence:
    source_native_status: str
    rendered_rgb_sha256: str
    exact_rendered_rgb_identity: bool
    candidates: tuple[RenderingCandidate, ...]
    original_native_bytes_proven: bool = field(default=False, init=False)
    scope: str = field(default="forward display comparison; no inverse/raw-input proof", init=False)


@dataclass(frozen=True, slots=True)
class GeometryEvidence:
    status: str
    profile_id: str | None
    observed_selection_bbox_physical: tuple[int, int, int, int] | None
    requested_transport_bbox_physical: tuple[int, int, int, int]
    requested_transport_matches: bool | None


@dataclass(frozen=True, slots=True)
class NativeEvidenceReview:
    context: ObservationContext
    original_content: OriginalContentEvidence
    rendered_relationship: RenderedRelationshipEvidence
    geometry: GeometryEvidence
    raw_payload_verified: bool = field(default=False, init=False)
    commit_authorization: bool = field(default=False, init=False)
    selection_receipt_created: bool = field(default=False, init=False)
    scope: str = field(default="read-only evidence grouping; strict handoff factories remain unchanged", init=False)

    def metadata_only(self) -> dict:
        return asdict(self)


def collect_native_evidence(
    frame: FrozenFrame,
    stage_verification: StagePixelVerification,
    observation: Mapping,
    *,
    extent: Mapping | None = None,
    now_ns: int,
) -> NativeEvidenceReview:
    """Group one fresh observation without producing any handoff permission.

    Pass the original native observer result and its separate live extent result.
    The latter must reference exactly the same observation/instance/timestamps.
    Expired or cross-instance/cross-stage records raise EvidenceBindingError.
    No export or scanner-input raw-content producer exists in this interface;
    that claim remains UNPROVEN even if a display candidate matches exactly.
    """
    _need(isinstance(frame, FrozenFrame) and isinstance(stage_verification, StagePixelVerification),
          "typed frozen source and real own-stage verification are required")
    _need(isinstance(observation, Mapping), "native observation is required")
    stage = StageReceipt.from_stage_verification(frame, stage_verification)
    _need(stage_verification.captured_white_stage_sha256 == stage_verification.expected_white_stage_sha256,
          "own white stage was not exact")
    _need(observation.get("status") in ("PAYLOAD_MISMATCH", "PAYLOAD_MATCH_EXTENT_UNVERIFIED")
          and observation.get("errors") in ([], ())
          and observation.get("read_only") is True and observation.get("native_input_sent") is False,
          "native observation did not complete as read-only")
    mapping = stage_verification.mapping
    for key, expected in (
        ("frame_id", frame.frame_id), ("generation", frame.generation),
        ("source_capture_sha256", frame.capture_sha256), ("topology_sha256", frame.topology_sha256),
        ("stage_id", mapping.stage_id), ("stage_epoch", mapping.stage_epoch),
    ):
        _need(observation.get(key) == expected and type(observation.get(key)) is type(expected),
              "observation source/stage binding changed: " + key)
    session_id, evidence_id = observation.get("session_id"), observation.get("evidence_id")
    _need(isinstance(session_id, str) and 0 < len(session_id) <= 128, "invalid observation session")
    try:
        parsed = UUID(evidence_id) if isinstance(evidence_id, str) else None
    except ValueError:
        parsed = None
    _need(parsed is not None and parsed.int != 0 and str(parsed) == evidence_id, "invalid observation identity")
    started, completed, finished, deadline = (observation.get(name) for name in (
        "started_monotonic_ns", "capture_completed_monotonic_ns", "finished_monotonic_ns", "deadline_monotonic_ns"))
    _need(all(_integer(v) for v in (started, completed, finished, deadline, now_ns))
          and 0 < started <= completed <= finished <= now_ns < deadline
          and stage.checked_ns <= started, "observation expired or causal time order changed")
    version = observation.get("version_metadata")
    _need(isinstance(version, Mapping), "native file version is missing")
    fixed = tuple(version.get("fixed_file_version") or ())
    _need(fixed == tuple(version.get("expected_fixed_file_version") or ()), "native declared version changed")
    before = NativeSnapshot.from_probe(observation.get("native_identity_before"),
        started_ns=started, finished_ns=finished, fixed_file_version=fixed)
    after = NativeSnapshot.from_probe(observation.get("native_identity_after"),
        started_ns=started, finished_ns=finished, fixed_file_version=fixed)
    _need(before.instance == after.instance
          and before.monitor_count == after.monitor_count
          and before.primary_screen_rect == after.primary_screen_rect
          and before.thread_dpi_awareness == after.thread_dpi_awareness,
          "native instance or display metadata changed")
    native = before.instance
    comparison = observation.get("payload_comparison")
    _need(isinstance(comparison, Mapping), "rendered payload comparison missing")
    identity = comparison.get("exact_rgb_bytes_equal")
    rendered_sha = _sha(comparison.get("captured_rgb_sha256"))
    _need(isinstance(identity, bool) and comparison.get("expected_rgb_sha256") == frame.capture_sha256
          and _rect(comparison.get("captured_bbox_physical")) == stage.payload_bbox_physical
          and (comparison.get("width"), comparison.get("height")) == (frame.width, frame.height)
          and identity == (rendered_sha == frame.capture_sha256)
          and (observation["status"] == "PAYLOAD_MATCH_EXTENT_UNVERIFIED") == identity,
          "rendered comparison does not belong to this source")
    candidates = []
    diagnostics = observation.get("content_diagnostics")
    if diagnostics is not None:
        _need(isinstance(diagnostics, Mapping) and diagnostics.get("diagnostic_only") is True
              and diagnostics.get("raw_payload_pass_created") is False
              and diagnostics.get("commit_authorization") is False
              and diagnostics.get("source_rgb_sha256") == frame.capture_sha256
              and diagnostics.get("captured_rgb_sha256") == rendered_sha,
              "rendering diagnostics cannot replace raw evidence")
        raw_candidates = diagnostics.get("preregistered_exact_candidate_comparisons")
        known = {"identity": True, "positive_channel_minus_one": False,
                 "round_scale_254_over_255": False, "only_255_to_254": False}
        _need(isinstance(raw_candidates, (tuple, list)) and len(raw_candidates) == 4,
              "four preregistered display comparisons required")
        seen = set()
        for item in raw_candidates:
            _need(isinstance(item, Mapping), "invalid rendering candidate")
            name, exact = item.get("candidate_id"), item.get("exact_byte_match")
            _need(name in known and name not in seen and isinstance(exact, bool)
                  and item.get("diagnostic_only") is True, "unknown or duplicate rendering candidate")
            predicted_sha = _sha(item.get("expected_rgb_sha256"))
            _need(exact == (predicted_sha == rendered_sha)
                  and (name != "identity" or predicted_sha == frame.capture_sha256),
                  "candidate comparison hashes disagree")
            seen.add(name)
            candidates.append(RenderingCandidate(name, predicted_sha, exact, known[name]))
    geometry = GeometryEvidence("UNVERIFIED", None, None, stage.transport_bbox_physical, None)
    if extent is not None:
        _need(isinstance(extent, Mapping) and extent.get("status") == "VERIFIED"
              and extent.get("analysis_mode") == "live"
              and extent.get("raw_payload_verified") is False
              and extent.get("commit_authorization") is False
              and extent.get("selection_receipt_created") is False,
              "only separate live geometry inference is accepted")
        for key in ("session_id", "frame_id", "generation", "stage_id", "stage_epoch",
                    "source_capture_sha256", "topology_sha256"):
            _need(extent.get(key) == observation.get(key), "geometry binding changed: " + key)
        _need(extent.get("source_evidence_id") == evidence_id
              and extent.get("source_observation_started_monotonic_ns") == started
              and extent.get("source_observation_finished_monotonic_ns") == finished
              and extent.get("source_deadline_monotonic_ns") == deadline,
              "geometry references another or expired observation")
        native_key = list(extent.get("native_instance_key") or ())
        _need(len(native_key) == 8, "geometry native instance key missing")
        native_key[6] = _rect(native_key[6])
        _need(tuple(native_key) == _instance_key(native), "geometry belongs to another native instance")
        actual = _rect(extent.get("observed_selection_bbox_physical"))
        requested = _rect(extent.get("requested_transport_bbox_physical"))
        matches = extent.get("requested_transport_matches")
        _need(requested == stage.transport_bbox_physical and isinstance(matches, bool)
              and matches == (actual == requested), "geometry requested/observed comparison is inconsistent")
        profile = extent.get("boundary_profile_id")
        _need(isinstance(profile, str) and bool(profile), "geometry profile is missing")
        geometry = GeometryEvidence("INDEPENDENTLY_OBSERVED", profile, actual, requested, matches)
    context = ObservationContext(FrameBinding.from_frame(frame), session_id, evidence_id,
        stage.stage_id, stage.stage_epoch, native, started, finished, deadline)
    original = OriginalContentEvidence(frame.capture_sha256, stage_verification.rendered_payload_sha256,
        stage_verification.rendered_transport_sha256, stage_verification.captured_white_stage_sha256)
    rendering = RenderedRelationshipEvidence(observation["status"], rendered_sha, identity, tuple(candidates))
    return NativeEvidenceReview(context, original, rendering, geometry)
