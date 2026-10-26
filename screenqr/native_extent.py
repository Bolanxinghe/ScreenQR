"""Pure fixed-profile inference of native selection borders from numeric strips.

Calibration is exclusively the observed WeChat 4.1.15.13 / DPI192 / 3200x2000
native border profile. This module imports no capture/UI/native API and reads no file.
It consumes eight structured numeric cross-sections from the bounded observer.

VERIFIED describes only this exact boundary profile. It is NOT a selection
receipt, native raw-payload equality, commit permission or business success.
Requested edge coordinates are deliberately never read. The requested transport
rectangle is consulted only AFTER the actual rectangle has been inferred.
"""
from __future__ import annotations

from .profiles import DEFAULT_PROFILE, current_profile

from collections.abc import Mapping
from copy import deepcopy
import math
import ntpath
import re
import time
from uuid import UUID

PROFILE_ID = DEFAULT_PROFILE.border_id
VERSION = DEFAULT_PROFILE.version
CLIENT_RECT = DEFAULT_PROFILE.screen
EXECUTABLE = DEFAULT_PROFILE.executable
CLASS_NAME = DEFAULT_PROFILE.window_class
GRAY = DEFAULT_PROFILE.boundary.gray
GREEN = DEFAULT_PROFILE.boundary.green
WHITE = DEFAULT_PROFILE.boundary.white
MAX_OBSERVATION_NS = 2_000_000_000
SIDES = ("top", "bottom", "left", "right")


class _Reject(Exception):
    pass


def _need(condition: bool, reason: str) -> None:
    if not condition:
        raise _Reject(reason)


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _mapping(value: object, field: str) -> Mapping:
    _need(isinstance(value, Mapping), field + "_must_be_mapping")
    return value


def _sequence(value: object, field: str) -> tuple:
    _need(isinstance(value, (tuple, list)), field + "_must_be_sequence")
    return tuple(value)


def _positive(value: object, field: str) -> int:
    _need(_integer(value) and value > 0, field + "_must_be_positive_integer")
    return value


def _nonnegative(value: object, field: str) -> int:
    _need(_integer(value) and value >= 0, field + "_must_be_nonnegative_integer")
    return value


def _rect(value: object, field: str) -> tuple[int, int, int, int]:
    rect = _sequence(value, field)
    _need(len(rect) == 4 and all(_integer(v) and -(2**31) <= v < 2**31 for v in rect),
          field + "_requires_four_win32_integer_edges")
    _need(rect[0] < rect[2] and rect[1] < rect[3], field + "_must_be_nonempty")
    return rect


def _inside(inner: tuple, outer: tuple) -> bool:
    return (outer[0] <= inner[0] < inner[2] <= outer[2]
            and outer[1] <= inner[1] < inner[3] <= outer[3])


def _uuid(value: object, field: str) -> str:
    try:
        identifier = UUID(value) if isinstance(value, str) else None
    except (ValueError, TypeError, AttributeError):
        identifier = None
    _need(identifier is not None and identifier.int != 0 and str(identifier) == value,
          field + "_must_be_canonical_uuid")
    return value


def _sha(value: object, field: str) -> str:
    _need(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
          field + "_must_be_sha256")
    return value


