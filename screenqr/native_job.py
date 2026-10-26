"""One user-started native job, with no Tk calls or persistent image output.

The UI cancels input BEFORE changing its source/widgets. Its pure Event lease
is the only UI state read by workers. After a confirmed dispatch, source changes
do not cancel the remaining read-only result observation; explicit exit does.
A result slot and done Event, rather than a
bounded UI notification queue, own completion. Long decoding/preparation does not consume an input lease; each native call
receives a short lease from the same one-shot controller session. Result observation is read-only and cannot turn a dispatch into success.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import threading
import time
from typing import Callable

from PIL import Image

from .controller import DispatchResult, HandoffController
from .core import classify_payload, is_wechat_login_confirm
from .decoding import read_qr_instances
from .frozen_frame import FrozenFrame
from .handoff import NATIVE_DISPLAY_PROFILE_A, Policy
from .native_port import WindowsWeChatPort
from .native_capture import MAX_CAPTURE_PIXELS, planned_capture_pixels
from .native_result import NativeResultObservation, NativeResultObserver
from .stage import FrozenStageMapping
from .profiles import DEFAULT_PROFILE, NativeProfile, current_profile, profile_scope, INPUT_BUDGET_NS, RESULT_BUDGET_NS

# 计算不消耗微信输入期限；控制器启动后共用两秒，提交后的观察另计十秒。
MAX_SETTLED_RESULT_SAMPLES = 32
EVIDENCE_AGE_NS = 300_000_000

# Fixed local identifiers only: never interpolate exception text, QR contents,
# window titles, URLs or arbitrary diagnostic values into the product status.
_OPERATION_LABELS = {
    "target_decode": "目标识别", "result_observer_setup": "微信结果观察初始化",
    "native_adapter_setup": "微信输入适配初始化", "controller_setup": "交付初始化",
    "native_flow": "微信交付", "verify_stage": "原图预览校验",
    "stage_is_current": "预览与来源复核", "absence_receipt": "微信窗口基线检查",
    "trigger_once": "唤起微信截图识码", "native_snapshot": "微信截图窗口定位",
    "select_once": "原图选区交付", "selection_evidence": "实际选区与显示校验",
    "observe_toolbar": "微信识码按钮定位", "commit_once": "提交微信识码",
    "result_observation": "打开结果观察",
}
_FAILURE_DETAILS = {
    "source_pixels_differ": "预览像素与原图不一致，请移开遮挡后重新框选。",
    "white_padding_not_white": "二维码周围的空白被遮挡或发生变化，请移开遮挡后重新框选。",
    "stage_metadata_changed": "原图预览的位置或显示状态发生变化，请重新框选。",
    "source_generation_or_stage_changed": "取景或预览布局发生变化，旧请求已停止。",
    "stage_extent_outside_current_even_profile": "扫码区域超出当前适配范围，请缩小取景框。",
    "native_profile_unsupported": "这个微信或屏幕设置暂不能扫，普通码照常能扫。",
    "native_instance_outside_profile": "微信窗口实例不符合当前适配要求。",
    "native_display_or_dpi_profile_changed": "屏幕或缩放配置发生变化，旧请求已停止。",
    "native_identity_not_unique_or_complete": "无法唯一定位微信截图窗口，请先关闭遗留截图浮层。",
    "native_probe_rejected_or_ambiguous": "微信截图窗口检测存在歧义，本次没有继续识码。",
    "verified_native_window_not_stably_foreground": "微信截图窗口没有稳定处于前台，请避免其他窗口抢占焦点。",
    "native_instance_not_stably_foreground": "微信截图窗口失去前台状态，请重新框选。",
    "native_startup_foreground_left_allowed_windows": "其他窗口抢占了截图启动过程的焦点，请重新框选。",
    "native_metadata_query_failed": "无法读取微信窗口的必要信息，请检查微信是否正常运行。",
    "selection_capture_failed": "无法获取实际选区的校验画面，本次已停止。",
    "selection_extent_conversion_rejected": "实际选区边界未通过严格校验，请重新框选。",
    "selection_source_or_native_instance_changed": "选区来源或微信窗口实例发生变化，请重新框选。",
    "source_changed_across_selection_evidence": "核验期间取景或预览发生变化，请重新框选。",
    "evidence_out_of_order_future_or_stale": "校验证据已过期或时序不符，请重新框选。",
    "profiled_display_evidence_expired": "原图显示校验证据已过期，请重新框选。",
    "commit_visual_expired": "识码按钮证据已过期，本次未继续提交。",
    "three_ring_qr_structure_not_found": "未找到符合当前适配结构的微信识码按钮。",
    "no_unique_toolbar_geometry": "无法唯一定位微信截图工具栏。",
    "multiple_structural_qr_candidates": "检测到多个识码按钮候选，本次没有猜测点击。",
    "deadline_or_extent_age_expired": "工具栏校验证据已过期，本次未继续提交。",
    "trigger_own_foreground_changed": "其他窗口挡住了扫码过程；请移动取景框再扫一次。",
    "user_keys_active": "正在按键或拖动鼠标，已停止本次识别；松开后移动取景框再扫。",
    "dispatch_deadline_expired": "本次识别已超时，程序没有继续操作。",
    "total_capture_exceeds_pixel_budget": "扫描范围超过当前屏幕支持的大小，本次没有继续。",
}


@dataclass(frozen=True, slots=True)
class TargetInspection:
    count: int
    restriction: str | None = None
    login_confirmation: bool = False


def inspect_qr_target(frame: FrozenFrame, *, deadline_ns: int | None = None) -> TargetInspection:
    """Count physical instances through the shared bounded scale-aware reader.

    Zero/one is not uniqueness proof: native WeChat symbols need not be readable
    by ZXing. The independently bound single-target request is still required.
    Scale/tile repetitions of one position merge inside the reader; identical
    payloads at different positions remain separate and block native dispatch.
    Decoding has no job-wide wall-clock deadline.
    """
    with Image.frombytes("RGB", (frame.width, frame.height), frame.rgb8) as image:
        found = read_qr_instances(image, deadline_ns=deadline_ns)
        limited = any(classify_payload(item.text).kind == "wechat_payment" for item in found)
        login_confirmation = len(found) == 1 and is_wechat_login_confirm(found[0].text)
        return TargetInspection(len(found), "native_payment_requires_camera_scan" if limited else None,
                                login_confirmation)


def count_qr_symbols(frame: FrozenFrame) -> int:
    """Count instances without merging repeated payloads or retaining text."""
    return inspect_qr_target(frame).count


@dataclass(frozen=True, slots=True)
class NativeJobResult:
    frame_id: str
    generation: int
    stage_id: str
    stage_epoch: int
    status: str
    reason: str
    dispatch_attempted: bool = False
    dispatch_count: int = 0
    handoff_elapsed_ns: int | None = None
    cleanup_status: str = "NOT_NEEDED"
    observation: NativeResultObservation | None = None
    diagnostics: tuple[tuple[str, str | None], ...] = ()
    business_success: bool = False
    retry_safe_before_input: bool = False


def describe_job_result(result: NativeJobResult) -> tuple[str, bool]:
    """Plain wording; a dispatched click or a new window never proves success."""
    details = {
        "CANCELLED": "画面已变化，请重新对准二维码。",
        "TARGET_NOT_CONFIRMED": "请把一个二维码完整放进框内。",
        "MULTIPLE_CODES": "框里有多个二维码，请只框住一个。",
        "TIMED_OUT": "微信没有正常响应，请检查微信后重试。",
        "OUTCOME_UNKNOWN": "请先查看微信，避免重复操作。",
        "CONTENT_UNPROVEN": "微信识别没有完成。",
        "UNSUPPORTED_BUSINESS": "这类支付码要求摄像头扫码，当前图片方式不支持。",
        "FRAME_TOO_LARGE": "请缩小取景框，让二维码完整放在屏幕内。",
    }
    warning = result.status != "DISPATCHED_UNCONFIRMED"
    if result.status == "DISPATCHED_UNCONFIRMED":
        text = "已交给微信，页面尚未确认，请查看识别结果。"
    else:
        detail = ("这个登录码仅支持微信摄像头扫码，当前方式不支持。"
                  if result.status == "UNSUPPORTED_BUSINESS" and result.reason == "login_requires_camera_scan"
                  else details.get(result.status, "请检查微信是否已登录，再重新对准二维码。"))
        text = "无法识别：" + detail
    if result.cleanup_status in ("UNKNOWN", "FAILED", "UNBOUND_OUTCOME_UNKNOWN"):
        text += " 若仍显示截图框，请按 Esc 退出。"
        warning = True
    return text, warning


class _DeadlinePort:
    """Record operations, optionally clamping legacy bounded callers only."""
    def __init__(self, port, deadline_ns: int | None, clock: Callable[[], int]):
        self.port, self.deadline_ns, self.clock = port, deadline_ns, clock
        self.commit_finished_ns: int | None = None
        self.last_operation: str | None = None
        self._last_ns = -1

    def _check(self, deadline_ns: int | None = None) -> None:
        now = self.clock()
        if type(now) is not int or now < self._last_ns:
            raise ValueError("native_job_clock_invalid")
        self._last_ns = now
        if ((self.deadline_ns is not None and now >= self.deadline_ns)
                or (deadline_ns is not None and now >= deadline_ns)):
            raise TimeoutError("native_job_input_deadline_expired")

    def _call(self, name, args, deadline_ns):
        self.last_operation = name
        self._check()
        effective = (self.deadline_ns if deadline_ns is None else
                     min(deadline_ns, self.deadline_ns) if self.deadline_ns is not None else deadline_ns)
        self._check(effective)
        result = getattr(self.port, name)(*args, effective)
        self._check(effective)
        return result

    def prepare_request(self, request_id):
        self.last_operation = "result_observer_setup"
        self._check()
        prepare = getattr(self.port, "prepare_request", None)
        if prepare is not None:
            prepare(request_id)
        self._check()

    def verify_stage(self, frame, deadline_ns):
        return self._call("verify_stage", (frame,), deadline_ns)

    def stage_is_current(self, frame, verification):
        self._check()
        result = self.port.stage_is_current(frame, verification)
        self._check()
        return result

    def absence_receipt(self, request_id, deadline_ns):
        return self._call("absence_receipt", (request_id,), deadline_ns)

    def trigger_once(self, request_id, trigger_id, deadline_ns):
        return self._call("trigger_once", (request_id, trigger_id), deadline_ns)

    def native_snapshot(self, request_id, deadline_ns):
        return self._call("native_snapshot", (request_id,), deadline_ns)

    def select_once(self, instance, bbox, request_id, deadline_ns):
        return self._call("select_once", (instance, bbox, request_id), deadline_ns)

    def selection_evidence(self, frame, stage, instance, request_id, deadline_ns):
        return self._call("selection_evidence", (frame, stage, instance, request_id), deadline_ns)

    def observe_toolbar(self, extent, *, observation_id, deadline_ns):
        self.last_operation = "observe_toolbar"
        self._check()
        effective = (self.deadline_ns if deadline_ns is None else
                     min(deadline_ns, self.deadline_ns) if self.deadline_ns is not None else deadline_ns)
        self._check(effective)
        result = self.port.observe_toolbar(extent, observation_id=observation_id,
            deadline_ns=effective)
        self._check(effective)
        return result

    def commit_once(self, instance, permit, deadline_ns):
        try:
            return self._call("commit_once", (instance, permit), deadline_ns)
        finally:
            self.commit_finished_ns = self.clock()

    def cleanup_once(self, instance, cleanup_deadline_ns):
        return self.port.cleanup_once(instance, cleanup_deadline_ns)


class NativeJob:
    """Run once on a worker; cancel on the UI thread before any source mutation."""
    def __init__(self, frame: FrozenFrame, mapping: FrozenStageMapping, *,
                 entered_ns: int, single_target_confirmed: bool,
                 clock: Callable[[], int] = time.monotonic_ns,
                 counter: Callable[[FrozenFrame], int | TargetInspection] = inspect_qr_target,
                 port_factory=WindowsWeChatPort, controller_factory=HandoffController,
                 observer_factory=NativeResultObserver, profile: NativeProfile = DEFAULT_PROFILE,
                 display_guard: Callable[[],bool] | None = None):
        if (not isinstance(frame, FrozenFrame) or not isinstance(mapping, FrozenStageMapping)
                or type(entered_ns) is not int or entered_ns <= 0
                or type(single_target_confirmed) is not bool):
            raise ValueError("typed_frame_mapping_and_explicit_user_entry_required")
        self.frame_id, self.generation = frame.frame_id, frame.generation
        self.stage_id, self.stage_epoch = mapping.stage_id, mapping.stage_epoch
        self.entered_ns = entered_ns
        if not isinstance(profile,NativeProfile):
            raise TypeError('typed_native_profile_required')
        self._profile=profile
        if display_guard is not None and not callable(display_guard):
            raise TypeError('display_guard_must_be_callable')
        self._display_guard=display_guard
        # 入口时间仅用于关联。计算和准备后，控制器才建立不可续期的输入期限。
        self.input_deadline_ns = None
        self.result_deadline_ns = None
        self._frame, self._mapping = frame, mapping
        self._confirmed, self._clock, self._counter = single_target_confirmed, clock, counter
        self._port_factory, self._controller_factory = port_factory, controller_factory
        self._observer_factory = observer_factory
        self._lock = threading.Lock()
        self._live, self._observe_stop, self.done = threading.Event(), threading.Event(), threading.Event()
        self._live.set()
        self._started = self._cancel_requested = False
        self._controller = None
        self._result: NativeJobResult | None = None

    def source_is_current(self) -> bool:
        """只读 Event 和线程安全的屏幕代次；不调用 Tk 或窗口 RPC。"""
        if not self._live.is_set(): return False
        try: return self._display_guard is None or self._display_guard() is True
        except Exception: return False

    @property
    def result(self) -> NativeJobResult | None:
        with self._lock:
            return self._result

    def cancel(self, *, stop_observation: bool = False) -> str:
        """Invalidate input; only explicit exit also stops a known dispatch's observer.

        The controller may already have retired its active request before the
        worker receives its DispatchResult. NO_ACTIVE_REQUEST therefore defers
        the read-only decision to that actual result, never authorizes input.
        """
        if type(stop_observation) is not bool:
            raise TypeError("stop_observation_must_be_bool")
        # Publish intent before a worker can publish its controller. Never hold
        # this lock while cancel waits for the controller's dispatch edge.
        with self._lock:
            self._cancel_requested = True
            controller = self._controller
        if stop_observation:
            self._observe_stop.set()
        outcome = "CANCELLATION_UNCERTAIN"
        try:
            outcome = controller.cancel() if controller is not None else "CANCELLED_BEFORE_DISPATCH"
            return outcome
        except Exception:
            return "CANCELLATION_UNCERTAIN"
        finally:
            # This order is deliberate: cancel controller, invalidate pure lease,
            # then the calling UI may mutate generation/layout/widgets.
            self._live.clear()
            if outcome not in ("TOO_LATE", "NO_ACTIVE_REQUEST"):
                self._observe_stop.set()

    def _may_observe_result(self, dispatch: DispatchResult) -> bool:
        if self._observe_stop.is_set():
            return False
        with self._lock:
            # Source invalidation cannot revive an uncertain dispatch. A normal
            # completed dispatch is the only post-cancel read-only continuation.
            return not self._cancel_requested or dispatch.status == "DISPATCHED_UNCONFIRMED"

    def run(self) -> None:
        with profile_scope(self._profile):
            self._run_bound()

    def _run_bound(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        dispatch = observation = port = capped = None
        controller_started = False
        status, reason = "FAILED", "native_job_failed"
        phase = "target_decode"
        try:
            if not self.source_is_current():
                status, reason = "CANCELLED", "cancelled_before_native_job"
            elif not self._confirmed:
                status, reason = "TARGET_NOT_CONFIRMED", "single_target_confirmation_required"
            elif planned_capture_pixels(self._frame) > current_profile().width*current_profile().height:
                status, reason = "FRAME_TOO_LARGE", "total_capture_exceeds_pixel_budget"
            else:
                now = self._clock()
                if type(now) is not int or now < self.entered_ns:
                    raise ValueError("native_job_clock_invalid")
                inspection = (inspect_qr_target(self._frame)
                              if self._counter is inspect_qr_target else self._counter(self._frame))
                count = inspection.count if isinstance(inspection, TargetInspection) else inspection
                restriction = inspection.restriction if isinstance(inspection, TargetInspection) else None
                if not self.source_is_current():
                    status, reason = "CANCELLED", "cancelled_during_target_check"
                elif type(count) is not int or count < 0:
                    raise ValueError("target_counter_invalid")
                elif count > 1:
                    status, reason = "MULTIPLE_CODES", "reframe_one_target_and_freeze_again"
                elif restriction == "native_payment_requires_camera_scan":
                    status, reason = "UNSUPPORTED_BUSINESS", restriction
                elif restriction is not None:
                    raise ValueError("target_restriction_invalid")
                else:
                    phase = "result_observer_setup"
                    observer = self._observer_factory(clock=self._clock,
                        inspect_login_notice=isinstance(inspection, TargetInspection) and inspection.login_confirmation,
                        max_observation_ns=RESULT_BUDGET_NS)
                    phase = "native_adapter_setup"
                    port = self._port_factory(self._frame, self._mapping, self.source_is_current,
                        max_evidence_age_ns=EVIDENCE_AGE_NS, clock=self._clock,
                        result_observer=observer, result_deadline_ns=self.result_deadline_ns)
                    capped = _DeadlinePort(port, self.input_deadline_ns, self._clock)
                    phase = "controller_setup"
                    controller = self._controller_factory(capped,
                        policy=Policy(INPUT_BUDGET_NS, EVIDENCE_AGE_NS, 200_000_000, 100_000_000,
                                      selection_evidence_mode=NATIVE_DISPLAY_PROFILE_A),
                        cleanup_budget_ns=500_000_000, clock=self._clock)
                    with self._lock:
                        cancelled = self._cancel_requested
                        if not cancelled:
                            self._controller = controller
                    if cancelled or not self.source_is_current():
                        status, reason = "CANCELLED", "cancelled_before_controller_start"
                    else:
                        controller_started = True
                        phase = "native_flow"
                        dispatch = controller.run_and_dispatch(self._frame)
                        status, reason = dispatch.status, dispatch.reason
                        if dispatch.dispatch_attempted and port.commit_permit is not None:
                            phase = "result_observation"
                            settled_samples, last_progress = 0, None
                            while self._may_observe_result(dispatch):
                                observation = port.observe_result()
                                if observation.status == "CAMERA_REQUIRED":
                                    status, reason = "UNSUPPORTED_BUSINESS", "login_requires_camera_scan"
                                    break
                                # Window creation can move foreground during
                                # the query itself. Resample only this known
                                # transient under the ORIGINAL read-only
                                # result deadline; never renew input authority.
                                transient_foreground = (observation.status == "STALE"
                                    and observation.reason in ("foreground_changed_during_result_query",
                                                               "login_notice_window_changed_during_read"))
                                if observation.status != "NOT_READY" and not transient_foreground:
                                    break
                                # A finite series of complete unchanged metadata
                                # observations is an honest terminal limitation,
                                # not a claim that QR content/page recognition
                                # succeeded. Real progress restarts this counter.
                                progress = (observation.status, observation.reason,
                                            observation.progress_sha256)
                                settled_samples = settled_samples+1 if progress == last_progress else 1
                                last_progress = progress
                                if settled_samples >= MAX_SETTLED_RESULT_SAMPLES:
                                    observation = replace(observation,
                                        reason="result_unconfirmed_after_complete_settled_samples")
                                    break
                                # A newly created H5 host can remain blank while
                                # loading. Space unchanged text observations
                                # farther apart rather than letting 32 rapid
                                # empty samples become a short whole-flow timer.
                                notice_wait = observation.reason == "login_notice_not_confirmed"
                                delay = (min(2.0, 0.1 * 2**min(settled_samples // 3, 5))
                                         if notice_wait else 0.1)
                                self._observe_stop.wait(delay)
        except TimeoutError:
            status = "OUTCOME_UNKNOWN" if controller_started else "TIMED_OUT"
            reason = "native_operation_watchdog_expired"
        except BaseException:
            # Never print exception text, traceback, user path or image content.
            if dispatch is not None:
                status, reason = dispatch.status, "result_observation_failed"
            elif controller_started:
                status, reason = "OUTCOME_UNKNOWN", "native_controller_interrupted_no_retry"
            else:
                status, reason = "FAILED", "native_job_failed"
        finally:
            # Result observation can outlast the screenshot tool transition.
            # Recheck absence only after a correlated result; never send input.
            if (port is not None and dispatch is not None
                    and dispatch.cleanup_status == "UNKNOWN"
                    and observation is not None
                    and observation.status in ("CAMERA_REQUIRED", "NEW_WINDOW_OBSERVED")):
                try:
                    if port.confirm_cleanup_absence() == "CONFIRMED_ABSENT":
                        dispatch = replace(dispatch, cleanup_status="CONFIRMED_ABSENT")
                except Exception:
                    pass  # Preserve unknown; no optimistic fallback.
            elapsed = None
            if capped is not None and type(capped.commit_finished_ns) is int and capped.commit_finished_ns >= self.entered_ns:
                elapsed = capped.commit_finished_ns-self.entered_ns
            diagnostics = (("job_phase", phase), ("last_operation", capped.last_operation if capped is not None else None))
            if port is not None:
                diagnostics += tuple((name, getattr(port, name, None)) for name in (
                    "last_stage_error_code", "last_native_error_code", "last_selection_error_code", "last_selection_boundary_reason", "last_input_error_code",
                    "last_toolbar_reason_code", "last_cleanup_error_code", "last_cleanup_probe_code"))
            result = NativeJobResult(self.frame_id, self.generation, self.stage_id, self.stage_epoch,
                status, reason, bool(dispatch and dispatch.dispatch_attempted),
                dispatch.dispatch_count if dispatch else 0, elapsed,
                dispatch.cleanup_status if dispatch else ("UNKNOWN" if controller_started else "NOT_NEEDED"),
                observation, diagnostics, retry_safe_before_input=(
                    not controller_started or (dispatch is not None
                        and not dispatch.trigger_attempted and not dispatch.select_attempted
                        and not dispatch.dispatch_attempted and dispatch.dispatch_count == 0
                        and dispatch.cleanup_status == "NOT_NEEDED")))
            with self._lock:
                self._result = result
                self._controller = self._frame = self._mapping = None
            self._live.clear()
            self.done.set()  # Completion cannot be lost when a UI queue is full.
