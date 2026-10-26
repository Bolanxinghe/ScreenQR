"""Original structural QR-icon observer for the development native profile.

No private template, copied pixel mask, capture, Win32 query, input or file I/O.
The caller supplies two actual native-client bands and typed identity/observed
extent receipts. The current profile declares an asymmetric 5:3:1 arrangement
from actual component metadata: one large frame with a separate center, three
small hollow frames and four separate dots. No screenshot bitmap is embedded.
The earlier symmetric hypothesis remains distinct; all candidates share one
uniqueness check. Synthetic fixtures do not establish real business acceptance.
A candidate is read-only; HandoffSession still requires three fresh observations
and a separate final commit check.
Only WeChat 4.1.15.13 / DPI192 / one 3200x2000 primary monitor is supported.
In-process receipt markers prevent accidental bare-metadata admission; they are
not authentication against malicious Python callers or pixel provenance proof.
"""
from __future__ import annotations

from .profiles import DEFAULT_PROFILE, current_profile

from collections import deque
from dataclasses import asdict, dataclass, field
import hashlib
import ntpath
import re
import time
from typing import Callable
from uuid import UUID

from PIL import Image, ImageChops

from . import handoff as _handoff
from .handoff import NativeSnapshot, SelectionExtentReceipt

Rect = tuple[int, int, int, int]
PROFILE = DEFAULT_PROFILE.toolbar_id
SCREEN = DEFAULT_PROFILE.screen
VERSION = DEFAULT_PROFILE.version
BAND_HEIGHT = DEFAULT_PROFILE.toolbar.band_height
# Fixed-profile bounds; structural geometry does not establish true/false rates.
TOOLBAR_WIDTH = DEFAULT_PROFILE.toolbar.width
TOOLBAR_HEIGHT = DEFAULT_PROFILE.toolbar.height
TOOLBAR_GAP_MAX = DEFAULT_PROFILE.toolbar.gap_max
FINDER_SIDE = DEFAULT_PROFILE.toolbar.finder_side
ICON_SIDE = DEFAULT_PROFILE.toolbar.icon_side
QUIET_PIXELS = DEFAULT_PROFILE.toolbar.quiet_pixels


@dataclass(frozen=True, slots=True)
class NativeToolbarBand:
    bbox_physical: Rect
    rgb8: bytes = field(repr=False)
    capture_started_ns: int
    capture_finished_ns: int


@dataclass(frozen=True, slots=True)
class ToolbarBinding:
    request_id: str
    generation: int
    extent: SelectionExtentReceipt
    identity_before: NativeSnapshot
    identity_after: NativeSnapshot
    max_evidence_age_ns: int


@dataclass(frozen=True, slots=True)
class ToolbarDetection:
    status: str
    reason: str
    session_id: str
    generation: int
    selection_bbox_physical: Rect | None
    identity_before: dict | None
    identity_after: dict | None
    candidate_bbox_physical: Rect | None
    started_ns: int
    capture_completed_ns: int
    finished_ns: int
    deadline_monotonic_ns: int
    version_metadata: dict
    errors: tuple[str, ...]
    toolbar_bbox_physical: Rect | None
    candidate_count: int
    captured_bands: tuple[dict, ...]
    read_only: bool = field(default=True, init=False)
    raw_pixels_persisted: bool = field(default=False, init=False)
    detector_profile: str = field(default_factory=lambda: current_profile().toolbar_id, init=False)
    evidence_scope: str = field(default="original structural visual candidate only; not input permission, original-content proof or business success", init=False)

    def to_dict(self) -> dict:
        return asdict(self)


class _Reject(ValueError):
    pass


def _need(condition: bool, reason: str) -> None:
    if not condition:
        raise _Reject(reason)