def _identity(probe_value: object) -> tuple:
    probe = _mapping(probe_value, "native_probe")
    _need(probe.get("status") == "UNIQUE", "native_identity_not_unique")
    _need(probe.get("errors") in ([], ()), "native_probe_errors_or_unknown")
    candidates = _sequence(probe.get("candidates"), "native_candidates")
    _need(len(candidates) == 1, "native_candidate_count_not_one")
    identity = _mapping(candidates[0], "native_identity")
    hwnd = _positive(identity.get("hwnd"), "native_hwnd")
    pid = _positive(identity.get("pid"), "native_pid")
    created = _positive(identity.get("process_created_filetime_100ns"), "native_process_created")
    path = identity.get("executable_path")
    _need(isinstance(path, str) and ntpath.normcase(path) == ntpath.normcase(current_profile().executable),
          "native_path_outside_profile")
    _need(identity.get("title") == current_profile().title and identity.get("window_class") == current_profile().window_class,
          "native_title_or_class_outside_profile")
    _need(_rect(identity.get("window_rect"), "native_window_rect") == current_profile().screen
          and _rect(identity.get("client_screen_rect"), "native_client_rect") == current_profile().screen,
          "native_rect_outside_profile")
    _need(_integer(identity.get("dpi")) and identity["dpi"] == current_profile().dpi,
          "native_dpi_outside_profile")
    _need(identity.get("foreground_match") is True, "native_not_foreground")
    _need(_integer(probe.get("monitor_count")) and probe["monitor_count"] == 1
          and _integer(probe.get("thread_dpi_awareness")) and probe["thread_dpi_awareness"] == 2
          and _rect(probe.get("primary_screen_rect"), "primary_screen_rect") == current_profile().screen,
          "native_monitor_or_awareness_outside_profile")
    _need(probe.get("foreground_hwnd_at_start") == hwnd
          and probe.get("foreground_hwnd_at_end") == hwnd,
          "native_foreground_changed_or_unknown")
    _need(identity.get("hwnd_source") == "EnumWindows; not a Sky window token",
          "native_hwnd_source_unknown")
    return hwnd, pid, created, ntpath.normcase(path), current_profile().title, current_profile().window_class, current_profile().screen, current_profile().dpi


def _time_interval(value: Mapping, prefix: str, start_field: str, end_field: str) -> tuple[int, int]:
    start = _positive(value.get(start_field), prefix + "_started")
    end = _positive(value.get(end_field), prefix + "_ended")
    _need(start <= end, prefix + "_time_runs_backwards")
    return start, end


def _uniform_rgb(row: Mapping) -> tuple[int, int, int]:
    low = _sequence(row.get("rgb_min"), "profile_rgb_min")
    high = _sequence(row.get("rgb_max"), "profile_rgb_max")
    mean = _sequence(row.get("rgb_mean"), "profile_rgb_mean")
    _need(len(low) == len(high) == len(mean) == 3, "profile_rgb_triplet_required")
    _need(all(_integer(v) and 0 <= v <= 255 for v in low + high),
          "profile_rgb_channel_invalid")
    _need(all(isinstance(v, (int, float)) and not isinstance(v, bool)
              and math.isfinite(v) and 0 <= v <= 255 for v in mean),
          "profile_rgb_mean_invalid")
    _need(low == high and all(mean[index] == low[index] for index in range(3)),
          "profile_not_exactly_uniform_across_eight_pixels")
    _need(_integer(row.get("exact_source_white_pixel_count"))
          and row["exact_source_white_pixel_count"] == (8 if low == (255, 255, 255) else 0),
          "profile_white_count_inconsistent")
    return low


