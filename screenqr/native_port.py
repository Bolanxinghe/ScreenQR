"""Real, bounded ScreenQR adapter for the fixed Windows WeChat profile.

Source callbacks read immutable/cache metadata only, never Tk or a UI-thread RPC
under the controller lock. The UI must cancel the controller BEFORE mutating its
frame/epoch or widgets. This module neither reads chats nor uses private APIs,
process memory, screenshot templates, clipboard transfer or saved pixel files.

Only a user-started product request may construct this adapter. A new adapter is
required for a new request; input outcomes are never retried. Unsupported native
profiles and changed focus stop rather than activating another application.
"""
from __future__ import annotations

from .profiles import DEFAULT_PROFILE, current_profile

from copy import deepcopy
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import ntpath
import re
import time
from typing import Callable
from uuid import UUID, uuid4

from .frozen_frame import FrozenFrame
from .handoff import (AbsenceReceipt, CommitPermit, FrameBinding, NativeInstance, NativeSnapshot,
                      ObservationReceipt, SelectionExtentReceipt, StageReceipt)
from .native_capture import (_Gdi, _ObserverError, _capture_rect, _identity_key, _plan, _sections,
                             _require_eligible_probe, observe_native_selection_pixels)
from .native_extent import extent_evidence_from_observation
from .native_identity import NativeWindowIdentity, WindowProbeResult, probe_screenshot_window
from .native_input import InputOutcomeUnknown, WeChatInput
from .native_result import NativeResultBaseline, NativeResultObservation, NativeResultObserver
from .stage import (FrozenStageMapping, StagePixelVerification, StageValidationError, _StageApi,
                    stage_mapping_is_current, verify_frozen_stage)


_STAGE_ERROR_CODES = {
    "Displayed payload pixels differ from the immutable source": "source_pixels_differ",
    "Explicit white stage padding is not entirely white": "white_padding_not_white",
    "Stage mapping no longer belongs to this immutable source frame": "stage_metadata_changed",
    "Own stage changed before pixel verification": "stage_metadata_changed",
    "Own stage changed across its pixel capture": "stage_metadata_changed",
    "Own stage changed during metadata queries": "stage_metadata_changed",
    "Own-stage capture clipped or changed display topology": "stage_metadata_changed",
}

_NATIVE_PROBE_ERROR_CODES = frozenset({
    "typed_native_probe_required", "native_identity_not_unique_or_complete",
    "native_identity_outside_fixed_profile", "verified_native_window_not_stably_foreground",
})

# These failures can describe an incomplete native paint. They are never
# accepted as extent evidence: only a subsequent fully verified capture can pass.
_WAITABLE_BOUNDARY_REASONS = frozenset({
    "profile_not_exactly_uniform_across_eight_pixels",
    "edge_run_sequence_not_calibrated_exact_three_runs",
    "edge_green_run_not_six_pixels",
    "edge_end_run_has_less_than_two_pixels",
    *("two_quarters_disagree:" + side for side in ("top", "bottom", "left", "right")),
})

_TOOLBAR_DIAGNOSTIC_REASONS = frozenset({
    "no_unique_toolbar_geometry", "multiple_toolbar_regions",
    "three_ring_qr_structure_not_found", "multiple_structural_qr_candidates",
    "unique_original_structure_only_no_input_permission",
    "deadline_or_extent_age_expired", "extent_not_fresh",
    "observation_time_invalid_or_expired", "caller_deadline_expired",
    "native_instance_or_fixed_profile_mismatch", "native_probe_time_invalid_or_old",
    "independent_instance_bound_extent_required", "monotonic_clock_invalid_or_regressed",
})


@dataclass(frozen=True, slots=True)
class PortTiming:
    operation: str
    started_ns: int
    finished_ns: int