def _int(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _rect(value: object) -> Rect:
    _need(type(value) is tuple and len(value) == 4 and all(_int(x) for x in value), "integer_physical_rectangle_required")
    x0, y0, x1, y1 = value
    _need(0 <= x0 < x1 <= current_profile().width and 0 <= y0 < y1 <= current_profile().height, "rectangle_outside_fixed_client")
    return value


def _probe(snapshot: NativeSnapshot) -> dict:
    item = asdict(snapshot.instance)
    item["foreground_match"] = True
    return dict(status="UNIQUE", errors=[], candidates=[item],
                foreground_hwnd_at_start=snapshot.foreground_before,
                foreground_hwnd_at_end=snapshot.foreground_after,
                monitor_count=snapshot.monitor_count,
                primary_screen_rect=snapshot.primary_screen_rect,
                thread_dpi_awareness=snapshot.thread_dpi_awareness)


def _validate(bands, binding, started, completed, deadline, now) -> Rect:
    _need(isinstance(binding, ToolbarBinding), "typed_binding_required")
    try:
        request = UUID(binding.request_id)
        _need(request.int != 0 and str(request) == binding.request_id, "canonical_request_uuid_required")
    except (ValueError, TypeError, AttributeError):
        raise _Reject("canonical_request_uuid_required") from None
    _need(_int(binding.generation) and _int(binding.max_evidence_age_ns, 1), "generation_or_age_invalid")
    _need(all(_int(t, 1) for t in (started, completed, deadline, now)) and started <= completed <= now < deadline,
          "observation_time_invalid_or_expired")
    extent = binding.extent
    _need(isinstance(extent, SelectionExtentReceipt)
          and extent._observed_boundary_proof is _handoff._EXTENT_PROOF
          and extent.method == "native-client-observed-boundaries-v1"
          and extent.request_id == binding.request_id, "independent_instance_bound_extent_required")
    selection = _rect(extent.actual_selection_bbox_physical)
    _need(not any(v % 2 for v in selection) and selection[1] >= current_profile().toolbar.band_height
          and selection[3] + current_profile().toolbar.band_height <= current_profile().screen[3], "odd_or_clipped_selection_unadapted")
    _need(_int(extent.measured_ns, 1) and extent.measured_ns <= started
          and now - extent.measured_ns <= binding.max_evidence_age_ns, "extent_not_fresh")
    before, after = binding.identity_before, binding.identity_after
    for snapshot in (before, after):
        _need(isinstance(snapshot, NativeSnapshot) and snapshot._metadata_probe_proof is _handoff._PROBE_PROOF,
              "typed_native_probe_receipts_required")
        item = snapshot.instance
        _need(item == extent.native_instance and item.fixed_file_version == current_profile().version
              and item.window_rect == current_profile().screen and item.client_screen_rect == current_profile().screen and item.dpi == current_profile().dpi
              and item.window_class == current_profile().window_class and item.title == current_profile().title
              and ntpath.normcase(ntpath.normpath(item.executable_path))
                  == ntpath.normcase(current_profile().executable)
              and snapshot.foreground_before == item.hwnd == snapshot.foreground_after
              and snapshot.monitor_count == 1 and snapshot.primary_screen_rect == current_profile().screen
              and snapshot.thread_dpi_awareness == 2, "native_instance_or_fixed_profile_mismatch")
        _need(_int(snapshot.started_ns, 1) and _int(snapshot.finished_ns, 1)
              and snapshot.started_ns <= snapshot.finished_ns
              and now - snapshot.started_ns <= binding.max_evidence_age_ns,
              "native_probe_time_invalid_or_old")
    _need(type(bands) is tuple and len(bands) == 2, "exactly_two_native_bands_required")
    expected = ((0, selection[1] - current_profile().toolbar.band_height, current_profile().width, selection[1]),
                (0, selection[3], current_profile().width, selection[3] + current_profile().toolbar.band_height))
    previous = before.finished_ns
    _need(started <= before.started_ns, "pre_probe_outside_observation")
    for band, bbox in zip(bands, expected):
        _need(isinstance(band, NativeToolbarBand) and _rect(band.bbox_physical) == bbox,
              "bands_not_derived_from_actual_extent")
        _need(type(band.rgb8) is bytes and len(band.rgb8) == current_profile().width * current_profile().toolbar.band_height * 3,
              "bounded_immutable_native_rgb_required")
        _need(_int(band.capture_started_ns, 1) and _int(band.capture_finished_ns, 1)
              and previous <= band.capture_started_ns <= band.capture_finished_ns <= completed,
              "band_capture_sequence_invalid")
        previous = band.capture_finished_ns
    _need(completed <= after.started_ns <= after.finished_ns <= now,
          "post_probe_does_not_bracket_capture")
    return selection


def _masks(rgb8: bytes, width: int, height: int) -> tuple[bytes, bytes]:
    images = []
    def hold(value):
        images.append(value)
        return value
    try:
        image = hold(Image.frombytes("RGB", (width, height), rgb8))
        r, g, b = image.split()
        images.extend((r, g, b))
        maximum = hold(ImageChops.lighter(hold(ImageChops.lighter(r, g)), b))
        minimum = hold(ImageChops.darker(hold(ImageChops.darker(r, g)), b))
        chroma = hold(ImageChops.subtract(maximum, minimum))
        neutral = hold(chroma.point([255 if value <= current_profile().toolbar.neutral_max else 0 for value in range(256)]))
        dark = hold(maximum.point([255 if value <= current_profile().toolbar.dark_max else 0 for value in range(256)]))
        light = hold(minimum.point([255 if value >= current_profile().toolbar.light_min else 0 for value in range(256)]))
        return (hold(ImageChops.darker(dark, neutral)).tobytes(),
                hold(ImageChops.darker(light, neutral)).tobytes())
    finally:
        for image in images:
            image.close()


def _components(mask: bytes, width: int, height: int, limit: int = 2048):
    """Four-neighbour components using bounded horizontal runs.

    Scan runs in C through the byte regex and merge only vertically overlapping
    runs. This preserves the same boxes/ink counts and seed order without a
    Python queue operation for every bright toolbar/background pixel.
    """
    _need(width > 0 and height > 0 and len(mask) == width * height,
          "component_shape_invalid")
    parents, boxes = [], []
    previous = []

    def find(label):
        while parents[label] != label:
            parents[label] = parents[parents[label]]
            label = parents[label]
        return label

    def merge(a, b):
        a, b = find(a), find(b)
        if a == b:
            return
        a, b = min(a, b), max(a, b)  # Retain the first row-major seed.
        parents[b] = a
        left, right = boxes[a], boxes[b]
        left[0], left[1] = min(left[0], right[0]), min(left[1], right[1])
        left[2], left[3] = max(left[2], right[2]), max(left[3], right[3])
        left[4] += right[4]

    for y in range(height):
        current, cursor = [], 0
        row = mask[y * width:(y + 1) * width]
        for run in re.finditer(rb"[^\x00]+", row):
            _need(len(parents) < 16384, "component_work_budget_exceeded")
            x0, x1 = run.span()
            label = len(parents)
            parents.append(label)
            boxes.append([x0, y, x1, y + 1, x1 - x0])
            while cursor < len(previous) and previous[cursor][1] <= x0:
                cursor += 1
            adjacent = cursor
            while adjacent < len(previous) and previous[adjacent][0] < x1:
                merge(label, previous[adjacent][2])
                adjacent += 1
            current.append((x0, x1, label))
        previous = current
    found = []
    for label, box in enumerate(boxes):
        if parents[label] == label:
            _need(len(found) < limit, "component_work_budget_exceeded")
            found.append(tuple(box))
    return found


def _count(mask, width, box):
    x0, y0, x1, y1 = box
    return sum(sum(bool(v) for v in mask[y * width + x0:y * width + x1]) for y in range(y0, y1))


def _fraction(mask, width, box):
    return _count(mask, width, box) / ((box[2] - box[0]) * (box[3] - box[1]))


def _ring(mask, width, component):
    x0, y0, x1, y1, count = component
    w, h = x1 - x0, y1 - y0
    if not (current_profile().toolbar.finder_side[0] <= w <= current_profile().toolbar.finder_side[1] and current_profile().toolbar.finder_side[0] <= h <= current_profile().toolbar.finder_side[1]
            and abs(w - h) <= 2 and .25 <= count / (w * h) <= .78):
        return False
    thickness = max(1, min(w, h) // 5)
    sides = ((x0, y0, x1, y0 + thickness), (x0, y1 - thickness, x1, y1),
             (x0, y0, x0 + thickness, y1), (x1 - thickness, y0, x1, y1))
    center = (x0 + w // 3, y0 + h // 3, x1 - w // 3, y1 - h // 3)
    return all(_fraction(mask, width, edge) >= .60 for edge in sides) and _fraction(mask, width, center) <= .10


def _symmetric_icon_candidates(dark, width, height, components):
    """Earlier self-drawn three-equal-ring hypothesis, kept separate."""
    rings = [c for c in components if _ring(dark, width, c)]
    _need(len(rings) <= 32, "finder_work_budget_exceeded")
    candidates = set()
    for a in rings:
        for b in rings:
            if b[0] <= a[2] + 1 or abs(a[1] - b[1]) > 2:
                continue
            for c in rings:
                if c[1] <= a[3] + 1 or abs(a[0] - c[0]) > 2:
                    continue
                dimensions = [v for ring in (a, b, c) for v in (ring[2] - ring[0], ring[3] - ring[1])]
                dx, dy = b[0] - a[0], c[1] - a[1]
                box = (a[0], a[1], b[2], c[3])
                w, h = box[2] - box[0], box[3] - box[1]
                if (max(dimensions) - min(dimensions) > 2 or abs(dx - dy) > 3
                        or not current_profile().toolbar.icon_side[0] <= w <= current_profile().toolbar.icon_side[1] or not current_profile().toolbar.icon_side[0] <= h <= current_profile().toolbar.icon_side[1]
                        or abs(w - h) > 3 or not 1.4 <= dx / dimensions[0] <= 3.2
                        or abs((box[1] + box[3]) / 2 - height / 2) > 12):
                    continue
                br = (b[0], c[1], b[2], c[3])
                if any(abs(r[0] - br[0]) <= 2 and abs(r[1] - br[1]) <= 2 for r in rings):
                    continue  # Four empty rings are a different icon.
                if not .08 <= _fraction(dark, width, br) <= .65:
                    continue
                bw, bh = br[2] - br[0], br[3] - br[1]
                local = b"".join(dark[y * width + br[0]:y * width + br[2]] for y in range(br[1], br[3]))
                pieces = _components(local, bw, bh, limit=32)
                if not 2 <= len(pieces) <= 12:
                    continue
                cells = {(x, y) for y in range(3) for x in range(3)
                         if _fraction(local, bw, (x * bw // 3, y * bh // 3, (x + 1) * bw // 3, (y + 1) * bh // 3)) >= .20}
                if len(cells) < 3 or len({x for x, y in cells}) < 2 or len({y for x, y in cells}) < 2:
                    continue
                q = current_profile().toolbar.quiet_pixels
                quiet = (box[0] - q, box[1] - q, box[2] + q, box[3] + q)
                if quiet[0] < 0 or quiet[1] < 0 or quiet[2] > width or quiet[3] > height:
                    continue
                if _count(dark, width, quiet) != _count(dark, width, box):
                    continue
                candidates.add(box)
    return sorted(candidates)


def _enclosed_light_region(mask, width, box, seed):
    """The designated hole cannot reach a component's bounding edge."""
    x0, y0, x1, y1 = box
    if not (x0 < seed[0] < x1-1 and y0 < seed[1] < y1-1) or mask[seed[1]*width+seed[0]]:
        return False
    seen, pending = {seed}, deque((seed,))
    while pending:
        x, y = pending.popleft()
        if x in (x0, x1-1) or y in (y0, y1-1):
            return False
        for point in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
            if point not in seen and not mask[point[1]*width+point[0]]:
                seen.add(point)
                pending.append(point)
    return True


def _asymmetric_icon_candidates(dark, width, height, components):
    """Relative frame/dot topology; neither screen coordinates nor a bitmap."""
    by_box = {component[:4]: component for component in components}
    candidates = set()
    for large in components:
        x, y, right, bottom, ink = large
        side = right-x
        unit = side//5
        if (not 3 <= unit <= 5 or side != 5*unit or bottom-y != side
                or not current_profile().toolbar.icon_side[0] <= 9*unit <= current_profile().toolbar.icon_side[1]
                or not .25 <= ink/(side*side) <= .78):
            continue
        box = (x,y,x+9*unit,y+9*unit)
        if (abs((box[1]+box[3])/2-height/2) > 12 or box[2] > width or box[3] > height):
            continue
        # A 5-unit frame, three 3-unit empty frames, and five 1-unit dots.
        def at(dx, dy, span):
            return by_box.get((x+dx*unit,y+dy*unit,x+(dx+span)*unit,y+(dy+span)*unit))
        small = [at(6,0,3),at(0,6,3),at(6,6,3)]
        dots = [at(2,2,1),at(6,4,1),at(8,4,1),at(4,6,1),at(4,8,1)]
        if any(component is None for component in small+dots):
            continue
        if any(dot[4] != unit*unit for dot in dots):
            continue
        if any(not _ring(dark,width,ring) or not _enclosed_light_region(dark,width,ring[:4],
                ((ring[0]+ring[2])//2,(ring[1]+ring[3])//2)) for ring in small):
            continue
        thickness = max(1,side//5)
        sides = ((x,y,right,y+thickness),(x,bottom-thickness,right,bottom),
                 (x,y,x+thickness,bottom),(right-thickness,y,right,bottom))
        center = (x+side//3,y+side//3,right-side//3,bottom-side//3)
        if (any(_fraction(dark,width,edge) < .60 for edge in sides)
                or _count(dark,width,center) != unit*unit
                or not _enclosed_light_region(dark,width,large[:4],center[:2])):
            continue
        # No unrelated ink within the glyph or its quiet surroundings.
        if _count(dark,width,box) != sum(component[4] for component in [large]+small+dots):
            continue
        q = current_profile().toolbar.quiet_pixels
        quiet = (box[0]-q,box[1]-q,box[2]+q,box[3]+q)
        if quiet[0] < 0 or quiet[1] < 0 or quiet[2] > width or quiet[3] > height:
            continue
        if _count(dark,width,quiet) != _count(dark,width,box):
            continue
        candidates.add(box)
    return sorted(candidates)


def _icon_candidates(dark, width, height):
    # One flood fill for both declared structures; uniqueness is decided later
    # over their union, rather than accepting the first successful hypothesis.
    components = _components(dark,width,height)
    return sorted(set(_symmetric_icon_candidates(dark,width,height,components))
                  | set(_asymmetric_icon_candidates(dark,width,height,components)))


def detect_toolbar_icon(bands: tuple[NativeToolbarBand, ...], *, binding: ToolbarBinding,
                        started_ns: int, capture_completed_ns: int, deadline_ns: int,
                        clock: Callable[[], int] = time.monotonic_ns) -> ToolbarDetection:
    """Inspect actual bounded bands; this function never captures or clicks.

    before/after snapshots bracket the actual native band acquisition. The final
    clock read includes structural computation; caller still discards cancelled
    requests and performs a new final visual/native check before any submission.
    No persistent pixels or temporal accumulator live in this pure observer.
    """
    status, reason = "REJECTED", "observation_invalid"
    selection = toolbar = candidate = None
    count = 0
    summaries = []
    errors = ()
    before = after = None
    try:
        now = clock()
        selection = _validate(bands, binding, started_ns, capture_completed_ns, deadline_ns, now)
        before, after = _probe(binding.identity_before), _probe(binding.identity_after)
        toolbars = []
        for index, band in enumerate(bands):
            _need(clock() < deadline_ns, "caller_deadline_expired")
            dark, light = _masks(band.rgb8, current_profile().width, current_profile().toolbar.band_height)
            summaries.append(dict(bbox_physical=band.bbox_physical,
                                  capture_started_ns=band.capture_started_ns,
                                  capture_finished_ns=band.capture_finished_ns,
                                  rgb_sha256=hashlib.sha256(band.rgb8).hexdigest()))
            for x0, y0, x1, y1, light_count in _components(light, current_profile().width, current_profile().toolbar.band_height):
                w, h = x1 - x0, y1 - y0
                if not (0 < x0 < x1 < current_profile().width and 0 < y0 < y1 < current_profile().toolbar.band_height
                        and current_profile().toolbar.width[0] <= w <= current_profile().toolbar.width[1] and current_profile().toolbar.height[0] <= h <= current_profile().toolbar.height[1]
                        and light_count / (w * h) >= .82):
                    continue
                box = (x0, band.bbox_physical[1] + y0, x1, band.bbox_physical[1] + y1)
                gap = selection[1] - box[3] if index == 0 else box[1] - selection[3]
                if not (0 <= gap <= current_profile().toolbar.gap_max and min(box[2], selection[2]) > max(box[0], selection[0])):
                    continue
                crop = b"".join(dark[y * current_profile().width + x0:y * current_profile().width + x1] for y in range(y0, y1))
                toolbars.append((box, _icon_candidates(crop, w, h)))
            del dark, light
        if not toolbars:
            status, reason = "NOT_READY", "no_unique_toolbar_geometry"
        elif len(toolbars) != 1:
            status, reason = "AMBIGUOUS", "multiple_toolbar_regions"
        else:
            toolbar, icons = toolbars[0]
            count = len(icons)
            if not icons:
                status, reason = "NOT_READY", "three_ring_qr_structure_not_found"
            elif len(icons) != 1:
                status, reason = "AMBIGUOUS", "multiple_structural_qr_candidates"
            else:
                local = icons[0]
                candidate = (toolbar[0] + local[0], toolbar[1] + local[1],
                             toolbar[0] + local[2], toolbar[1] + local[3])
                status, reason = "READ_ONLY_CANDIDATE", "unique_original_structure_only_no_input_permission"
        finished = clock()
        _need(_int(finished, 1) and finished >= now, "monotonic_clock_invalid_or_regressed")
        if finished >= deadline_ns or finished - binding.extent.measured_ns > binding.max_evidence_age_ns:
            status, reason, candidate = "STALE", "deadline_or_extent_age_expired", None
    except _Reject as exc:
        reason = str(exc)
        status = "STALE" if "expired" in reason or "not_fresh" in reason else "REJECTED"
        candidate, errors = None, (reason,)
        finished = clock()
    except Exception as exc:
        status, reason, candidate = "REJECTED", "detector_error:" + type(exc).__name__, None
        errors, finished = (reason,), clock()
    return ToolbarDetection(status, reason, getattr(binding, "request_id", ""), getattr(binding, "generation", -1),
                            selection, before, after, candidate, started_ns, capture_completed_ns, finished,
                            deadline_ns, dict(fixed_file_version=list(current_profile().version), source="typed bracketed native snapshot; supplied by live port, not queried here"),
                            errors, toolbar, count, tuple(summaries))