def _edge(section_value: object) -> dict:
    section = _mapping(section_value, "edge_section")
    side = section.get("side")
    _need(side in SIDES, "unknown_edge_side")
    quarter = section.get("quarter_numerator")
    _need(_integer(quarter) and quarter in (1, 3)
          and _integer(section.get("quarter_denominator")) and section["quarter_denominator"] == 4,
          "unknown_edge_quarter")
    box = _rect(section.get("captured_bbox_physical"), "edge_captured_bbox")
    _need(_inside(box, current_profile().screen), "edge_capture_outside_client")
    width, height = box[2] - box[0], box[3] - box[1]
    _need(_integer(section.get("width")) and _integer(section.get("height"))
          and (section["width"], section["height"]) == (width, height),
          "edge_dimensions_inconsistent")
    axis = "y" if side in ("top", "bottom") else "x"
    _need(section.get("normal_axis") == axis, "edge_normal_axis_inconsistent")
    _need((width, height) == ((8, 18) if axis == "y" else (18, 8)),
          "edge_strip_shape_outside_profile")
    digest = _sha(section.get("captured_rgb_sha256"), "edge_rgb_digest")
    rows = _sequence(section.get("normal_profiles"), "normal_profiles")
    _need(len(rows) == 18, "edge_normal_profile_length_not_eighteen")
    origin = box[1] if axis == "y" else box[0]
    colours: list[tuple[int, int, int]] = []
    for index, row_value in enumerate(rows):
        row = _mapping(row_value, "normal_profile_row")
        coordinate = row.get("physical_pixel_coordinate")
        _need(_integer(coordinate) and coordinate == origin + index,
              "edge_profile_coordinates_not_complete_contiguous")
        _need(_integer(row.get("tangential_pixel_count")) and row["tangential_pixel_count"] == 8,
              "edge_tangential_count_not_eight")
        colours.append(_uniform_rgb(row))
    runs: list[dict] = []
    for index, colour in enumerate(colours):
        coordinate = origin + index
        if not runs or runs[-1]["rgb"] != colour:
            runs.append(dict(rgb=colour, first=coordinate, last=coordinate, length=1))
        else:
            runs[-1]["last"] = coordinate
            runs[-1]["length"] += 1
    expected_colours = (current_profile().boundary.gray, current_profile().boundary.green, current_profile().boundary.white) if side in ("top", "left") else (current_profile().boundary.white, current_profile().boundary.green, current_profile().boundary.gray)
    _need(len(runs) == 3 and tuple(run["rgb"] for run in runs) == expected_colours,
          "edge_run_sequence_not_calibrated_exact_three_runs")
    _need(runs[1]["length"] == 6, "edge_green_run_not_six_pixels")
    _need(runs[0]["length"] >= 2 and runs[2]["length"] >= 2,
          "edge_end_run_has_less_than_two_pixels")
    # Real native export calibration establishes these directional crop conventions.
    # Neither requested_edge_coordinate nor any requested rectangle is read here.
    actual_edge = runs[1]["last"] if side in ("top", "left") else runs[1]["first"] + 1
    started, ended = _time_interval(section, "edge_capture", "capture_started_monotonic_ns", "capture_ended_monotonic_ns")
    return dict(side=side, quarter=quarter, actual_edge=actual_edge,
                captured_bbox_physical=box, captured_rgb_sha256=digest,
                normal_axis=axis, runs=runs,
                capture_started_monotonic_ns=started,
                capture_ended_monotonic_ns=ended)