class WindowsWeChatPort:
    """No native operation occurs on import/construction except API binding."""
    def __init__(self, frame: FrozenFrame, mapping: FrozenStageMapping,
                 source_is_current: Callable[[], bool], *, max_evidence_age_ns: int,
                 clock: Callable[[], int] = time.monotonic_ns,
                 result_observer: NativeResultObserver | None = None,
                 result_deadline_ns: int | None = None) -> None:
        if (not isinstance(frame, FrozenFrame) or not isinstance(mapping, FrozenStageMapping)
                or mapping.frame_id != frame.frame_id or mapping.generation != frame.generation
                or mapping.source_capture_sha256 != frame.capture_sha256
                or mapping.topology_sha256 != frame.topology_sha256
                or (mapping.payload_width, mapping.payload_height) != (frame.width, frame.height)
                or not callable(source_is_current)
                or type(max_evidence_age_ns) is not int or max_evidence_age_ns <= 0):
            raise ValueError("current frozen frame, stage and explicit evidence age required")
        if ((result_observer is None and result_deadline_ns is not None)
                or (result_observer is not None and not isinstance(result_observer, NativeResultObserver))
                or (result_deadline_ns is not None
                    and (type(result_deadline_ns) is not int or result_deadline_ns <= 0))):
            raise ValueError("result observer and optional valid absolute result deadline required")
        self.frame, self.mapping = frame, mapping
        self._source_is_current, self._clock = source_is_current, clock
        self._input = WeChatInput(clock=clock)
        self._api = _Gdi()
        self._request: str | None = None
        self._bound: NativeInstance | None = None
        self._selection_extent: SelectionExtentReceipt | None = None
        # Diagnostic metadata only; no pixels, decoded payload or disk writes.
        # Callers get a detached copy and cannot alter the receipt producer.
        self._last_selection_observation: dict | None = None
        self._last_selection_error_code: str | None = None
        self._last_selection_boundary_reason: str | None = None
        self._selection_poll_count = 0
        self._last_toolbar_diagnostic: dict | None = None
        self._last_stage_error_code: str | None = None
        self._last_native_probe: dict | None = None
        self._latest_native_probe_result: WindowProbeResult | None = None
        self._last_native_error_code: str | None = None
        self._native_startup_transitions: dict[str, int] = {}
        self._native_creation_recheck_used = False
        self._cleanup_attempted = False
        self._absence_rechecked = False
        self._last_cleanup_probe_code: str | None = None
        self._last_cleanup_error_code: str | None = None
        self._last_cleanup_probe: dict | None = None
        self._cleanup_probe_count = 0
        # One dedicated observer per port. Its independent deadline never
        # extends the controller/input deadline. Retiring this adapter leaves
        # only these immutable correlation receipts available for read-only use.
        self._result_observer = result_observer
        self._result_deadline_ns = result_deadline_ns
        self._result_baseline: NativeResultBaseline | None = None
        self._commit_permit: CommitPermit | None = None
        self._commit_called = False
        self._commit_returned_normally = False
        self._commit_input_outcome_unknown = False
        self._age = max_evidence_age_ns
        self.timings: list[PortTiming] = []

    @property
    def last_selection_observation(self) -> dict | None:
        return deepcopy(self._last_selection_observation)

    @property
    def last_selection_error_code(self) -> str | None:
        """A fixed local code, never arbitrary exception/private-content text."""
        return self._last_selection_error_code

    @property
    def last_input_error_code(self) -> str:
        """The input layer's fixed allowlisted reason, with no native query."""
        return self._input.last_report.error_code

    @property
    def last_selection_boundary_reason(self) -> str | None:
        """Whitelisted last boundary diagnosis; no arbitrary exception text."""
        return self._last_selection_boundary_reason

    @property
    def selection_poll_count(self) -> int:
        """Read-only sampling attempts in the latest selection_evidence call."""
        return self._selection_poll_count

    @property
    def last_toolbar_diagnostic(self) -> dict | None:
        """Detached numeric detection summary, never pixels or input permission."""
        return deepcopy(self._last_toolbar_diagnostic)

    @property
    def last_toolbar_reason_code(self) -> str | None:
        return self._last_toolbar_diagnostic["reason"] if self._last_toolbar_diagnostic is not None else None

    @property
    def last_cleanup_probe_code(self) -> str | None:
        return self._last_cleanup_probe_code

    @property
    def last_cleanup_error_code(self) -> str | None:
        return self._last_cleanup_error_code

    @property
    def cleanup_probe_count(self) -> int:
        return self._cleanup_probe_count

    @property
    def last_cleanup_probe(self) -> dict | None:
        return deepcopy(self._last_cleanup_probe)

    @property
    def last_stage_error_code(self) -> str | None:
        """Whitelisted own-stage failure metadata; never exception/path text."""
        return self._last_stage_error_code

    @property
    def last_native_probe(self) -> dict | None:
        """Actual enumerated metadata only; no pixel or arbitrary-window text."""
        return deepcopy(self._last_native_probe)

    @property
    def last_native_error_code(self) -> str | None:
        return self._last_native_error_code

    @property
    def native_startup_transitions(self) -> dict[str, int]:
        """Counts of explicitly admitted read-only startup waits, not retries."""
        return dict(self._native_startup_transitions)

    @property
    def result_baseline(self) -> NativeResultBaseline | None:
        """The immutable actual observer receipt; never a reconstructed copy."""
        return self._result_baseline

    @property
    def commit_permit(self) -> CommitPermit | None:
        """The caller's actual consumed permit, retained even if input raises."""
        return self._commit_permit

    @property
    def commit_metadata(self) -> dict:
        """Accepted commit_once call metadata, never input/page confirmation.

        commit_called can be true when its deadline blocks before click_once;
        input_metadata() separately records actual SendInput attempts.
        """
        return {"commit_called": self._commit_called,
                "commit_returned_normally": self._commit_returned_normally,
                "input_outcome_unknown": self._commit_input_outcome_unknown,
                "business_success": False}

    def _result_binding_matches(self, request_id: str) -> bool:
        if self._result_observer is None:
            return True
        baseline = self._result_baseline
        return (isinstance(baseline, NativeResultBaseline)
                and baseline.request_id == request_id and baseline.frame_id == self.frame.frame_id
                and baseline.deadline_ns == self._result_deadline_ns
                # Reject an observer accidentally reused/rebound by its caller.
                # This is only an in-memory identity check, never another query.
                and self._result_observer._baseline is baseline)

    def observe_result(self, *, expected_title_sha256: str | None = None) -> NativeResultObservation:
        """Read-only observation remains available after cleanup/unknown input.

        A new host window is metadata evidence only. This method never retries
        input, revives a retired adapter or closes the result. A scoped login
        observer may read a fixed restriction notice from its proven new host.
        """
        if (self._result_observer is None or self._result_baseline is None
                or self._commit_permit is None or not self._commit_called):
            return NativeResultObservation("REJECTED", "result_observer_baseline_or_consumed_commit_unavailable")
        return self._result_observer.observe_new_window(self._result_baseline, self._commit_permit,
            deadline_ns=self._result_deadline_ns, expected_title_sha256=expected_title_sha256)

    def _deadline(self, deadline_ns: int) -> None:
        if type(deadline_ns) is not int or self._clock() >= deadline_ns:
            raise TimeoutError("native_port_deadline_expired")

    def input_metadata(self) -> dict:
        """Detached dispatch metadata only; no pixels, key text or business claim."""
        return self._input.metadata_only()

    def _current(self) -> bool:
        return (not self._cleanup_attempted and self._source_is_current() is True
                and stage_mapping_is_current(self.frame, self.mapping))

    def _stage_matches(self, stage: StageReceipt) -> bool:
        return (isinstance(stage, StageReceipt) and stage.source == FrameBinding.from_frame(self.frame)
                and stage.stage_id == self.mapping.stage_id and stage.stage_epoch == self.mapping.stage_epoch
                and stage.payload_bbox_physical == self.mapping.payload_bbox_physical
                and stage.transport_bbox_physical == self.mapping.transport_bbox_physical)

    def _request_matches(self, request_id: str) -> bool:
        return self._request == request_id

    def prepare_request(self, request_id: str) -> None:
        """Read-only result baseline before any fresh input/absence evidence.

        Preparation may take time without consuming a short native input lease.
        It cannot trigger/select/commit, and the normal source and absence gates
        are acquired again afterwards. Legacy direct adapter callers may still
        acquire their baseline inside absence_receipt.
        """
        if self._request is not None or self._cleanup_attempted or not self._current():
            raise ValueError("request_changed_before_preparation")
        baseline = (self._capture_result_baseline(request_id)
                    if self._result_observer is not None else None)
        if not self._current():
            raise ValueError("source_changed_across_result_baseline")
        self._result_baseline = baseline

    def _capture_result_baseline(self, request_id: str) -> NativeResultBaseline:
        if self._result_baseline is not None:
            raise ValueError("result_baseline_is_single_request")
        evidence_id = str(uuid4())
        captured = self._result_observer.capture_baseline(request_id, self.frame.frame_id,
            evidence_id, deadline_ns=self._result_deadline_ns)
        baseline = captured.baseline if isinstance(captured, NativeResultObservation) else None
        if (not isinstance(captured, NativeResultObservation) or captured.status != "NOT_READY"
                or not isinstance(baseline, NativeResultBaseline)
                or baseline.request_id != request_id or baseline.frame_id != self.frame.frame_id
                or baseline.evidence_id != evidence_id
                or baseline.deadline_ns != self._result_deadline_ns):
            raise ValueError("result_baseline_rejected")
        self.timings.append(PortTiming("result_baseline", baseline.started_ns, baseline.finished_ns))
        return baseline

    def verify_stage(self, frame: FrozenFrame, deadline_ns: int) -> StagePixelVerification:
        self._last_stage_error_code = None
        self._deadline(deadline_ns)
        if frame is not self.frame or not self._current():
            self._last_stage_error_code = "stage_metadata_changed"
            raise ValueError("source_changed_before_native_stage_check")
        start = self._clock()
        try:
            result = verify_frozen_stage(frame, self.mapping, deadline_monotonic_ns=deadline_ns)
        except StageValidationError as exc:
            self._last_stage_error_code = _STAGE_ERROR_CODES.get(str(exc), "stage_validation_rejected")
            raise
        self.timings.append(PortTiming("own_stage", start, self._clock()))
        if not self._current():
            self._last_stage_error_code = "stage_metadata_changed"
            raise ValueError("source_changed_across_native_stage_check")
        self._deadline(deadline_ns)
        return result

    def stage_is_current(self, frame: FrozenFrame, verification: StagePixelVerification) -> bool:
        return (frame is self.frame and verification.mapping == self.mapping and self._current())

    def absence_receipt(self, request_id: str, deadline_ns: int) -> AbsenceReceipt:
        if self._request is not None:
            raise ValueError("adapter_is_single_request")
        self._deadline(deadline_ns)
        start = self._clock()
        probe = probe_screenshot_window()
        end = self._clock()
        self._deadline(deadline_ns)
        result = AbsenceReceipt.from_probe(request_id, probe, started_ns=start, finished_ns=end)
        self._request = request_id
        self.timings.append(PortTiming("absence", start, end))
        if self._result_observer is not None:
            baseline = (self._capture_result_baseline(request_id)
                        if self._result_baseline is None else self._result_baseline)
            self._deadline(deadline_ns)
            if not self._current():
                raise ValueError("source_changed_across_result_baseline")
            self._deadline(deadline_ns)
            self._result_baseline = baseline
            if not self._result_binding_matches(request_id):
                self._result_baseline = None
                raise ValueError("result_baseline_request_changed")
        return result

    def trigger_once(self, request_id: str, trigger_id: str, deadline_ns: int) -> None:
        if (not self._request_matches(request_id) or self._cleanup_attempted
                or not self._result_binding_matches(request_id)):
            raise ValueError("request_changed_or_adapter_retired")
        def eligible() -> bool:
            if not self._request_matches(request_id) or not self._current():
                return False
            if self._api.fixed_version(current_profile().executable)!=current_profile().version:
                return False
            probe = probe_screenshot_window()
            return probe.status == "NOT_FOUND" and not probe.candidates and not probe.errors
        start = self._clock()
        self._input.trigger_once(request_id, trigger_id, deadline_ns, eligible)
        self.timings.append(PortTiming("trigger_dispatch", start, self._clock()))

    def _snapshot_once(self, deadline_ns: int) -> NativeSnapshot:
        self._deadline(deadline_ns)
        start = self._clock()
        self._latest_native_probe_result = None
        self._last_native_probe = None
        self._last_native_error_code = None
        try:
            probe = probe_screenshot_window()
            if isinstance(probe, WindowProbeResult):
                self._latest_native_probe_result = probe
                self._last_native_probe = probe.to_dict()
            identity = _require_eligible_probe(probe)
            version = self._api.fixed_version(identity.executable_path)
            result = NativeSnapshot.from_probe(probe, started_ns=start, finished_ns=self._clock(),
                                               fixed_file_version=version)
            # Fixed version and rendering profile are still mandatory at ready.
            if (version != current_profile().version or identity.dpi != current_profile().dpi
                    or identity.window_class != current_profile().window_class
                    or identity.window_rect != (0, 0, current_profile().width, current_profile().height)):
                self._last_native_error_code = "native_profile_unsupported"
                raise ValueError("native_profile_unsupported")
            self._deadline(deadline_ns)
            return result
        except _ObserverError as exc:
            self._last_native_error_code = (exc.code if exc.code in _NATIVE_PROBE_ERROR_CODES
                                             else "native_metadata_query_failed")
            raise
        except TimeoutError:
            self._last_native_error_code = "native_readiness_deadline_expired"
            raise
        except Exception:
            if self._last_native_error_code is None:
                self._last_native_error_code = "native_metadata_query_failed"
            raise

    def _startup_wait_state(self, pinned_key: tuple | None, deadline_ns: int) -> tuple[str, tuple | None]:
        """Classify only known startup transitions; never manufacture ready proof.

        Own HWNDs are limited to the mapping's DPI references, whose process,
        top-level status and visibility are independently rechecked here. A candidate is
        pinned only after the complete metadata and fixed-version profile pass.
        """
        def reject(code: str) -> None:
            self._last_native_error_code = code
            raise ValueError(code)

        probe = self._latest_native_probe_result
        if not isinstance(probe, WindowProbeResult):
            reject("typed_native_probe_required")
        if (probe.monitor_count != 1 or probe.primary_screen_rect != (0, 0, current_profile().width, current_profile().height)
                or probe.thread_dpi_awareness != 2):
            reject("native_profile_unsupported")
        # A foreground transition is the sole permitted probe error. An error
        # from any other query, ambiguous candidate or unsupported status stops.
        if (probe.errors not in ((), ("foreground_changed_during_query",))
                or probe.status not in ("UNIQUE", "NOT_FOUND", "REJECTED")
                or (probe.status == "REJECTED" and probe.errors != ("foreground_changed_during_query",))
                or (probe.status == "UNIQUE" and (probe.errors or len(probe.candidates) != 1))
                or (probe.status == "NOT_FOUND" and (probe.errors or probe.candidates))
                or len(probe.candidates) > 1):
            reject("native_probe_rejected_or_ambiguous")
        own_api = _StageApi()
        own_pid = self.mapping.own_stage.pid
        if int(own_api.kernel.GetCurrentProcessId()) != own_pid:
            reject("native_startup_own_window_identity_changed")
        allowed_foreground = set()
        for hwnd in self.mapping.dpi_reference_hwnds:
            owner_pid = wintypes.DWORD()
            if (own_api.user.IsWindow(hwnd) and own_api.user.IsWindowVisible(hwnd)
                    and not own_api.user.IsIconic(hwnd)
                    and int(own_api.user.GetAncestor(hwnd, 2) or 0) == hwnd
                    and own_api.user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
                    and owner_pid.value == own_pid):
                allowed_foreground.add(hwnd)
        key = None
        if probe.candidates:
            item = probe.candidates[0]
            if (not isinstance(item, NativeWindowIdentity)
                    or any(type(v) is not int or v <= 0 for v in
                           (item.hwnd, item.pid, item.process_created_filetime_100ns))
                    or ntpath.normcase(item.executable_path) != ntpath.normcase(
                        current_profile().executable)
                    or item.title != current_profile().title or item.window_class != current_profile().window_class
                    or item.hwnd_source != "EnumWindows; not a Sky window token"
                    or item.window_rect != (0, 0, current_profile().width, current_profile().height)
                    or item.client_screen_rect != (0, 0, current_profile().width, current_profile().height) or item.dpi != current_profile().dpi):
                reject("native_profile_unsupported")
            try:
                version = self._api.fixed_version(item.executable_path)
            except Exception:
                self._last_native_error_code = "native_metadata_query_failed"
                raise
            self._deadline(deadline_ns)
            if version != current_profile().version:
                reject("native_profile_unsupported")
            key = _identity_key(item) + (version,)
            if pinned_key is not None and key != pinned_key:
                reject("native_startup_candidate_changed")
            allowed_foreground.add(item.hwnd)
        elif pinned_key is not None:
            reject("native_startup_candidate_disappeared")
        elif probe.status == "UNIQUE":
            reject("native_probe_rejected_or_ambiguous")
        if (pinned_key is None and not probe.candidates and probe.status == "REJECTED"
                and probe.errors == ("foreground_changed_during_query",)
                and probe.foreground_hwnd_at_start in allowed_foreground
                and type(probe.foreground_hwnd_at_end) is int and probe.foreground_hwnd_at_end > 0
                and probe.foreground_hwnd_at_end not in allowed_foreground):
            # A window may be created after EnumWindows visited its position.
            # This unknown endpoint is NOT considered a verified native window.
            # Permit one immediate complete read, never another trigger or an
            # open-ended wait on an unclassified/foreign foreground handle.
            if self._native_creation_recheck_used:
                reject("native_startup_creation_recheck_already_used")
            state = "startup_recheck_unclassified_endpoint"
            self._last_native_error_code = state
            self._native_startup_transitions[state] = self._native_startup_transitions.get(state, 0) + 1
            return state, None
        if (probe.foreground_hwnd_at_start not in allowed_foreground
                or probe.foreground_hwnd_at_end not in allowed_foreground):
            reject("native_startup_foreground_left_allowed_windows")
        state = "startup_wait_native_foreground" if key is not None else "startup_wait_not_found"
        self._last_native_error_code = state
        self._native_startup_transitions[state] = self._native_startup_transitions.get(state, 0) + 1
        return state, key

    def native_snapshot(self, request_id: str, deadline_ns: int | None) -> NativeSnapshot:
        if not self._request_matches(request_id) or self._cleanup_attempted:
            raise ValueError("request_changed_or_adapter_retired")
        start = self._clock()
        pinned_key = None
        unbounded = deadline_ns is None
        settled, previous_progress = 0, None
        while True:
            if unbounded:
                deadline_ns = self._clock()+2_000_000_000
            try:
                self._deadline(deadline_ns)
                if not self._request_matches(request_id) or not self._current():
                    self._last_native_error_code = "native_source_or_request_changed"
                    raise ValueError("native_source_or_request_changed")
                self._deadline(deadline_ns)
                try:
                    result = self._snapshot_once(deadline_ns)
                except _ObserverError as exc:
                    # Only the first post-trigger discovery may wait. Once bound,
                    # any loss of strict readiness is immediately ineligible.
                    if (self._bound is not None or exc.code not in (
                            "native_identity_not_unique_or_complete",
                            "verified_native_window_not_stably_foreground")):
                        raise
                    state, observed_key = self._startup_wait_state(pinned_key, deadline_ns)
                    if state == "startup_recheck_unclassified_endpoint":
                        endpoint = self._latest_native_probe_result.foreground_hwnd_at_end
                        self._native_creation_recheck_used = True
                        self._deadline(deadline_ns)
                        try:
                            result = self._snapshot_once(deadline_ns)
                        except TimeoutError:
                            raise
                        except Exception:
                            self._last_native_error_code = "native_startup_creation_endpoint_not_verified"
                            raise ValueError("native_startup_creation_endpoint_not_verified") from None
                        if result.instance.hwnd != endpoint:
                            self._last_native_error_code = "native_startup_creation_endpoint_changed"
                            raise ValueError("native_startup_creation_endpoint_changed")
                        # The strict snapshot itself supplies the full identity;
                        # the preceding bare endpoint supplied no native proof.
                        pinned_key = _identity_key_from_instance(result.instance) + (result.instance.fixed_file_version,)
                    else:
                        if observed_key is not None:
                            pinned_key = observed_key
                        self._deadline(deadline_ns)
                        if unbounded:
                            progress = (state, pinned_key)
                            settled = settled+1 if progress == previous_progress else 1
                            previous_progress = progress
                            if settled >= 32:
                                self._last_native_error_code = "native_readiness_no_progress_after_complete_samples"
                                raise ValueError("native_readiness_no_progress_after_complete_samples")
                            time.sleep(min(0.25, 0.012*settled))
                        else:
                            time.sleep(min(0.012, max(0, (deadline_ns-self._clock())/1e9)))
                        continue
                self._deadline(deadline_ns)
                key = _identity_key_from_instance(result.instance) + (result.instance.fixed_file_version,)
                if pinned_key is not None and key != pinned_key:
                    self._last_native_error_code = "native_startup_candidate_changed"
                    raise ValueError("native_startup_candidate_changed")
                if not self._current():
                    self._last_native_error_code = "native_source_or_request_changed"
                    raise ValueError("native_source_or_request_changed")
                self._deadline(deadline_ns)
                if self._bound is not None and self._bound != result.instance:
                    self._last_native_error_code = "native_instance_changed"
                    raise ValueError("native_instance_changed")
                end = self._clock()
                self._deadline(deadline_ns)
                self._bound = result.instance
                self.timings.append(PortTiming("native_ready", start, end))
                return result
            except TimeoutError:
                self._last_native_error_code = "native_readiness_deadline_expired"
                raise

    def _same_live(self, instance: NativeInstance, request_id: str, deadline_ns: int,
                   *, require_source: bool = True) -> bool:
        if (self._bound != instance or not self._request_matches(request_id)
                or (require_source and not self._current())):
            return False
        try:
            return self._snapshot_once(deadline_ns).instance == instance
        except Exception:
            return False

    def select_once(self, instance: NativeInstance, bbox: tuple[int, int, int, int],
                    request_id: str, deadline_ns: int) -> None:
        if not self._request_matches(request_id) or self._cleanup_attempted:
            raise ValueError("request_changed_or_adapter_retired")
        if bbox != self.mapping.transport_bbox_physical:
            raise ValueError("selection_differs_from_verified_stage")
        start = self._clock()
        self._input.select_once(instance, bbox, request_id, deadline_ns,
            lambda: self._same_live(instance, request_id, deadline_ns))
        self.timings.append(PortTiming("select_dispatch", start, self._clock()))

    def _complete_wait_sample(self, observation: object, stage: StageReceipt,
                              instance: NativeInstance, request_id: str, deadline_ns: int,
                              acquired_start: int, acquired_end: int) -> bool:
        """Validate all records before waiting on an early border-colour failure.

        Extent inference stops at its first error. A paint-related first error
        must not hide a later malformed strip, wrong instance or stale binding.
        This checks acquisition structure only; it neither infers an actual edge
        nor checks/predicts display-A colours, and never creates a receipt.
        """
        def integer(value: object) -> bool:
            return type(value) is int and value >= 0
        def digest(value: object) -> bool:
            return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        try:
            if (not isinstance(observation, dict) or observation.get("schema_version") != 1
                    or observation.get("status") not in ("PAYLOAD_MISMATCH", "PAYLOAD_MATCH_EXTENT_UNVERIFIED")
                    or observation.get("errors") not in ([], ()) or observation.get("read_only") is not True
                    or observation.get("native_input_sent") is not False or observation.get("raw_pixels_persisted") is not False
                    or observation.get("session_id") != request_id):
                return False
            identifier = observation.get("evidence_id")
            parsed = UUID(identifier) if isinstance(identifier, str) else None
            if parsed is None or not parsed.int or str(parsed) != identifier:
                return False
            plan = _plan(self.frame, self.mapping.metadata_only(), self.frame.generation)
            expected_stage = plan.metadata_only()
            observed_stage = observation.get("stage_mapping")
            if not isinstance(observed_stage, dict):
                return False
            for name in ("frame_id", "generation", "stage_id", "stage_epoch", "source_capture_sha256", "topology_sha256"):
                if observation.get(name) != expected_stage[name] or observed_stage.get(name) != expected_stage[name]:
                    return False
            for name in ("payload_bbox_physical", "transport_requested_bbox_physical", "white_padding_bbox_physical"):
                if tuple(observed_stage.get(name) or ()) != expected_stage[name]:
                    return False
            if any(observed_stage.get(name) != expected_stage[name]
                   for name in ("guard_physical_px", "white_padding_physical_px")):
                return False
            started, completed, finished = (observation.get(name) for name in
                ("started_monotonic_ns", "capture_completed_monotonic_ns", "finished_monotonic_ns"))
            if (not all(integer(v) for v in (started, completed, finished))
                    or not acquired_start <= started <= completed <= finished <= acquired_end < deadline_ns
                    or observation.get("deadline_monotonic_ns") != deadline_ns
                    or deadline_ns-started > 2_000_000_000):
                return False
            versions = observation.get("version_metadata")
            if (not isinstance(versions, dict) or any(tuple(versions.get(name) or ()) != instance.fixed_file_version
                    for name in ("fixed_file_version", "expected_fixed_file_version", "fixed_file_version_after"))):
                return False
            for name in ("native_identity_before", "native_identity_after"):
                snapshot = NativeSnapshot.from_probe(observation.get(name), started_ns=started,
                    finished_ns=finished, fixed_file_version=instance.fixed_file_version)
                if (snapshot.instance != instance or snapshot.monitor_count != 1
                        or snapshot.primary_screen_rect != (0, 0, current_profile().width, current_profile().height) or snapshot.thread_dpi_awareness != 2):
                    return False
            payload = observation.get("payload_comparison")
            if (not isinstance(payload, dict) or tuple(payload.get("captured_bbox_physical") or ()) != plan.payload
                    or (payload.get("width"), payload.get("height")) != (self.frame.width, self.frame.height)
                    or payload.get("expected_rgb_sha256") != self.frame.capture_sha256
                    or not digest(payload.get("captured_rgb_sha256"))
                    or type(payload.get("exact_rgb_bytes_equal")) is not bool
                    or (observation["status"] == "PAYLOAD_MATCH_EXTENT_UNVERIFIED") != payload["exact_rgb_bytes_equal"]):
                return False
            previous = payload.get("capture_ended_monotonic_ns")
            begin = payload.get("capture_started_monotonic_ns")
            if not all(integer(v) for v in (begin, previous)) or not started <= begin <= previous <= completed:
                return False
            sections = observation.get("edge_observations")
            expected_sections = _sections(plan)
            if not isinstance(sections, (tuple, list)) or len(sections) != 8:
                return False
            expected_pixels = self.frame.width*self.frame.height + 8*18*8
            if (observation.get("captured_pixels") != expected_pixels
                    or observation.get("planned_capture_pixels") != expected_pixels):
                return False
            for section, expected in zip(sections, expected_sections):
                if (not isinstance(section, dict) or section.get("side") != expected.side
                        or section.get("quarter_numerator") != expected.quarter or section.get("quarter_denominator") != 4
                        or tuple(section.get("captured_bbox_physical") or ()) != expected.bbox
                        or section.get("normal_axis") != expected.normal_axis or not digest(section.get("captured_rgb_sha256"))
                        or (section.get("width"), section.get("height")) !=
                            (expected.bbox[2]-expected.bbox[0], expected.bbox[3]-expected.bbox[1])):
                    return False
                begin, end = section.get("capture_started_monotonic_ns"), section.get("capture_ended_monotonic_ns")
                if not all(integer(v) for v in (begin, end)) or not previous <= begin <= end <= completed:
                    return False
                previous = end
                rows = section.get("normal_profiles")
                if not isinstance(rows, (tuple, list)) or len(rows) != 18:
                    return False
                origin = expected.bbox[1 if expected.normal_axis == "y" else 0]
                for index, row in enumerate(rows):
                    if (not isinstance(row, dict) or row.get("physical_pixel_coordinate") != origin+index
                            or row.get("tangential_pixel_count") != 8):
                        return False
                    low, high, mean = (row.get(name) for name in ("rgb_min", "rgb_max", "rgb_mean"))
                    if (not all(isinstance(v, (tuple, list)) and len(v) == 3 for v in (low, high, mean))
                            or not all(type(v) is int and 0 <= v <= 255 for v in (*low, *high))
                            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                and math.isfinite(v) and low[i] <= v <= high[i] for i, v in enumerate(mean))):
                        return False
                    white_count = row.get("exact_source_white_pixel_count")
                    if type(white_count) is not int or not 0 <= white_count <= 8:
                        return False
                    if tuple(low) == tuple(high) and white_count != (8 if tuple(low) == (255, 255, 255) else 0):
                        return False
            return self._stage_matches(stage)
        except Exception:
            return False

    def selection_evidence(self, frame: FrozenFrame, stage: StageReceipt,
                           instance: NativeInstance, request_id: str, deadline_ns: int | None) -> dict:
        unbounded = deadline_ns is None
        if unbounded:
            deadline_ns = self._clock()+2_000_000_000
        settled, previous_progress = 0, None
        self._selection_extent = None
        self._last_selection_observation = None
        self._last_selection_error_code = None
        self._last_selection_boundary_reason = None
        self._selection_poll_count = 0
        if (frame is not self.frame or not self._stage_matches(stage)
                or not self._same_live(instance, request_id, deadline_ns)):
            self._last_selection_error_code = "selection_source_or_native_instance_changed"
            raise ValueError("selection_source_or_native_instance_changed")
        try:
            while True:
                if unbounded:
                    deadline_ns = self._clock()+2_000_000_000
                self._deadline(deadline_ns)
                if not self._current():
                    self._last_selection_error_code = "source_changed_across_selection_evidence"
                    raise ValueError("source_changed_across_selection_evidence")
                self._deadline(deadline_ns)
                start = self._clock()
                self._selection_poll_count += 1
                try:
                    observation = observe_native_selection_pixels(frame, stage_mapping=self.mapping.metadata_only(),
                        session_id=request_id, generation=frame.generation, deadline_monotonic_ns=deadline_ns)
                except Exception:
                    self._last_selection_error_code = "selection_capture_failed"
                    raise
                end = self._clock()
                self._last_selection_observation = deepcopy(observation)
                self.timings.append(PortTiming("selection_pixels_extent", start, end))
                self._deadline(deadline_ns)
                if not self._current():
                    self._last_selection_error_code = "source_changed_across_selection_evidence"
                    raise ValueError("source_changed_across_selection_evidence")
                self._deadline(deadline_ns)
                if (isinstance(observation, dict) and observation.get('status') == 'REJECTED'
                        and 'total_capture_exceeds_pixel_budget' in observation.get('errors', ())):
                    self._last_selection_error_code = 'total_capture_exceeds_pixel_budget'
                    raise ValueError('total_capture_exceeds_pixel_budget')
                try:
                    result = extent_evidence_from_observation(observation)
                except (ValueError, TypeError, KeyError) as exc:
                    self._deadline(deadline_ns)
                    prefix = "native selection extent unverified: "
                    reason = str(exc)[len(prefix):] if isinstance(exc, ValueError) and str(exc).startswith(prefix) else None
                    self._last_selection_error_code = "selection_extent_conversion_rejected"
                    self._last_selection_boundary_reason = reason if reason in _WAITABLE_BOUNDARY_REASONS else "boundary_rejected_other"
                    if reason not in _WAITABLE_BOUNDARY_REASONS:
                        raise
                    if not self._complete_wait_sample(observation, stage, instance, request_id, deadline_ns, start, end):
                        self._last_selection_error_code = "selection_wait_sample_incomplete_or_changed"
                        raise ValueError("selection_wait_sample_incomplete_or_changed") from None
                    if not self._same_live(instance, request_id, deadline_ns):
                        self._last_selection_error_code = "selection_source_or_native_instance_changed"
                        raise ValueError("selection_source_or_native_instance_changed")
                    self._deadline(deadline_ns)
                    if unbounded:
                        comparison = observation.get("payload_comparison") or {}
                        progress = (reason, comparison.get("captured_rgb_sha256"),
                                    tuple(observation.get("observed_selection_bbox_physical") or ()))
                        settled = settled+1 if progress == previous_progress else 1
                        previous_progress = progress
                        if settled >= 32:
                            self._last_selection_error_code = "selection_not_verified_after_complete_samples"
                            raise ValueError("selection_not_verified_after_complete_samples")
                        time.sleep(min(0.25, 0.012*settled))
                    else:
                        time.sleep(min(0.012, max(0, (deadline_ns-self._clock())/1e9)))
                    continue
                observed_bbox = result.get("observed_selection_bbox_physical")
                if observed_bbox is not None and tuple(observed_bbox) != stage.transport_bbox_physical:
                    self._last_selection_boundary_reason = "actual_selection_bbox_differs_from_requested"
                    self._last_selection_error_code = "selection_extent_conversion_rejected"
                    raise ValueError("independently_observed_selection_bbox_differs_from_requested")
                try:
                    verified_extent = SelectionExtentReceipt.from_native_extent_evidence(
                        stage=stage, native_instance=instance, evidence=result)
                except (ValueError, TypeError, KeyError):
                    self._last_selection_error_code = "selection_extent_conversion_rejected"
                    self._last_selection_boundary_reason = "boundary_receipt_rejected"
                    raise
                if verified_extent.request_id != request_id:
                    self._last_selection_error_code = "selection_observation_request_changed"
                    raise ValueError("selection_observation_request_changed")
                self._deadline(deadline_ns)
                if not self._current():
                    self._last_selection_error_code = "source_changed_across_selection_evidence"
                    raise ValueError("source_changed_across_selection_evidence")
                self._deadline(deadline_ns)
                self._last_selection_boundary_reason = "exact_calibrated_profile_two_quarters_agree"
                self._last_selection_error_code = None
                self._selection_extent = verified_extent
                return result
        except TimeoutError:
            self._last_selection_error_code = "selection_evidence_deadline_expired"
            raise

    def observe_toolbar(self, extent: SelectionExtentReceipt, *, observation_id: int,
                        deadline_ns: int) -> ObservationReceipt:
        from .native_toolbar import NativeToolbarBand, ToolbarBinding, detect_toolbar_icon
        self._last_toolbar_diagnostic = dict(status="REJECTED", reason="toolbar_precheck_or_capture_failed",
            toolbar_bbox_physical=None, candidate_bbox_physical=None, candidate_count=0,
            finished_ns=None, extent_age_ns=None, max_evidence_age_ns=self._age,
            read_only=True, diagnostic_not_authorization=True)
        if (not isinstance(extent, SelectionExtentReceipt) or self._selection_extent is None
                or extent._observed_boundary_proof is not self._selection_extent._observed_boundary_proof
                or extent != self._selection_extent):
            raise ValueError("toolbar_selection_source_or_instance_changed")
        request_id = extent.request_id
        instance = extent.native_instance
        if (self._cleanup_attempted
                or not self._same_live(instance, request_id, deadline_ns)):
            raise ValueError("toolbar_selection_source_or_instance_changed")
        start = self._clock()
        before_start = self._clock()
        before_probe = probe_screenshot_window()
        identity = _require_eligible_probe(before_probe)
        before_version = self._api.fixed_version(identity.executable_path)
        before = NativeSnapshot.from_probe(before_probe, started_ns=before_start,
            finished_ns=self._clock(), fixed_file_version=before_version)
        if before.instance != instance:
            raise ValueError("toolbar_instance_changed_before_capture")
        left, top, right, bottom = extent.actual_selection_bbox_physical
        if (extent.actual_selection_bbox_physical != self.mapping.transport_bbox_physical
                or top < current_profile().toolbar.band_height or bottom+current_profile().toolbar.band_height > current_profile().height):
            raise ValueError("toolbar_bands_would_clip_or_selection_changed")
        bands = []
        for bbox in ((0, top-current_profile().toolbar.band_height, current_profile().width, top), (0, bottom, current_profile().width, bottom+current_profile().toolbar.band_height)):
            self._deadline(deadline_ns)
            begin = self._clock()
            with _capture_rect(self._api, identity, bbox, bbox) as image:
                end = self._clock()
                bands.append(NativeToolbarBand(bbox, image.tobytes(), begin, end))
            self._deadline(deadline_ns)
        completed = self._clock()
        after_start = self._clock()
        after_probe = probe_screenshot_window()
        after_identity = _require_eligible_probe(after_probe)
        after_version = self._api.fixed_version(after_identity.executable_path)
        after = NativeSnapshot.from_probe(after_probe, started_ns=after_start,
            finished_ns=self._clock(), fixed_file_version=after_version)
        if after.instance != instance or _identity_key(identity) != _identity_key(after_identity):
            raise ValueError("toolbar_instance_changed_across_capture")
        binding = ToolbarBinding(request_id, self.frame.generation, extent, before, after, self._age)
        detected = detect_toolbar_icon(tuple(bands), binding=binding, started_ns=start,
            capture_completed_ns=completed, deadline_ns=deadline_ns, clock=self._clock)
        def bounded_box(value):
            if (type(value) is tuple and len(value) == 4 and all(type(v) is int for v in value)
                    and 0 <= value[0] < value[2] <= current_profile().width and 0 <= value[1] < value[3] <= current_profile().height):
                return value
            return None
        status = getattr(detected, "status", None)
        reason = getattr(detected, "reason", None)
        finished = getattr(detected, "finished_ns", None)
        count = getattr(detected, "candidate_count", None)
        valid_time = type(finished) is int and finished >= extent.measured_ns
        self._last_toolbar_diagnostic = dict(
            status=status if status in ("READ_ONLY_CANDIDATE", "NOT_READY", "AMBIGUOUS", "STALE", "REJECTED") else "REJECTED",
            reason=reason if isinstance(reason, str) and reason in _TOOLBAR_DIAGNOSTIC_REASONS else "toolbar_validation_rejected",
            toolbar_bbox_physical=bounded_box(getattr(detected, "toolbar_bbox_physical", None)),
            candidate_bbox_physical=bounded_box(getattr(detected, "candidate_bbox_physical", None)),
            candidate_count=count if type(count) is int and count >= 0 else 0,
            finished_ns=finished if valid_time else None,
            extent_age_ns=finished-extent.measured_ns if valid_time else None,
            max_evidence_age_ns=self._age, read_only=True, diagnostic_not_authorization=True)
        self.timings.append(PortTiming("toolbar_pixels", start, self._clock()))
        self._deadline(deadline_ns)
        if not self._current():
            raise ValueError("source_changed_across_toolbar_capture")
        result = ObservationReceipt.from_native_observation(detected, observation_id=observation_id)
        self._deadline(deadline_ns)
        return result

    def commit_once(self, instance: NativeInstance, permit: CommitPermit, deadline_ns: int) -> None:
        if (not isinstance(permit, CommitPermit) or permit.frame_id != self.frame.frame_id
                or self._cleanup_attempted or self._commit_called
                or not self._request_matches(permit.request_id)
                or not self._result_binding_matches(permit.request_id)):
            raise ValueError("commit_source_or_request_changed")
        start = self._clock()
        if self._result_observer is not None:
            if (type(permit.consumed_ns) is not int
                    or not self._result_baseline.finished_ns < permit.consumed_ns <= start):
                raise ValueError("commit_does_not_follow_current_result_baseline")
        self._commit_permit = permit
        # The gate has already consumed this permit. Even a deadline/precheck
        # failure here must not allow the same adapter to try another commit.
        self._commit_called = True
        try:
            if self._result_observer is not None and self._result_deadline_ns is not None:
                self._deadline(self._result_deadline_ns)
            self._deadline(deadline_ns)
            self._input.click_once(instance, permit, deadline_ns,
                lambda: self._same_live(instance, permit.request_id, deadline_ns))
        except InputOutcomeUnknown:
            self._commit_input_outcome_unknown = True
            raise
        else:
            self._commit_returned_normally = True
        finally:
            self.timings.append(PortTiming("commit_dispatch", start, self._clock()))

    def _cleanup_probe_state(self, probe: object, instance: NativeInstance, deadline_ns: int) -> str:
        """Post-commit absence is read-only; recovery input keeps its old scope."""
        if not isinstance(probe, WindowProbeResult):
            raise ValueError("cleanup_typed_probe_required")
        if (probe.monitor_count != 1 or probe.primary_screen_rect != (0, 0, current_profile().width, current_profile().height)
                or probe.thread_dpi_awareness != 2):
            raise ValueError("cleanup_profile_unsupported")
        if (probe.errors not in ((), ("foreground_changed_during_query",))
                or probe.status not in ("UNIQUE", "NOT_FOUND", "REJECTED")
                or (probe.status == "REJECTED" and probe.errors != ("foreground_changed_during_query",))
                or (probe.status == "UNIQUE" and (probe.errors or len(probe.candidates) != 1))
                or (probe.status == "NOT_FOUND" and (probe.errors or probe.candidates))
                or len(probe.candidates) > 1):
            raise ValueError("cleanup_probe_rejected_or_ambiguous")
        try:
            own_api = _StageApi()
            own_pid = self.mapping.own_stage.pid
            if int(own_api.kernel.GetCurrentProcessId()) != own_pid:
                raise ValueError("cleanup_own_window_identity_changed")
            own_foreground = set()
            for hwnd in self.mapping.dpi_reference_hwnds:
                pid = wintypes.DWORD()
                if (own_api.user.IsWindow(hwnd) and own_api.user.IsWindowVisible(hwnd)
                        and not own_api.user.IsIconic(hwnd)
                        and int(own_api.user.GetAncestor(hwnd, 2) or 0) == hwnd
                        and own_api.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        and pid.value == own_pid):
                    own_foreground.add(hwnd)
        except ValueError:
            raise
        except Exception:
            raise ValueError("cleanup_own_metadata_unavailable") from None
        if probe.candidates:
            candidate = probe.candidates[0]
            if (not isinstance(candidate, NativeWindowIdentity)
                    or candidate.hwnd_source != "EnumWindows; not a Sky window token"
                    or _identity_key(candidate) != _identity_key_from_instance(instance)
                    or candidate.foreground_match is not (candidate.hwnd == probe.foreground_hwnd_at_start)):
                raise ValueError("cleanup_bound_instance_changed")
            try:
                version = self._api.fixed_version(candidate.executable_path)
            except Exception:
                raise ValueError("cleanup_version_query_failed") from None
            if version != instance.fixed_file_version or version != current_profile().version:
                raise ValueError("cleanup_bound_version_changed")
        if self._commit_returned_normally:
            # Opening a business page may move foreground away from the source
            # and retire the screenshot tool. Never follow that click with Escape.
            self._deadline(deadline_ns)
            if probe.errors:
                raise ValueError("cleanup_probe_rejected_or_ambiguous")
            if (probe.status == "NOT_FOUND" and not probe.candidates
                    and type(probe.foreground_hwnd_at_start) is int
                    and probe.foreground_hwnd_at_start > 0
                    and probe.foreground_hwnd_at_start == probe.foreground_hwnd_at_end
                    and probe.foreground_hwnd_at_start != instance.hwnd):
                return "cleanup_absent_postcommit_readonly"
            if probe.status == "UNIQUE" and len(probe.candidates) == 1:
                return "cleanup_wait_bound_postcommit_readonly"
            raise ValueError("cleanup_postcommit_absence_not_stable")
        allowed = own_foreground | {instance.hwnd}
        if probe.foreground_hwnd_at_start not in allowed or probe.foreground_hwnd_at_end not in allowed:
            raise ValueError("cleanup_foreground_left_allowed_windows")
        self._deadline(deadline_ns)
        if (probe.status == "NOT_FOUND" and not probe.candidates and not probe.errors
                and probe.foreground_hwnd_at_start == probe.foreground_hwnd_at_end
                and probe.foreground_hwnd_at_end in own_foreground):
            return "cleanup_absent_own_foreground"
        if (probe.status == "UNIQUE" and not probe.errors
                and probe.foreground_hwnd_at_start == probe.foreground_hwnd_at_end == instance.hwnd):
            return "cleanup_bound_native_ready"
        # If absent, a bare old numeric HWND is not a verified living instance.
        # Only an actual native -> own foreground transition may wait here.
        if (not probe.candidates and instance.hwnd in (probe.foreground_hwnd_at_start, probe.foreground_hwnd_at_end)
                and not (probe.foreground_hwnd_at_start == instance.hwnd
                         and probe.foreground_hwnd_at_end in own_foreground
                         and probe.errors == ("foreground_changed_during_query",))):
            raise ValueError("cleanup_absent_native_foreground_unverified")
        return "cleanup_wait_known_transition"

    def confirm_cleanup_absence(self) -> str:
        """Bounded late read-only sampling; never renew or repeat input.

        A result host can take foreground while the screenshot tool retires.
        Retry only a complete known transition or the original bound tool;
        ambiguity and changed identity remain terminal unknown outcomes.
        """
        if (not self._cleanup_attempted or not self._commit_returned_normally
                or self._bound is None or self._request is None
                or self._absence_rechecked):
            return "UNKNOWN"
        self._absence_rechecked = True
        deadline = self._clock() + 500_000_000
        consecutive_absent = 0
        try:
            for _ in range(8):
                self._deadline(deadline)
                self._cleanup_probe_count += 1
                probe = probe_screenshot_window()
                self._last_cleanup_probe = probe.to_dict() if isinstance(probe, WindowProbeResult) else None
                self._deadline(deadline)
                try:
                    state = self._cleanup_probe_state(probe, self._bound, deadline)
                except ValueError as exc:
                    # _cleanup_probe_state has already checked profile and any
                    # candidate identity before its post-commit error rejection.
                    # Never turn this transitional observation into absence.
                    known_transition = (str(exc) == 'cleanup_probe_rejected_or_ambiguous'
                        and isinstance(probe, WindowProbeResult)
                        and probe.status == 'REJECTED'
                        and probe.errors == ('foreground_changed_during_query',)
                        and len(probe.candidates) <= 1
                        and type(probe.foreground_hwnd_at_start) is int
                        and type(probe.foreground_hwnd_at_end) is int
                        and probe.foreground_hwnd_at_start > 0
                        and probe.foreground_hwnd_at_end > 0
                        and probe.foreground_hwnd_at_start != probe.foreground_hwnd_at_end)
                    if not known_transition:
                        self._last_cleanup_error_code = 'cleanup_probe_rejected_or_ambiguous'
                        return "UNKNOWN"
                    state = 'cleanup_wait_known_transition'
                self._last_cleanup_probe_code = state
                if state == 'cleanup_absent_postcommit_readonly':
                    consecutive_absent += 1
                    if consecutive_absent == 2:
                        self._last_cleanup_error_code = None
                        return "CONFIRMED_ABSENT"
                elif state in ('cleanup_wait_bound_postcommit_readonly', 'cleanup_wait_known_transition'):
                    consecutive_absent = 0
                else:
                    return "UNKNOWN"
                self._deadline(deadline)
                time.sleep(min(0.02, max(0, (deadline-self._clock())/1e9)))
            self._last_cleanup_error_code = 'cleanup_postcommit_absence_not_stable'
            return "UNKNOWN"
        except TimeoutError:
            self._last_cleanup_error_code = 'cleanup_deadline_expired'
            return "UNKNOWN"
        except Exception:
            self._last_cleanup_error_code = 'cleanup_probe_or_dispatch_failed'
            return "UNKNOWN"

    def cleanup_once(self, instance: NativeInstance, cleanup_deadline_ns: int) -> str:
        if self._cleanup_attempted or self._bound != instance or self._request is None:
            return "UNKNOWN"
        self._cleanup_attempted = True
        self._selection_extent = None
        sent = False
        while True:
            try:
                self._deadline(cleanup_deadline_ns)
                self._cleanup_probe_count += 1
                probe = probe_screenshot_window()
                self._last_cleanup_probe = probe.to_dict() if isinstance(probe, WindowProbeResult) else None
                self._deadline(cleanup_deadline_ns)
                state = self._cleanup_probe_state(probe, instance, cleanup_deadline_ns)
                self._last_cleanup_probe_code = state
                if state in ("cleanup_absent_own_foreground", "cleanup_absent_postcommit_readonly"):
                    return "CONFIRMED_CLOSED" if sent else "CONFIRMED_ABSENT"
                if state == "cleanup_bound_native_ready" and not sent and not self._commit_returned_normally:
                    sent = True  # One cleanup attempt even if the dispatcher raises.
                    self._input.cancel_once(instance, self._request, cleanup_deadline_ns,
                        lambda: self._same_live(instance, self._request, cleanup_deadline_ns, require_source=False))
                    self._deadline(cleanup_deadline_ns)
                self._deadline(cleanup_deadline_ns)
                time.sleep(min(0.012, max(0, (cleanup_deadline_ns-self._clock())/1e9)))
            except TimeoutError:
                self._last_cleanup_error_code = "cleanup_deadline_expired"
                return "UNKNOWN"
            except ValueError as exc:
                code = str(exc)
                self._last_cleanup_error_code = code if code in {
                    "cleanup_typed_probe_required", "cleanup_profile_unsupported",
                    "cleanup_probe_rejected_or_ambiguous", "cleanup_own_window_identity_changed",
                    "cleanup_own_metadata_unavailable", "cleanup_bound_instance_changed",
                    "cleanup_version_query_failed", "cleanup_bound_version_changed",
                    "cleanup_foreground_left_allowed_windows", "cleanup_absent_native_foreground_unverified",
                    "cleanup_postcommit_absence_not_stable",
                } else "cleanup_probe_or_dispatch_failed"
                return "UNKNOWN"
            except Exception:
                self._last_cleanup_error_code = "cleanup_probe_or_dispatch_failed"
                return "UNKNOWN"


def _identity_key_from_instance(instance: NativeInstance) -> tuple:
    # Reuse the exact metadata key shape without accepting an arbitrary HWND.
    from .native_identity import NativeWindowIdentity
    identity = NativeWindowIdentity(instance.hwnd, instance.pid, instance.process_created_filetime_100ns,
        instance.executable_path, instance.title, instance.window_class, instance.window_rect,
        instance.client_screen_rect, instance.dpi, True)
    return _identity_key(identity)
