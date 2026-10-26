"""One serial request: real ports supply provenance and bounded native input.

run prepares and retires its selection. run_and_dispatch also consumes one
commit permit and calls the port once; neither path claims QR business success.
No GUI, native query or input implementation lives here. Synchronous ports cannot
be forcibly interrupted: late returns are rejected.
Input ports must do one minimal dispatch, no waiting/retry, and recheck the full
native identity immediately before native input. Cleanup must similarly check the
entire supplied instance; a different/reused HWND is never an eligible target.

All own-source mutation must call cancel BEFORE mutation. The shared edge lock
linearizes cancellation with dispatch: cancellation can wait for a dispatch already
in progress, and reports that fact. A read-only current check alone cannot make an
uncoordinated external UI mutation atomic. No blocking UI-thread marshalling may
occur inside stage_is_current or input ports while this lock is held.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Callable, Mapping, Protocol
from uuid import UUID

from .frozen_frame import FrozenFrame
from .stage import StagePixelVerification
from .handoff import (AbsenceReceipt, CaptureReceipt, CommitCheck, CommitPermit,
                      FrameBinding, GateRejected, HandoffSession, ObservationReceipt,
                      NativeInstance, NativeSnapshot, Policy, SelectionReceipt, StageReceipt,
                      SelectionExtentReceipt, ProfiledDisplayReceipt, NATIVE_DISPLAY_PROFILE_A,
                      NativeDisplayPredictionMismatch)


class HandoffPort(Protocol):
    """Adapters are responsible for truthful live observations and bounded calls.

    All deadlines are absolute monotonic nanoseconds. trigger/select return only
    after one dispatch call returns, not after UI/business acceptance. cleanup's
    CONFIRMED_* values require a real post-action absence check of that instance.
    No port may retry an input or substitute a baseline/arbitrary window.
    """
    def verify_stage(self, frame: FrozenFrame, deadline_ns: int) -> StagePixelVerification: ...
    def stage_is_current(self, frame: FrozenFrame, verification: StagePixelVerification) -> bool: ...
    def absence_receipt(self, request_id: str, deadline_ns: int) -> AbsenceReceipt: ...
    def trigger_once(self, request_id: str, trigger_id: str, deadline_ns: int) -> None: ...
    def native_snapshot(self, request_id: str, deadline_ns: int | None) -> NativeSnapshot: ...
    def select_once(self, instance: NativeInstance, bbox: tuple[int, int, int, int],
                    request_id: str, deadline_ns: int) -> None: ...
    def selection_evidence(self, frame: FrozenFrame, stage: StageReceipt,
                           instance: NativeInstance, request_id: str, deadline_ns: int | None) -> Mapping: ...
    def observe_toolbar(self, extent: SelectionExtentReceipt, *, observation_id: int,
                        deadline_ns: int) -> ObservationReceipt: ...
    def commit_once(self, instance: NativeInstance, permit: CommitPermit, deadline_ns: int) -> None: ...
    def cleanup_once(self, instance: NativeInstance, cleanup_deadline_ns: int) -> str: ...


@dataclass(frozen=True, slots=True)
class PreparationResult:
    status: str
    reason: str
    request_id: str | None
    handoff_state: str | None
    trigger_attempted: bool
    select_attempted: bool
    cleanup_status: str
    native_instance: NativeInstance | None
    commit_authorization: bool = field(default=False, init=False)
    selection_reusable: bool = field(default=False, init=False)


@dataclass(frozen=True, slots=True)
class DispatchResult(PreparationResult):
    # These count the commit port invocation, never accepted input or an opened
    # page. Latency ends at that invocation's return/raise, before business I/O.
    dispatch_attempted: bool
    dispatch_count: int
    dispatch_latency_ns: int | None


@dataclass(slots=True)
class _Run:
    session: HandoffSession
    last_ns: int
    started_ns: int = 0
    cancelled: bool = False
    completed: bool = False
    trigger_attempted: bool = False
    select_attempted: bool = False
    input_in_progress: bool = False
    native: NativeInstance | None = None
    commit_consumed: bool = False
    dispatch_attempted: bool = False
    dispatch_count: int = 0
    dispatch_latency_ns: int | None = None
    selection_evidence_ids: set[str] = field(default_factory=set)
    last_selection_finished_ns: int | None = None
    display_prediction_mismatches: int = 0


class _Stopped(Exception):
    def __init__(self, status: str, reason: str) -> None:
        self.status, self.reason = status, reason


class HandoffController:
    """One run; all proof and any commit belong to that same request.

    Both paths retire the bound screenshot instance. Returned selection proof is
    historical and cannot be resumed, and no result exposes a reusable permit.
    """
    def __init__(self, port: HandoffPort, *, policy: Policy, cleanup_budget_ns: int,
                 clock: Callable[[], int] = time.monotonic_ns) -> None:
        if not isinstance(policy, Policy):
            raise TypeError("an explicit Handoff policy is required")
        if type(cleanup_budget_ns) is not int or cleanup_budget_ns <= 0:
            raise ValueError("a separate positive cleanup budget is required")
        self._port, self._policy, self._cleanup_budget_ns = port, policy, cleanup_budget_ns
        self._clock = clock
        self._flight, self._edge = threading.Lock(), threading.RLock()
        self._active: _Run | None = None
        self._last_display_prediction_mismatches = 0

    @property
    def display_prediction_mismatches(self) -> int:
        """Latest request's exact-F mismatch count, never a success measure."""
        with self._edge:
            return (self._active.display_prediction_mismatches if self._active is not None
                    else self._last_display_prediction_mismatches)

    def cancel(self) -> str:
        """Source invalidation uses this same method before changing the source."""
        with self._edge:
            run = self._active
            if run is None:
                return "NO_ACTIVE_REQUEST"
            if run.commit_consumed:
                return "TOO_LATE"
            if run.completed:
                return "NO_ACTIVE_REQUEST"
            if run.cancelled:
                return "ALREADY_CANCELLED"
            run.cancelled = True
            run.session.cancel()
            if run.select_attempted:
                return "CANCELLED_AFTER_SELECT_DISPATCH"
            if run.trigger_attempted:
                return "CANCELLED_AFTER_TRIGGER_DISPATCH"
            return "CANCELLED_BEFORE_DISPATCH"

    def _guard(self, run: _Run, frame: FrozenFrame,
               verification: StagePixelVerification | None = None) -> None:
        # Caller holds _edge. Check again after a potentially slow current probe.
        def check() -> None:
            if run.cancelled:
                raise _Stopped("CANCELLED", "cancelled_at_dispatch_boundary")
            now = self._clock()
            if type(now) is not int or now < run.last_ns:
                raise _Stopped("REJECTED", "monotonic_clock_invalid_or_regressed")
            run.last_ns = now
            if run.session.deadline_ns is not None and now >= run.session.deadline_ns:
                raise _Stopped("TIMED_OUT", "request_deadline_expired")
        check()
        if verification is not None:
            current = self._port.stage_is_current(frame, verification)
            check()
            if current is not True:
                raise _Stopped("REJECTED", "source_generation_or_stage_changed")

    def _operation_deadline(self, run: _Run) -> int:
        """A fresh short native-call lease, never a refreshed request permit.

        Unbounded jobs retain one session/one trigger/select/commit. Only this
        call's watchdog renews; all evidence age and source/identity gates stay
        unchanged. Legacy explicitly bounded sessions retain their old expiry.
        """
        if run.session.deadline_ns is not None:
            return run.session.deadline_ns
        now = self._clock()
        if type(now) is not int or now < run.last_ns:
            raise _Stopped("REJECTED", "monotonic_clock_invalid_or_regressed")
        return now + self._policy.operation_budget_ns

    def _readiness_deadline(self, run: _Run) -> int | None:
        # A read-only multi-sample wait is not a single native input. Real ports
        # bound every individual probe and terminate settled unsupported states.
        return run.session.deadline_ns

    def _read_wait(self, run: _Run) -> None:
        remaining = (run.session.deadline_ns-run.last_ns
                     if run.session.deadline_ns is not None else 12_000_000)
        time.sleep(min(12_000_000, max(0, remaining))/1e9)

    def _cleanup(self, run: _Run, status: str) -> str:

        if run.native is None:
            return "UNBOUND_OUTCOME_UNKNOWN" if run.trigger_attempted else "NOT_NEEDED"
        try:
            now = self._clock()
            if type(now) is not int or now < run.last_ns:
                return "UNKNOWN"
            # Independent budget: cleanup can occur after request expiration.
            deadline = now + self._cleanup_budget_ns
            result = self._port.cleanup_once(run.native, deadline)
            finished = self._clock()
            if type(finished) is not int or finished < now or finished >= deadline:
                return "UNKNOWN"
            return result if result in ("CONFIRMED_CLOSED", "CONFIRMED_ABSENT", "FAILED", "UNKNOWN") else "UNKNOWN"
        except Exception:
            # A raised dispatch may already have had a side effect; never claim closed.
            return "UNKNOWN"

    def _selection(self, run: _Run, frame: FrozenFrame, stage: StageReceipt,
                   verification: StagePixelVerification) -> SelectionReceipt | ProfiledDisplayReceipt:
        pending_reads = 0
        while True:
            with self._edge:
                self._guard(run, frame, verification)
            evidence = self._port.selection_evidence(frame, stage, run.native,
                                                     run.session.request_id, self._readiness_deadline(run))
            with self._edge:
                self._guard(run, frame, verification)
                try:
                    identifier = evidence.get("evidence_id") if isinstance(evidence, Mapping) else None
                    parsed = UUID(identifier) if isinstance(identifier, str) else None
                    started, finished = evidence.get("started_monotonic_ns"), evidence.get("finished_monotonic_ns")
                    if (parsed is None or not parsed.int or str(parsed) != identifier
                            or type(started) is not int or type(finished) is not int or not 0 <= started <= finished):
                        raise ValueError("invalid selection sampling identity/interval")
                except (ValueError, TypeError, AttributeError):
                    raise _Stopped("CONTENT_UNPROVEN", "selection_evidence_identity_or_interval_invalid") from None
                previous = run.last_selection_finished_ns
                if (identifier in run.selection_evidence_ids
                        or (previous is not None and (started < previous or finished <= previous))):
                    raise _Stopped("CONTENT_UNPROVEN", "selection_evidence_replayed_or_overlapping")
                # Track every sample, including mismatches, across all calls to
                # this method and the final selection refresh before commit.
                run.selection_evidence_ids.add(identifier)
                run.last_selection_finished_ns = finished
                try:
                    if self._policy.selection_evidence_mode == NATIVE_DISPLAY_PROFILE_A:
                        extent = SelectionExtentReceipt.from_native_extent_evidence(
                            stage=stage, native_instance=run.native, evidence=evidence)
                        selection = ProfiledDisplayReceipt.from_native_display_evidence(
                            frame=frame, stage=stage, extent=extent,
                            native_instance=run.native, evidence=evidence, clock=self._clock)
                    else:
                        selection = SelectionReceipt.from_native_selection_evidence(
                            stage=stage, native_instance=run.native, evidence=evidence)
                except NativeDisplayPredictionMismatch:
                    run.display_prediction_mismatches += 1
                    self._guard(run, frame, verification)
                    pending_reads += 1
                    if (run.session.deadline_ns is None
                            and pending_reads >= self._policy.max_pending_observations):
                        raise _Stopped("CONTENT_UNPROVEN", "native_display_not_verified_after_complete_samples")
                except (ValueError, TypeError, KeyError):
                    # If a synchronous read/verification exhausted the request,
                    # report its original deadline rather than start a new wait.
                    self._guard(run, frame, verification)
                    raise _Stopped("CONTENT_UNPROVEN", "strict_native_selection_evidence_rejected") from None
                else:
                    self._guard(run, frame, verification)
                    return selection
            # No input or relaxed comparison: a new real sample must pass the
            # same factory. Release _edge so cancellation can enter while waiting.
            self._read_wait(run)

    def _dispatch(self, run: _Run, frame: FrozenFrame, stage: StageReceipt,
                  verification: StagePixelVerification,
                  selection: SelectionReceipt | ProfiledDisplayReceipt) -> None:
        session = run.session
        observation_id = 0
        pending_reads = 0
        last_pending = None
        while True:
            with self._edge:
                self._guard(run, frame, verification)
                refresh = observation_id > 0
            if refresh:
                # Each later visual needs its own fresh independent extent.
                # A previous visual consumes the same fixed evidence-age budget;
                # refreshing reads never repeats selection.
                selection = self._selection(run, frame, stage, verification)
            visual = self._port.observe_toolbar(selection.extent, observation_id=observation_id,
                                                 deadline_ns=self._operation_deadline(run))
            observation_id += 1
            with self._edge:
                self._guard(run, frame, verification)
                if session.observe(visual):
                    break
                self._guard(run, frame, verification)
                pending = (visual.status, visual.candidate_bbox_physical)
                pending_reads = pending_reads+1 if pending == last_pending else 1
                last_pending = pending
                if (session.deadline_ns is None
                        and pending_reads >= self._policy.max_pending_observations):
                    raise _Stopped("CONTENT_UNPROVEN", "native_toolbar_not_verified_after_complete_samples")
            # Only a read-only observation may repeat. Yield between observations
            # and keep every wait inside the same absolute request deadline.
            self._read_wait(run)
        latest_native = self._port.native_snapshot(session.request_id, self._readiness_deadline(run))
        with self._edge:
            self._guard(run, frame, verification)
            # stage_is_current just established that this mapping still describes
            # the live own stage; no drag request is substituted for actual extent.
            mapping = verification.mapping
            check = CommitCheck(session.request_id, FrameBinding.from_frame(frame),
                mapping.stage_id, mapping.stage_epoch, mapping.transport_bbox_physical,
                frame.generation, frame.topology_sha256, selection, visual, latest_native,
                visual.candidate_bbox_physical)
            permit = session.consume_commit_once(check)
            run.commit_consumed = True
            run.dispatch_attempted = run.input_in_progress = True
            run.dispatch_count += 1  # Mark before input; any partial/raise is unknown.
            try:
                self._port.commit_once(run.native, permit, self._operation_deadline(run))
            finally:
                finished = self._clock()
                started = run.started_ns
                if type(finished) is not int or finished < run.last_ns:
                    raise _Stopped("REJECTED", "monotonic_clock_invalid_or_regressed")
                run.dispatch_latency_ns = finished-started
                run.last_ns = finished
            run.input_in_progress = False
            self._guard(run, frame, verification)
            run.completed = True

    def run(self, frame: FrozenFrame) -> PreparationResult:
        """Verify selection then retire it; no toolbar observation or commit."""
        return self._execute(frame, dispatch=False)

    def run_and_dispatch(self, frame: FrozenFrame) -> DispatchResult:
        """Prepare, observe and consume one commit in the same bounded request.

        DISPATCHED_UNCONFIRMED means only that the commit port returned in time.
        Business/result association is deliberately outside this input controller.
        """
        return self._execute(frame, dispatch=True)

    def _execute(self, frame: FrozenFrame, *, dispatch: bool) -> PreparationResult | DispatchResult:
        if not isinstance(frame, FrozenFrame):
            raise TypeError("a FrozenFrame is required")
        if not self._flight.acquire(blocking=False):
            args = ("BUSY", "another_request_is_active", None, None,
                    False, False, "NOT_NEEDED", None)
            return DispatchResult(*args, False, 0, None) if dispatch else PreparationResult(*args)
        run = None
        try:
            session = HandoffSession(frame, policy=self._policy, clock=self._clock)
            with self._edge:
                run = _Run(session, self._clock(), started_ns=self._clock())
                self._active = run
            status, reason = "FAILED", "preparation_incomplete"
            try:
                with self._edge:
                    self._guard(run, frame)
                prepare = getattr(self._port, "prepare_request", None)
                if prepare is not None:
                    prepare(session.request_id)
                with self._edge:
                    self._guard(run, frame)
                verification = self._port.verify_stage(frame, self._operation_deadline(run))
                stage = StageReceipt.from_stage_verification(frame, verification)
                with self._edge:
                    self._guard(run, frame, verification)
                    session.bind_stage(stage)
                absence = self._port.absence_receipt(session.request_id, self._operation_deadline(run))
                with self._edge:
                    self._guard(run, frame, verification)
                    session.record_absence(absence)
                    # Mark before calling input; an exception never permits retry.
                    trigger_id = session.mark_trigger_attempt()
                    run.trigger_attempted = run.input_in_progress = True
                    self._port.trigger_once(session.request_id, trigger_id, self._operation_deadline(run))
                    run.input_in_progress = False
                    self._guard(run, frame, verification)
                snapshot = self._port.native_snapshot(session.request_id, self._readiness_deadline(run))
                with self._edge:
                    self._guard(run, frame, verification)
                    session.bind_capture(CaptureReceipt(session.request_id, absence.evidence_id,
                                                         trigger_id, snapshot))
                    run.native = snapshot.instance
                    self._guard(run, frame, verification)
                    run.select_attempted = run.input_in_progress = True
                    self._port.select_once(run.native, stage.transport_bbox_physical,
                                           session.request_id, self._operation_deadline(run))
                    run.input_in_progress = False
                    self._guard(run, frame, verification)
                selection = self._selection(run, frame, stage, verification)
                with self._edge:
                    self._guard(run, frame, verification)
                    session.bind_selection(selection)
                    self._guard(run, frame, verification)
                    if not dispatch:
                        status = "SELECTION_VERIFIED"
                        reason = ("profile_A_display_verified_raw_unproven_instance_retired_no_submission"
                                  if self._policy.selection_evidence_mode == NATIVE_DISPLAY_PROFILE_A
                                  else "strict_selection_verified_instance_retired_no_submission")
                        run.completed = True  # Completion/cancellation linearization point.
                if dispatch:
                    self._dispatch(run, frame, stage, verification, selection)
                    status, reason = "DISPATCHED_UNCONFIRMED", "single_commit_dispatch_returned_business_result_unconfirmed"
            except _Stopped as exc:
                status = "OUTCOME_UNKNOWN" if run.commit_consumed else exc.status
                reason = "commit_outcome_unknown:" + exc.reason if run.commit_consumed else exc.reason
            except TimeoutError:
                uncertain = run.input_in_progress or run.commit_consumed
                status = "OUTCOME_UNKNOWN" if uncertain else "TIMED_OUT"
                reason = "input_dispatch_outcome_unknown" if uncertain else "port_deadline_expired"
            except GateRejected:
                state = session.snapshot()["state"]
                uncertain = run.input_in_progress or run.commit_consumed
                status = "OUTCOME_UNKNOWN" if uncertain else ("TIMED_OUT" if state == "TIMED_OUT" else "REJECTED")
                reason = "input_dispatch_outcome_unknown" if uncertain else session.snapshot()["reason"]
            except Exception as exc:
                uncertain = run.input_in_progress or run.commit_consumed
                status = "OUTCOME_UNKNOWN" if uncertain else "FAILED"
                reason = "input_dispatch_outcome_unknown" if uncertain else "port_error:" + type(exc).__name__
            if status not in ("SELECTION_VERIFIED", "DISPATCHED_UNCONFIRMED"):
                with self._edge:
                    session.cancel()
                    run.completed = True
            cleanup = self._cleanup(run, status)
            args = (status, reason, session.request_id, session.snapshot()["state"],
                    run.trigger_attempted, run.select_attempted, cleanup, run.native)
            return (DispatchResult(*args, run.dispatch_attempted, run.dispatch_count, run.dispatch_latency_ns)
                    if dispatch else PreparationResult(*args))
        finally:
            with self._edge:
                if self._active is run:
                    self._last_display_prediction_mismatches = run.display_prediction_mismatches if run is not None else 0
                    self._active = None
            self._flight.release()