def _derive(observation: object, *, live: bool) -> dict:
    output = dict(
        schema_version=1, status="REJECTED", reason="not_started",
        boundary_profile_id=current_profile().border_id,
        observed_selection_bbox_physical=None, requested_transport_matches=None,
        requested_transport_bbox_physical=None,
        native_instance_key=None, edge_evidence=[],
        source_evidence_id=None, session_id=None, frame_id=None, generation=None,
        stage_id=None, stage_epoch=None, source_capture_sha256=None, topology_sha256=None,
        source_observation_started_monotonic_ns=None,
        source_observation_finished_monotonic_ns=None,
        source_deadline_monotonic_ns=None,
        source_native_status=None, source_rendered_payload_equal=None,
        raw_payload_verified=False, commit_authorization=False,
        selection_receipt_created=False, analysis_mode="live" if live else "retrospective_calibration",
        limitations=[
            "Exact observed border profile only; not raw image equality or business success.",
            "Structured records are not authenticated native provenance or a current window query.",
            "No SelectionReceipt or commit permission is created; source payload failure is preserved.",
            "Only 4.1.15.13, DPI192, 3200x2000, exact three-run colours/geometry are supported.",
        ],
    )
    try:
        source = _mapping(observation, "observation")
        _need(_integer(source.get("schema_version")) and source["schema_version"] == 1,
              "unsupported_observation_schema")
        _need(source.get("status") in ("PAYLOAD_MISMATCH", "PAYLOAD_MATCH_EXTENT_UNVERIFIED"),
              "source_observer_did_not_complete")
        _need(source.get("errors") in ([], ()), "source_observer_errors_or_unknown")
        _need(source.get("read_only") is True and source.get("raw_pixels_persisted") is False
              and source.get("native_input_sent") is False, "source_observer_scope_not_read_only")
        output["source_evidence_id"] = _uuid(source.get("evidence_id"), "source_evidence_id")
        for name in ("frame_id", "stage_id"):
            output[name] = _uuid(source.get(name), name)
        session = source.get("session_id")
        _need(isinstance(session, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", session) is not None,
              "invalid_session_id")
        output["session_id"] = session
        output["generation"] = _nonnegative(source.get("generation"), "generation")
        output["stage_epoch"] = _nonnegative(source.get("stage_epoch"), "stage_epoch")
        for name in ("source_capture_sha256", "topology_sha256"):
            output[name] = _sha(source.get(name), name)
        before = _identity(source.get("native_identity_before"))
        after = _identity(source.get("native_identity_after"))
        _need(before == after, "native_instance_changed_across_capture")
        output["native_instance_key"] = before
        version = _mapping(source.get("version_metadata"), "version_metadata")
        actual_version = _sequence(version.get("fixed_file_version"), "fixed_file_version")
        declared_version = _sequence(version.get("expected_fixed_file_version"), "expected_fixed_file_version")
        _need(all(_integer(v) for v in actual_version + declared_version)
              and actual_version == declared_version == current_profile().version, "version_outside_calibrated_profile")
        started = _positive(source.get("started_monotonic_ns"), "source_started")
        completed = _positive(source.get("capture_completed_monotonic_ns"), "source_capture_completed")
        finished = _positive(source.get("finished_monotonic_ns"), "source_finished")
        deadline = _positive(source.get("deadline_monotonic_ns"), "source_deadline")
        _need(started <= completed <= finished < deadline
              and deadline - started <= MAX_OBSERVATION_NS, "source_capture_timing_invalid_or_expired")
        output.update(source_observation_started_monotonic_ns=started,
                      source_observation_finished_monotonic_ns=finished,
                      source_deadline_monotonic_ns=deadline)
        if live:
            now = time.monotonic_ns()
            _need(finished <= now < deadline, "source_observation_not_current")
        payload = _mapping(source.get("payload_comparison"), "payload_comparison")
        equal = payload.get("exact_rgb_bytes_equal")
        _need(isinstance(equal, bool), "rendered_payload_comparison_missing")
        _need((source["status"] == "PAYLOAD_MATCH_EXTENT_UNVERIFIED") == equal,
              "source_status_and_payload_comparison_disagree")
        output["source_native_status"] = source["status"]
        output["source_rendered_payload_equal"] = equal
        payload_start, previous_end = _time_interval(payload, "payload_capture", "capture_started_monotonic_ns", "capture_ended_monotonic_ns")
        _need(started <= payload_start <= previous_end <= completed, "payload_capture_time_outside_observation")
        sections = _sequence(source.get("edge_observations"), "edge_observations")
        _need(len(sections) == 8, "exactly_eight_edge_sections_required")
        edges: dict[tuple[str, int], dict] = {}
        for section in sections:
            edge = _edge(section)
            key = edge["side"], edge["quarter"]
            _need(key not in edges, "duplicate_edge_quarter")
            _need(previous_end <= edge["capture_started_monotonic_ns"]
                  <= edge["capture_ended_monotonic_ns"] <= completed,
                  "edge_capture_time_outside_ordered_observation")
            previous_end = edge["capture_ended_monotonic_ns"]
            edges[key] = edge
        _need(set(edges) == {(side, quarter) for side in SIDES for quarter in (1, 3)},
              "missing_edge_side_or_quarter")
        actual_edges: dict[str, int] = {}
        for side in SIDES:
            first, second = edges[side, 1], edges[side, 3]
            _need(first["actual_edge"] == second["actual_edge"], "two_quarters_disagree:" + side)
            axis = first["normal_axis"]
            # Independent physical cross-sections must not be duplicated.
            first_box, second_box = first["captured_bbox_physical"], second["captured_bbox_physical"]
            _need(first_box[2 if axis == "y" else 3] <= second_box[0 if axis == "y" else 1],
                  "two_quarters_not_distinct_ordered_cross_sections:" + side)
            actual_edges[side] = first["actual_edge"]
        actual = _rect((actual_edges["left"], actual_edges["top"], actual_edges["right"], actual_edges["bottom"]),
                       "independently_derived_actual_bbox")
        _need(_inside(actual, current_profile().screen), "derived_actual_bbox_outside_client")
        for edge in edges.values():
            box = edge["captured_bbox_physical"]
            _need((actual[0] <= box[0] < box[2] <= actual[2]) if edge["normal_axis"] == "y"
                  else (actual[1] <= box[1] < box[3] <= actual[3]),
                  "cross_section_tangential_span_outside_derived_extent")
        # Only now inspect the requested stage mapping. It cannot determine actual.
        stage = _mapping(source.get("stage_mapping"), "stage_mapping")
        for field in ("frame_id", "generation", "stage_id", "stage_epoch", "source_capture_sha256", "topology_sha256"):
            _need(stage.get(field) == source.get(field), "source_stage_binding_mismatch:" + field)
        requested = _rect(stage.get("transport_requested_bbox_physical"), "requested_transport_bbox")
        white_container = _rect(stage.get("white_padding_bbox_physical"), "white_container_bbox")
        _need(_inside(white_container, current_profile().screen) and _inside(actual, white_container)
              and _inside(requested, white_container), "extent_outside_recorded_white_container")
        _need(all(_inside(edge["captured_bbox_physical"], white_container) for edge in edges.values()),
              "edge_capture_outside_recorded_white_container")
        if live:
            _need(time.monotonic_ns() < deadline, "boundary_analysis_finished_after_source_deadline")
        output.update(
            status="VERIFIED" if live else "OFFLINE_INFERRED",
            reason="exact_calibrated_profile_two_quarters_agree",
            observed_selection_bbox_physical=actual,
            requested_transport_bbox_physical=requested,
            requested_transport_matches=actual == requested,
            edge_evidence=[edges[side, quarter] for side in SIDES for quarter in (1, 3)],
        )
    except _Reject as exc:
        output["status"], output["reason"] = "REJECTED", str(exc)
    except Exception as exc:
        output["status"], output["reason"] = "REJECTED", "unsupported_or_malformed_structure:" + type(exc).__name__
    return output


def derive_native_extent(observation: dict) -> dict:
    """Infer a fresh fixed-profile boundary without querying/control of an app.

    Both source completion and this analysis must precede the source observation
    deadline. The caller still owns current stage/session and window checks.
    """
    return _derive(observation, live=True)


def analyze_calibration_observation(observation: dict) -> dict:
    """Retrospective analysis only; returns OFFLINE_INFERRED, never VERIFIED.

    Source acquisition timing is still validated. Current wall/monotonic age is
    not treated as fresh evidence; this path cannot authorize a live handoff.
    """
    return _derive(observation, live=False)


def extent_evidence_from_observation(observation: Mapping) -> dict:
    """Add independently derived edges without changing the source observation.

    The same original observation ID, payload comparison, identity and sampling
    interval are preserved for extent and display receipt factories. This is an
    interpretation of the observed strips, not a new native capture or raw-image
    success. Failure raises before any evidence suitable for a receipt is made.
    """
    boundary = derive_native_extent(observation)
    if boundary["status"] != "VERIFIED":
        raise ValueError("native selection extent unverified: " + boundary["reason"])
    result = deepcopy(dict(observation))
    original_id = boundary["source_evidence_id"]
    if result.get("source_evidence_id", original_id) != original_id:
        raise ValueError("native extent source observation ID mismatch")
    result.update(
        source_evidence_id=original_id,
        selection_extent_status="VERIFIED",
        observed_selection_bbox_physical=boundary["observed_selection_bbox_physical"],
        extent_verification_method="native-client-observed-boundaries-v1",
        boundary_profile_id=boundary["boundary_profile_id"],
        boundary_analysis=boundary,
    )
    return result
