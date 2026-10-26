"""The local Windows viewfinder and result panel for ScreenQR."""

from __future__ import annotations

import ctypes
from concurrent.futures import CancelledError
import hashlib
import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox
from uuid import uuid4

from PIL import Image, ImageTk
from . import __version__

from .capture import (
    capture_region,
    capture_region_with_metadata,
    enable_dpi_awareness,
    get_virtual_bounds,
)
from .core import ScanResult, browser_url_for_result, classify_payload, decode_image
from .image_io import load_image_for_decode
from .decoding import QRInstance, read_qr_instances
from .confirmation import Candidate, build_candidate, build_selected_candidate, matches
from .history import HistoryStore
from .panels import HistoryPanel, position_dialog
from .presentation import result_line, kind_name
from .profiles import DEFAULT_PROFILE, NativeProfile, load_profiles, profile_scope
from .environment import (DesktopState, DesktopChanged, DisplaySignals, probe_desktop,
                          assess_environment, capture_checked)
from .frozen_frame import FrozenFrame
from .native_job import NativeJob, describe_job_result
from .stage import (
    FrozenStageMapping, StagePixelVerification,
    LIVE_NATIVE_GUARD_PHYSICAL_PX, LIVE_NATIVE_WHITE_PADDING_PHYSICAL_PX,
    describe_frozen_stage as describe_stage_native,
    disable_own_stage_transitions, stage_mapping_is_current, verify_frozen_stage,
    focus_own_stage,
)


INK = "#182B3A"
PAPER = "#F3F7FA"
BLUE = "#176BDA"
MUTED = "#60788B"
CYAN = "#38A9BB"
WHITE = "#FFFFFF"
RULE = "#CCD9E2"
KEY = "#FF00FF"
BODY = ("Microsoft YaHei UI", 10)
SMALL = ("Microsoft YaHei UI", 9)
DISPLAY = ("Segoe UI Semibold", 21)
MONO = ("Consolas", 10)


@dataclass(frozen=True)
class _FreezeRequest:
    generation: int
    request_id: str
    bbox: tuple[int, int, int, int]
    dpi_reference_hwnds: tuple[int, ...]
    requested_monotonic: float
    deadline_monotonic: float | None
    native_entered_ns: int | None = None
    manual_scan: bool = False
    expected_wechat_digest: bytes | None = None


@dataclass(frozen=True)
class _StageVerifyRequest:
    frame: FrozenFrame
    mapping: FrozenStageMapping
    request_id: str
    deadline_monotonic_ns: int


@dataclass(frozen=True)
class _Job:
    generation: int
    bbox: tuple[int, int, int, int] | None = None
    path: str | None = None
    freeze: _FreezeRequest | None = None
    stage_verify: _StageVerifyRequest | None = None
    confirm: Candidate | None = None
    desktop: DesktopState | None = None
    display_epoch: int | None = None
    own_hwnd: int | None = None
    profile: NativeProfile = DEFAULT_PROFILE
    selected: QRInstance | None = None


class _StageDoesNotFit(ValueError):
    """The immutable frame and required white border cannot fit this screen."""


def _position(window: tk.Toplevel | tk.Tk, x: int, y: int, width: int, height: int) -> None:
    """Use physical absolute coordinates, including monitors left of primary."""
    window.geometry(f"{width}x{height}")
    window.update_idletasks()
    if os.name == "nt":
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        hwnd = user32.GetAncestor(window.winfo_id(), 2) or window.winfo_id()
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        rect, point = wintypes.RECT(), wintypes.POINT(0, 0)
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        user32.ClientToScreen(hwnd, ctypes.byref(point))
        outer_x, outer_y = x - (point.x - rect.left), y - (point.y - rect.top)
        # Tk geometry sizes the client; only move the native frame here.
        if user32.SetWindowPos(hwnd, None, outer_x, outer_y, 0, 0, 0x0015):
            return
    # Windows is the supported platform; this fallback is also useful to contributors.
    window.geometry(f"{width}x{height}{x:+d}{y:+d}")


class Viewfinder(tk.Toplevel):
    HEADER = 32
    FOOTER = 232
    INSET = 12

    def __init__(self, app: "ScreenQRApp") -> None:
        super().__init__(app.root)
        self.app = app
        self.scale = app.scale
        self.HEADER = self.px(32)
        self.FOOTER = self.px(232)
        self.INSET = self.px(12)
        self.title(f"ScreenQR {__version__}")
        # Keep a normal top-level frame for native resize, task switching and
        # accessibility. The client area still has a transparent optical center.
        self.minsize(self.px(280), self.px(420))
        self.protocol("WM_DELETE_WINDOW", self.app.close)
        self.attributes("-topmost", True)
        self.configure(bg=KEY)
        if os.name == "nt":
            self.attributes("-transparentcolor", KEY)
        self.canvas = tk.Canvas(self, bg=KEY, highlightthickness=0, bd=0, cursor="arrow")
        self.canvas.pack(fill="both", expand=True)
        self._width, self._height = self.px(420), self.px(620)
        self._drag: tuple[int, int, int, int] | None = None
        self._resize: tuple[int, int, int, int] | None = None
        self._gesture_bbox: tuple[int, int, int, int] | None = None
        self._last_geometry: tuple[int, ...] | None = None
        self._visible = True
        self.canvas.bind("<Configure>", self._draw)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._hover)
        self.bind("<Escape>", lambda _e: self.app.close())
        self.bind("<space>", self.app._space)
        self.bind("<Control-o>", lambda _e: self.app.choose_image())
        self.bind("<Control-c>", lambda _e: self.app.copy_selected())
        self.bind("<F1>", lambda _e: self.app.show_about())
        self.canvas.bind("<Button-3>", self.app.show_help_menu)
        self.bind("<Control-Shift-F>", lambda _e: self.app.request_wechat_scan())
        self.bind("<Control-Shift-f>", lambda _e: self.app.request_wechat_scan())
        self.bind("<Configure>", self._region_changed)
        self.update_idletasks()

    def _region_changed(self, event: tk.Event) -> None:
        if event.widget is self:
            geometry = (self.winfo_rootx(), self.winfo_rooty(), self.winfo_width(), self.winfo_height())
            if geometry == self._last_geometry:
                return  # Stacking-only Configure is not a new source region.
            self._last_geometry = geometry
            if getattr(self.app, '_handoff_idle', False):
                self.app._resume_after_handoff()
            self.app._invalidate_source("取景框已移动或调整大小，之前的画面已清除。")

    def px(self, value: int | float) -> int:
        return round(value * self.scale)

    def place_initially(self) -> None:
        self.app.root.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self._width = min(self._width, sw - 32)
        self._height = min(self._height, sh - 80)
        _position(self, max(8, (sw-self._width)//2), max(8, (sh-self._height)//2-30), self._width, self._height)
        self._draw()

    def _draw(self, _event: tk.Event | None = None) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if w < 100 or h < 100: return
        self._width, self._height = w, h
        p, c = self.px, self.canvas
        c.delete("all")
        c.create_rectangle(0, 0, w, self.HEADER, fill=INK, outline=INK)
        c.create_text(p(14), p(16), text="ScreenQR · 拖动这里取景", fill=WHITE,
                      font=("Microsoft YaHei UI", 10), anchor="w")
        top, bottom, left, right = self.HEADER+p(6), h-self.FOOTER-p(6), p(6), w-p(6)
        c.create_rectangle(left, top, right, bottom, outline=CYAN, width=max(1,p(1)))
        size=p(22)
        for x,y,dx,dy in ((left,top,1,1),(right,top,-1,1),(left,bottom,1,-1),(right,bottom,-1,-1)):
            c.create_line(x+dx*size,y,x,y,x,y+dy*size,fill=CYAN,width=p(4))
        if hasattr(self.app, '_footer'):
            self.app._layout_panel()

    def _press(self, event: tk.Event) -> str:
        if self.app._closed or self.app._closing: return "break"
        w,h=self.winfo_width(),self.winfo_height()
        if event.y < self.HEADER:
            self.app._resume_after_handoff()
            if self.app._file_source is not None or self.app.paused.get():
                self.app._file_source=None
                self.app.paused.set(False)
            self._gesture_bbox=self.capture_bbox(clip_to_desktop=False)
            self.app._invalidate_source()
            self._drag=(event.x_root,event.y_root,self.winfo_rootx(),self.winfo_rooty())
        elif event.y > h-self.px(22) and event.x > w-self.px(25):
            self.app._resume_after_handoff()
            self._gesture_bbox=self.capture_bbox(clip_to_desktop=False)
            self.app._invalidate_source()
            self._resize=(event.x_root,event.y_root,w,h)
        return "break"

    def _motion(self, event: tk.Event) -> str:
        if self.app._closed or self.app._closing:
            return "break"
        if self._drag:
            sx, sy, wx, wy = self._drag
            x, y = wx + event.x_root - sx, wy + event.y_root - sy
            vx, vy, vw, vh = get_virtual_bounds()
            x = min(max(x, vx - self._width + self.px(90)), vx + vw - self.px(90))
            y = min(max(y, vy), vy + vh - self.HEADER)
            _position(self, x, y, self._width, self._height)
        elif self._resize:
            sx, sy, sw, sh = self._resize
            self._set_size(sw + event.x_root - sx, sh + event.y_root - sy)
        return "break"

    def _begin_resize(self,event):
        if self.app._closed or self.app._closing: return 'break'
        self.app._resume_after_handoff()
        self._gesture_bbox=self.capture_bbox(clip_to_desktop=False)
        self.app._invalidate_source()
        self._resize=(event.x_root,event.y_root,self.winfo_width(),self.winfo_height())
        return 'break'

    def _release(self, _event: tk.Event) -> None:
        entered_ns = time.monotonic_ns()
        initial = self._gesture_bbox
        changed_gesture = self._drag is not None or self._resize is not None
        self._drag = None
        self._resize = None
        self._gesture_bbox = None
        if changed_gesture and initial is not None:
            current = self.capture_bbox(clip_to_desktop=False)
            if current != initial:
                self.app._viewfinder_released(entered_ns, current)

    def _hover(self, event: tk.Event) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if event.y < self.HEADER and event.x < w - self.px(44):
            cursor = "fleur"
        elif event.x > w - self.px(25) and event.y > h - self.px(22):
            cursor = "size_nw_se"
        elif event.y > h - self.FOOTER or event.y < self.HEADER:
            cursor = "hand2"
        else:
            cursor = "arrow"
        self.canvas.configure(cursor=cursor)

    def _set_size(self, width: int, height: int) -> None:
        self.app._invalidate_source()
        _, _, vw, vh = get_virtual_bounds()
        width, height = max(self.px(280), min(width, vw)), max(self.px(420), min(height, vh))
        _position(self, self.winfo_rootx(), self.winfo_rooty(), width, height)

    def resize_by(self, delta: int) -> None:
        self._set_size(self.winfo_width() + self.px(delta), self.winfo_height() + self.px(delta))

    def capture_bbox(self, *, clip_to_desktop: bool = True) -> tuple[int, int, int, int]:
        x, y = self.winfo_rootx(), self.winfo_rooty()
        bbox = (x + self.INSET, y + self.HEADER + self.INSET,
                x + self.winfo_width() - self.INSET,
                y + self.winfo_height() - self.FOOTER - self.INSET)
        if not clip_to_desktop:
            return bbox
        vx, vy, vw, vh = get_virtual_bounds()
        return (max(bbox[0], vx), max(bbox[1], vy), min(bbox[2], vx + vw), min(bbox[3], vy + vh))


class ScreenQRApp:
    def __init__(self, root: tk.Tk, *, diagnostics=None, measurement=None) -> None:
        self.root = root
        self._diagnostics = diagnostics
        self._measurement = measurement
        self.root.title(f"ScreenQR {__version__} · 屏幕扫码")
        icon = Path(__file__).resolve().parent.parent / "assets" / "screenqr.ico"
        if icon.exists():
            self.root.iconbitmap(default=str(icon))
        self.root.configure(bg=PAPER)
        self.scale = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.root.minsize(1, 1)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.paused = tk.BooleanVar(value=False)
        self.auto_open = tk.BooleanVar(value=False)
        self.wechat_on_release = tk.BooleanVar(value=True)
        self._auto_internal_pause = False
        self._handoff_idle = False
        self._auto_handoff_request: _FreezeRequest | None = None
        self._auto_handoff_callback: str | None = None
        self._auto_stage_settling = False
        self._auto_stage_signature: tuple | None = None
        self._auto_seen_frames: set[tuple[int, int, str]] = set()
        self._wechat_seen_payloads: set[bytes] = set()
        self._wechat_outcomes: dict[bytes, tuple[str, bool]] = {}
        self._wechat_active_digest: bytes | None = None
        self._native_outcome_digest: bytes | None = None
        self._native_source_generation: int | None = None
        self._wechat_candidate: bytes | None = None
        self._wechat_candidate_count = 0
        self._native_requires_gesture = False
        self._finder_hidden_for_native = False
        self._native_stage_focus_ready = False
        self._native_auto_frame_id: str | None = None
        self._native_auto_frame_key: tuple[int, int, str] | None = None
        self._stage_geometry: dict[tk.Misc, tuple[int, ...]] = {}
        self._closed = False
        self._closing = False
        self._ui_thread_id = threading.get_ident()
        self._generation = 0
        self._busy = False
        self._pending_image: str | None = None
        self._pending_freeze: _FreezeRequest | None = None
        self._active_freeze: _FreezeRequest | None = None
        self._frozen_frame: FrozenFrame | None = None
        self._freeze_preview: tk.Toplevel | None = None
        self._freeze_photo: ImageTk.PhotoImage | None = None
        self._freeze_image_label: tk.Label | None = None
        self._freeze_white_stage: tk.Frame | None = None
        self._freeze_stage_id: str | None = None
        self._freeze_stage_epoch = 0
        self._pending_stage_verify: _StageVerifyRequest | None = None
        self._active_stage_verify_id: str | None = None
        self._stage_verification: StagePixelVerification | None = None
        self._stage_verification_error: str | None = None
        self._native_job: NativeJob | None = None
        self._native_thread: threading.Thread | None = None
        self._native_spent_frame_id: str | None = None
        self._native_last_result = None
        self._native_button: tk.Button | None = None
        self._next_scan = 0.0
        self._results: list[ScanResult] = []
        self._instances: tuple[QRInstance, ...] = ()
        self._selected_instance: QRInstance | None = None
        self._choice_token = None
        self._history = HistoryStore()
        self._history_panel = None
        self._result_key: tuple[tuple[str, str], ...] = ()
        self._auto_seen: set[bytes] = set()
        self._automatic_candidate: str | None = None
        self._candidate_count = 0
        self._opening = False
        self._candidate: Candidate | None = None
        self._pending_confirm: Candidate | None = None
        self._confirm_inflight: Candidate | None = None
        self._browser_grant: Candidate | None = None
        self._preview_photo = None
        self._last_opened_candidate: Candidate | None = None
        self._file_source: str | None = None
        self._payload_text = ""
        self._readiness_misses = 0
        self._jobs: queue.Queue[_Job | None] = queue.Queue(maxsize=1)
        self._events: queue.Queue[tuple] = queue.Queue(maxsize=3)
        self._stop = threading.Event()
        self._event_lock = threading.Lock()
        self._dropped_event_kinds: set[str] = set()
        self._worker = threading.Thread(target=self._work, name="ScreenQR-decoder", daemon=True)
        self._profiles=load_profiles()
        self._desktop_state=None
        self._environment=None
        self._native_profile=DEFAULT_PROFILE
        self._display_signals=None
        self._display_epoch=0
        self._own_hwnd=None
        self._next_environment_check=0.0
        self._next_version_check=0.0
        self._about=None
        root.withdraw()
        self.finder = Viewfinder(self)
        self._build_panel()
        self.finder.place_initially()
        root.update_idletasks()
        try:
            self._own_hwnd=self._own_top_hwnd(self.finder)
            self._display_signals=DisplaySignals(self._own_hwnd)
            self._display_epoch=self._display_signals.epoch
        except (OSError,ValueError,RuntimeError,AttributeError):
            self._display_signals=None
        self._watch_environment(force=True)
        # The capture bounds are entirely inside the transparent center, so
        # neither header nor border is captured. Keeping windows visible to
        # capture also allows normal screen sharing and assistive tools.
        self._worker.start()
        self.root.bind("<Control-o>", lambda _e: self.choose_image())
        self.root.bind("<Control-Shift-F>", lambda _e: self.request_wechat_scan())
        self.root.bind("<Control-Shift-f>", lambda _e: self.request_wechat_scan())
        self.root.bind("<Escape>", lambda _e: self.hide_viewfinder())
        self.root.bind("<space>", self._space)
        self.root.after_idle(self.finder.focus_set)
        self.root.after(60, self._tick)

    def _label(self, parent: tk.Misc, text: str, **kwargs) -> tk.Label:
        options = {"bg": PAPER, "fg": INK, "font": BODY, "anchor": "w"}
        options.update(kwargs)
        return tk.Label(parent, text=text, **options)

    def _button(self, parent: tk.Misc, text: str, command, primary: bool = False, **kwargs) -> tk.Button:
        options = dict(text=text, command=command, font=BODY, bg=BLUE if primary else WHITE,
                       fg=WHITE if primary else INK, activebackground="#1257B1" if primary else "#E5EEF5",
                       activeforeground=WHITE if primary else INK, relief="flat", bd=0,
                       padx=15, pady=9, cursor="hand2", highlightthickness=1,
                       highlightbackground=BLUE if primary else RULE, highlightcolor=CYAN,
                       disabledforeground="#95A6B4", takefocus=True)
        options.update(kwargs)
        return tk.Button(parent, **options)

    def _build_panel(self) -> None:
        self._footer = tk.Frame(self.finder, bg=PAPER, bd=0)
        self.status = self._label(self._footer, "把完整二维码放进框内", fg=MUTED, font=SMALL)
        self._preview_label = tk.Label(self._footer, bg=PAPER, bd=0)
        self._target_label = tk.Text(self._footer,wrap='word',font=SMALL,bg=PAPER,fg=INK,
            relief='flat',bd=0,highlightthickness=0,padx=0,pady=0,state='disabled',height=3)
        self.confirm_button = self._button(self._footer, "在浏览器打开", self.open_selected, primary=True, padx=10, pady=5)
        self.copy_button = self._button(self._footer, "复制", self.copy_selected, padx=10, pady=5)
        self._history_button = self._button(self.finder, "历史", self.show_history, padx=8, pady=2)
        self._multi_list = tk.Listbox(self._footer,exportselection=False,font=SMALL,bg=WHITE,fg=INK,
            relief='flat',bd=0,highlightthickness=1,selectmode='browse',activestyle='dotbox')
        self._multi_scroll = tk.Scrollbar(self._footer,command=self._multi_list.yview)
        self._multi_list.configure(yscrollcommand=self._multi_scroll.set)
        self._multi_list.bind('<<ListboxSelect>>',self._choose_instance)
        self._instruction = self._label(self._footer,'拖动上栏，把完整的码放进框里。\n网址看清后点打开；微信内容会交给微信。F1 查看帮助。',font=SMALL,fg=MUTED,justify='left')
        self._compatibility_hint=self._label(self._footer,"",fg=MUTED,font=SMALL)
        # 底栏覆盖了 Canvas 原有缩放角；在最上层放置同一个缩放入口。
        self._resize_grip=tk.Label(self._footer,text='⋰',bg=PAPER,fg=MUTED,font=('Segoe UI',12),cursor='size_nw_se')
        self._resize_grip.bind('<ButtonPress-1>',self.finder._begin_resize)
        self._resize_grip.bind('<B1-Motion>',self.finder._motion)
        self._resize_grip.bind('<ButtonRelease-1>',self.finder._release)
        self._layout_panel()

    def _layout_panel(self) -> None:
        p=self.finder.px
        self._footer.place(x=0,rely=1.0,anchor="sw",relwidth=1.0,height=self.finder.FOOTER)
        self.status.place(x=p(10),y=p(5),width=max(1,self.finder.winfo_width()-p(20)),height=p(34))
        self.status.configure(wraplength=max(1,self.finder.winfo_width()-p(20)))
        self._preview_label.place(x=p(10),y=p(42),width=p(64),height=p(64))
        target_left=82 if getattr(self,'_preview_photo',None) is not None else 10
        self._target_label.place(x=p(target_left),y=p(37),width=max(1,self.finder.winfo_width()-p(target_left+12)),height=p(64))
        if self._candidate is not None:
            self.confirm_button.place(x=p(10),y=p(107),width=p(156),height=p(32))
        else:
            self.confirm_button.place_forget()
        self.copy_button.place(relx=1.0,x=-p(12),y=p(107),anchor='ne',width=p(70),height=p(32))
        self._history_button.place(relx=1.0,x=-p(8),y=p(3),anchor='ne',width=p(54),height=p(26))
        if len(getattr(self,'_instances',())) > 1:
            self._instruction.place_forget()
            self._multi_list.place(x=p(10),y=p(145),width=max(1,self.finder.winfo_width()-p(36)),height=p(54))
            self._multi_scroll.place(relx=1.0,x=-p(10),y=p(145),anchor='ne',width=p(16),height=p(54))
        else:
            self._multi_list.place_forget(); self._multi_scroll.place_forget()
            if not self._results:
                self._instruction.place(x=p(10),y=p(144),width=max(1,self.finder.winfo_width()-p(20)),height=p(52))
                self._instruction.configure(wraplength=max(1,self.finder.winfo_width()-p(20)))
            else:
                self._instruction.place_forget()
        hint=getattr(self,'_compatibility_hint',None)
        if hint is not None:
            hint.place(x=p(10),rely=1.0,y=-p(26),width=max(1,self.finder.winfo_width()-p(42)),height=p(24))
            hint.configure(wraplength=max(1,self.finder.winfo_width()-p(42)))
        grip=getattr(self,'_resize_grip',None)
        if grip is not None:
            grip.place(relx=1.0,rely=1.0,anchor='se',width=p(20),height=p(20))

    def _set_payload(self, value: str) -> None:
        self._payload_text=value

    def _show_target(self,value):
        if getattr(self,'_target_text_cache',None)==value: return
        self._target_text_cache=value
        self._target_label.configure(state='normal')
        self._target_label.delete('1.0','end')
        self._target_label.insert('1.0',value)
        self._target_label.configure(state='disabled')

    def _set_status(self, message: str, warning: bool = False) -> None:
        if self._closed: return
        # Private error identifiers stay inside result metadata, never this UI.
        if message.startswith("无法识别"):
            pass  # A final failure must never be rewritten as active progress.
        elif warning:
            if "多个" in message: message="框里有多个码，请只框一个"
            else: message="无法识别"
        elif any(word in message for word in ("原图", "预览", "截取", "核验", "交付", "松手", "提交")):
            message="正在识别微信内容" if self._native_job is not None or self._active_freeze is not None else "把完整二维码放进框内"
        # 普通界面只说日常用语；内部原因和严格校验仍保留原始编号。
        message=message.replace('无法识别','没扫出来').replace('已识别','扫到了').replace('正在识别','正在扫')
        message=message.replace('识别结果','扫码结果').replace('识别','扫').replace('解码','扫')
        if message.startswith('没扫出来'):
            if '显示不下' in message or '超出当前适配范围' in message:
                message='没扫出来：框太大了，请缩小一点再扫。'
            elif any(word in message for word in ('校验','核验','原生','证据','实例','预览')):
                message='没扫出来：微信这次没扫完，请重新框好再扫。'
        self.status.configure(text=message,fg=INK if warning else MUTED)

    def show_help_menu(self,event=None):
        if self._closed or self._closing: return
        menu=tk.Menu(self.finder,tearoff=False)
        menu.add_command(label='帮助 / 关于（F1）',command=self.show_about)
        if event is not None:
            try: menu.tk_popup(event.x_root,event.y_root)
            finally: menu.grab_release()

    def show_history(self):
        if self._closed or self._closing: return
        if self._history_panel is not None and self._history_panel.winfo_exists():
            self._history_panel.lift(); return
        if self._native_job is not None or self._active_freeze is not None:
            self._set_status('微信还在处理，结束后再看记录。'); return
        was_paused=self.paused.get()
        self.paused.set(True)
        self._invalidate_source()
        def resume():
            self._history_panel=None
            if not self._closed and not self._closing:
                self.paused.set(was_paused)
                self._invalidate_source()
                self._pending_image=self._file_source
                self._next_scan=0
        self._history_panel=HistoryPanel(self,resume)

    def open_history_entry(self, entry_id):
        if self._closed or self._closing or self._opening: return
        history=getattr(self,'_history',None)
        entry=history.get(entry_id) if history is not None else None
        url=browser_url_for_result(entry.result) if entry is not None else None
        if url is not None:
            self._opening=True
            self._launch_browser(url,history=True)

    def show_about(self):
        if self._closed or self._closing: return
        if self._about is not None and self._about.winfo_exists():
            self._about.lift(); return
        if self._native_job is not None or self._active_freeze is not None:
            self._set_status('微信还在处理，结束后再打开帮助。'); return
        from .help_ui import HelpWindow
        was_paused=self.paused.get()
        self.paused.set(True)
        self._invalidate_source()
        def resume():
            self._about=None
            if not self._closed and not self._closing:
                self.paused.set(was_paused)
                self._invalidate_source()
                self._pending_image=self._file_source
                self._next_scan=0
        self._about=HelpWindow(self,resume)

    def _watch_environment(self,*,force=False):
        if not hasattr(self,'_profiles') or self._own_hwnd is None: return
        now=time.monotonic()
        epoch=self._display_signals.epoch if self._display_signals is not None else self._display_epoch
        changed_signal=epoch!=self._display_epoch
        if not force and not changed_signal and now<self._next_environment_check: return
        self._next_environment_check=now+0.35
        try: state=probe_desktop(self._own_hwnd)
        except (DesktopChanged,OSError,ValueError): state=None
        changed=changed_signal or state!=self._desktop_state
        if changed and self._desktop_state is not None:
            self._invalidate_source()
            self._set_status('屏幕情况变了，请重新拖动框再扫。')
        self._display_state_changed(state,self._desktop_state)
        self._desktop_state=state; self._display_epoch=epoch
        if force or changed or now>=self._next_version_check:
            self._next_version_check=now+2.0
            self._environment=assess_environment(self._profiles,state)
            self._native_profile=self._environment.profile or DEFAULT_PROFILE
            if self._display_signals is None:
                self._compatibility_hint.configure(text='暂时不能跟随屏幕变化，普通码请保持屏幕设置不变。')
            else: self._compatibility_hint.configure(text=self._environment.message)

    def _display_state_changed(self,state,previous):
        if state is None: return
        previous_dpi=round(self.finder.scale*96)
        if state.dpi==previous_dpi: return
        # 先撤销旧来源，再按自有窗口的真实 DPI 重排；不改变 Windows 缩放设置。
        ratio=state.dpi/previous_dpi
        self.scale=state.dpi/96; self.finder.scale=self.scale
        self.root.tk.call('tk','scaling',state.dpi/72)
        self.finder.HEADER=self.finder.px(32)
        self.finder.FOOTER=self.finder.px(232)
        self.finder.INSET=self.finder.px(12)
        self.finder.minsize(self.finder.px(280),self.finder.px(420))
        work=next(m.work for m in state.monitors if m.rect==state.own_monitor)
        width=min(round(self.finder.winfo_width()*ratio),work[2]-work[0]-20)
        height=min(round(self.finder.winfo_height()*ratio),work[3]-work[1]-60)
        _position(self.finder,max(work[0],min(self.finder.winfo_rootx(),work[2]-width)),
            max(work[1],min(self.finder.winfo_rooty(),work[3]-height)),width,height)
        self.finder._draw()

    def _wechat_ready(self,*,refresh=False):
        # 没有 GUI 的旧单元夹具不承担环境探测；真实 App 必须经过此门禁。
        if not hasattr(self,'_profiles'): return True
        if refresh: self._watch_environment(force=True)
        assessment=self._environment
        ready=(assessment is not None and assessment.profile is not None and self._display_signals is not None)
        if not ready:
            self._set_status(assessment.message if assessment else '微信暂时不能扫，普通码照常能扫。')
        return ready

    def _screen_job(self,bbox,**kwargs):
        state=getattr(self,'_desktop_state',None)
        if hasattr(self,'_profiles') and (state is None or not state.permits(bbox)):
            self._clear_confirmation()
            self._set_status('请把取景框完整放进一块屏幕里；受系统保护的画面扫不了。')
            return None
        return _Job(self._generation,bbox=bbox,desktop=state,
            display_epoch=getattr(self,'_display_epoch',None),own_hwnd=getattr(self,'_own_hwnd',None),
            profile=getattr(self,'_native_profile',DEFAULT_PROFILE),selected=getattr(self,'_selected_instance',None),**kwargs)

    def _job_display_current(self,job):
        if job.desktop is None: return True
        try:
            signals=getattr(self,'_display_signals',None)
            return ((signals is None or signals.epoch==job.display_epoch)
                and probe_desktop(job.own_hwnd)==job.desktop)
        except (DesktopChanged,OSError,ValueError): return False

    def _capture_for_job(self,job):
        if job.desktop is None: return capture_region(job.bbox)
        if not self._job_display_current(job): raise CancelledError()
        return capture_checked(job.bbox,job.own_hwnd,job.desktop)

    def _space(self, event: tk.Event) -> str | None:
        if self._closed or self._closing:
            return "break"
        if isinstance(event.widget, (tk.Text, tk.Entry, tk.Button)):
            return None
        self.toggle_pause()
        return "break"

    def toggle_pause(self) -> None:
        if self._closed or self._closing:
            return
        pause = not self.paused.get()
        self._resume_after_handoff()
        self._auto_internal_pause = False
        self._invalidate_source()
        self.paused.set(pause)
        if not self.paused.get() and not self.finder._visible:
            self.show_viewfinder()
        if not self.paused.get():
            self._native_requires_gesture = False
        self.finder._draw()
        self._set_status("已暂停，识别结果仍可打开或复制。" if self.paused.get() else "正在扫描，移动取景框即可。")

    def hide_viewfinder(self) -> None:
        self.close()

    def show_viewfinder(self) -> None:
        if self._closed or self._closing: return
        self._file_source=None
        self._resume_after_handoff()
        self._invalidate_source()
        self.finder.deiconify()
        self.finder._visible=True
        self.finder.lift()
        self.paused.set(False)
        self._set_status("把完整二维码放进框内")

    def choose_image(self) -> None:
        if self._closed or self._closing:
            return
        self._resume_after_handoff()
        was_paused = self.paused.get()
        self._auto_internal_pause = False
        self._invalidate_source()
        self.paused.set(True)
        self._pending_image = None
        self.finder._draw()
        path = filedialog.askopenfilename(parent=self.finder, title="选择二维码图片", filetypes=[
            ("图片", "*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif *.tiff"), ("所有文件", "*.*")])
        if not path:
            self.paused.set(was_paused)
            self.finder._draw()
            return
        self.paused.set(True)
        self.finder._draw()
        self._invalidate_source()
        self._file_source = self._pending_image = path
        self._set_status("正在识别所选图片…")

    def _cancel_native_before_mutation(self) -> None:
        """UI only: controller cancellation precedes lease/Tk/source mutation."""
        job = getattr(self, "_native_job", None)
        if job is not None and not job.done.is_set():
            if self._closing:
                job.cancel(stop_observation=True)
            else:
                job.cancel()

    def _cancel_pending_auto_handoff(self) -> None:
        callback = getattr(self, "_auto_handoff_callback", None)
        self._auto_handoff_callback = None
        self._auto_handoff_request = None
        self._auto_stage_settling = False
        self._auto_stage_signature = None
        if callback is not None:
            self.root.after_cancel(callback)

    def _wechat_release_changed(self) -> None:
        if self._closed or self._closing:
            return
        self._auto_internal_pause = False
        self._invalidate_source()
        if self.wechat_on_release.get():
            self.paused.set(False)
            self.finder._draw()
            self._set_status("微信自动识别已开启：把一个码框好后松手，程序会选择打开方式。")
        else:
            self._set_status("微信自动识别已停止；请关闭遗留截图窗口后重新移动取景框。")

    def request_wechat_scan(self) -> None:
        """One explicit button action starts the same original 2s input budget."""
        entered_ns = time.monotonic_ns()
        if self._closed or self._closing:
            return
        self._poll_native_job()
        if self._native_job is not None or self._auto_handoff_request is not None:
            self._set_status("微信正在处理上一个码，请稍候。", warning=True)
            return
        self.request_freeze(native_entered_ns=entered_ns, native_by_button=True)

    def _viewfinder_released(self, entered_ns: int, bbox: tuple[int, int, int, int]) -> None:
        if getattr(self,'_measurement',None) is not None:
            # 计时模式仅测公开样本，不给任何微信或浏览器业务发送输入。
            self._resume_scanning()
            return
        """One deliberate changed drag/resize is the user's target selection."""
        if (self._closed or self._closing or not self.wechat_on_release.get()
                or not self.finder._visible
                or (self.paused.get() and not self._auto_internal_pause)):
            return
        self._poll_native_job()
        if not self.wechat_on_release.get() or (self.paused.get() and not self._auto_internal_pause):
            return  # Completion may have disabled automatic input as unknown.
        if self._native_job is not None:
            self._set_status("上一请求仍在收尾；本次移动不排队，请完成后重新移动。", warning=True)
            return
        self._native_requires_gesture = False
        if self.finder.capture_bbox(clip_to_desktop=False) != bbox:
            return
        self.request_freeze(native_entered_ns=entered_ns)

    def _start_auto_handoff(self, request: _FreezeRequest) -> None:
        self._auto_handoff_callback = None
        frame = self._frozen_frame
        if (self._closed or self._closing or not (request.manual_scan or self.wechat_on_release.get())
                or self._auto_handoff_request is not request or frame is None
                or frame.frame_id != request.request_id or frame.generation != request.generation
                or self._generation != request.generation or not self._auto_internal_pause
                or self.finder.capture_bbox(clip_to_desktop=False) != request.bbox):
            return
        if (frame.width, frame.height, frame.capture_sha256) in self._auto_seen_frames:
            self._finish_wechat_attempt("无法识别：这个画面已处理，请重新框选")
            return
        if len(self._auto_seen_frames) >= 512:
            self.wechat_on_release.set(False)
            self._finish_wechat_attempt("无法识别：请重新启动程序")
            return
        ready, signature = self._auto_stage_readiness(frame)
        if (self._closed or self._closing or self._auto_handoff_request is not request
                or self._generation != request.generation or self._frozen_frame is not frame
                or self.finder.capture_bbox(clip_to_desktop=False) != request.bbox):
            return
        if not ready or signature != getattr(self, "_auto_stage_signature", None):
            self._readiness_misses=getattr(self,"_readiness_misses",0)+1
            if self._readiness_misses>=80:
                self._native_diagnostic(preparation='own_stage_not_ready')
                self._finish_wechat_attempt("无法识别：扫码窗口未准备好，请重新框选")
                return
            self._auto_stage_settling = True
            self._auto_stage_signature = signature if ready else None
            self._auto_handoff_callback = self.root.after(15,
                lambda request=request: self._start_auto_handoff(request))
            return
        if not self._native_stage_focus_ready:
            # Mapping/layout settle on the Tk thread before normal activation.
            # The helper refuses to steal focus if the user changed apps.
            try:
                preview_hwnd = self._own_top_hwnd(self._freeze_preview)
                finder_hwnd = self._own_top_hwnd(self.finder)
                self._native_stage_focus_ready = focus_own_stage(
                    preview_hwnd, allowed_foreground_hwnds=(finder_hwnd, preview_hwnd))
            except (OSError, ValueError, RuntimeError, tk.TclError):
                self._native_stage_focus_ready = False
            if (self._closed or self._closing or self._auto_handoff_request is not request
                    or self._generation != request.generation or self._frozen_frame is not frame
                    or self.finder.capture_bbox(clip_to_desktop=False) != request.bbox):
                self._native_stage_focus_ready = False
                return
            if not self._native_stage_focus_ready:
                self._native_diagnostic(preparation='own_stage_foreground_rejected')
                self._finish_wechat_attempt("无法识别：请保持扫码窗口在前台，再重新框选")
                return
            self._finder_hidden_for_native = True
            self.finder.withdraw()
        self._auto_stage_settling = False
        self._readiness_misses=0
        self.request_native_handoff(auto_request=request)

    def _auto_stage_readiness(self, frame: FrozenFrame) -> tuple[bool, tuple | None]:
        """UI readiness only; original stage/pixel/native checks remain mandatory."""
        widgets = (self._freeze_preview, self._freeze_image_label, self._freeze_white_stage)
        if any(widget is None for widget in widgets):
            return False, None
        try:
            if not all(widget.winfo_exists() and widget.winfo_viewable() for widget in widgets):
                return False, None
            label = self._freeze_image_label
            if (label.winfo_width(), label.winfo_height()) != (frame.width, frame.height):
                return False, None
            signature = tuple((widget.winfo_rootx(), widget.winfo_rooty(), widget.winfo_width(),
                               widget.winfo_height(), int(widget.winfo_ismapped())) for widget in widgets)
            return True, (self._freeze_stage_epoch, signature)
        except tk.TclError:
            return False, None

    def _invalidate_source(self, message: str | None = None) -> None:
        """Cancel before mutation; an already consumed submission may be unknown."""
        self._cancel_native_before_mutation()
        self._cancel_pending_auto_handoff()
        had_freeze = self._active_freeze is not None or self._frozen_frame is not None
        self._generation += 1
        self._clear_confirmation()
        self._selected_instance=None
        self._instances=()
        self._choice_token=None
        self._results=[]
        self._payload_text=''
        if hasattr(self,'_multi_list'):
            self._multi_list.delete(0,'end')
            self._refresh_actions()
        self._automatic_candidate, self._candidate_count = None, 0
        self._wechat_candidate, self._wechat_candidate_count = None, 0
        self._wechat_active_digest = None
        self._pending_freeze = None
        self._active_freeze = None
        self._frozen_frame = None
        self._freeze_stage_epoch += 1
        self._freeze_stage_id = None
        self._pending_stage_verify = None
        self._active_stage_verify_id = None
        self._stage_verification = None
        self._stage_verification_error = None
        self._freeze_image_label = None
        self._freeze_white_stage = None
        self._stage_geometry = {}
        self._native_button = None
        preview, self._freeze_preview = self._freeze_preview, None
        self._freeze_photo = None
        self._native_stage_focus_ready = False
        if preview is not None:
            preview.destroy()
        if getattr(self, '_finder_hidden_for_native', False):
            self._finder_hidden_for_native = False
            if not self._closing and self.finder._visible:
                self.finder.deiconify()
        if had_freeze:
            self._set_payload("暂停画面已释放；点击“继续扫描”返回屏幕取景。")
            if message:
                self._set_status(message)

    def cancel_freeze(self, _event: tk.Event | None = None) -> str:
        if self._closed or self._closing:
            return "break"
        self._auto_internal_pause = False
        self._invalidate_source()
        self.paused.set(False)
        self.finder._draw()
        self._set_status("已返回扫描，移动取景框即可。")
        return "break"

    def _resume_scanning(self) -> None:
        """Release only our transient stage; never infer foreign UI cleanup."""
        self._invalidate_source()
        self._auto_internal_pause = False
        self.paused.set(False)
        self.finder._draw()

    def _record_wechat_outcome(self, digest: bytes | None, message: str, warning: bool) -> None:
        """A completed attempt belongs to its content, never the next live read."""
        if digest is None:
            return
        outcomes = getattr(self, "_wechat_outcomes", None)
        if outcomes is None:
            self._wechat_outcomes = outcomes = {}
        # Match the admission bound; never evict an earlier completion and turn
        # a stationary, already handled target back into a fresh operation.
        if digest in outcomes or len(outcomes) < 512:
            outcomes[digest] = (message, warning)

    def _finish_wechat_attempt(self, message: str, warning: bool = True) -> None:
        digest = getattr(self, "_wechat_active_digest", None)
        self._resume_scanning()
        self._record_wechat_outcome(digest, message, warning)
        self._set_status(message, warning=warning)

    def _own_top_hwnd(self, window: tk.Misc) -> int:
        """Called on the UI thread; never interprets a Sky token as an HWND."""
        if os.name != "nt":
            raise OSError("原始画面截取需要 Windows。")
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        handle = user32.GetAncestor(window.winfo_id(), 2)
        if not handle:
            raise OSError("无法核实本程序窗口的显示缩放。")
        return int(handle)

    def _own_dpi_reference_hwnds(self) -> tuple[int, ...]:
        """Read only existing own Tk top-level handles on the UI thread."""
        handles = []
        for window in (self.root, self.finder, self._freeze_preview):
            if window is not None:
                value = self._own_top_hwnd(window)
                if value not in handles:
                    handles.append(value)
        return tuple(handles)

    def _require_ui_thread(self) -> None:
        if threading.get_ident() != self._ui_thread_id:
            raise RuntimeError("Stage UI methods must run on the owning Tk thread")

    def _stage_layout_changed(self, event: tk.Event) -> None:
        if event.widget not in (
            self._freeze_preview, self._freeze_image_label, self._freeze_white_stage,
        ):
            return
        widget = event.widget
        geometry = (widget.winfo_rootx(), widget.winfo_rooty(), widget.winfo_width(),
                    widget.winfo_height(), int(widget.winfo_ismapped()))
        if self._stage_geometry.get(widget) == geometry:
            return
        self._stage_geometry[widget] = geometry
        if self._frozen_frame is not None and not getattr(self, "_auto_stage_settling", False):
            self._cancel_pending_auto_handoff()
        self._cancel_native_before_mutation()
        self._freeze_stage_epoch += 1
        self._pending_stage_verify = None
        self._active_stage_verify_id = None
        self._stage_verification = None
        self._stage_verification_error = None
        self._refresh_native_action()

    def describe_frozen_stage(self) -> FrozenStageMapping:
        """UI-thread own-widget mapping; no screenshot or third-party input."""
        self._require_ui_thread()
        frame, preview = self._frozen_frame, self._freeze_preview
        label, padding = self._freeze_image_label, self._freeze_white_stage
        if (self._closed or frame is None or preview is None or label is None
                or padding is None or self._freeze_photo is None
                or self._freeze_stage_id is None or not self.paused.get()
                or frame.generation != self._generation
                or not all(w.winfo_exists() and w.winfo_viewable() for w in (preview, label, padding))):
            raise ValueError("画面预览当前不能用于微信识别。")
        if (label.cget("image") != str(self._freeze_photo)
                or (self._freeze_photo.width(), self._freeze_photo.height()) != (frame.width, frame.height)
                or (label.winfo_width(), label.winfo_height()) != (frame.width, frame.height)):
            raise ValueError("预览没有按原始像素完整显示。")
        return describe_stage_native(
            frame, stage_id=self._freeze_stage_id, stage_epoch=self._freeze_stage_epoch,
            top_hwnd=self._own_top_hwnd(preview), image_hwnd=int(label.winfo_id()),
            padding_hwnd=int(padding.winfo_id()),
            dpi_reference_hwnds=self._own_dpi_reference_hwnds(),
            guard_physical_px=LIVE_NATIVE_GUARD_PHYSICAL_PX,
            white_padding_physical_px=LIVE_NATIVE_WHITE_PADDING_PHYSICAL_PX,
        )

    def frozen_stage_matches(self, mapping: FrozenStageMapping) -> bool:
        """UI commit boundary, independent of the completed freeze's old deadline."""
        self._require_ui_thread()
        frame = self._frozen_frame
        if (not isinstance(mapping, FrozenStageMapping) or self._closed or frame is None
                or not self.paused.get() or frame.frame_id != mapping.frame_id
                or self._generation != mapping.generation
                or self._freeze_stage_id != mapping.stage_id
                or self._freeze_stage_epoch != mapping.stage_epoch):
            return False
        try:
            preview, label, padding = self._freeze_preview, self._freeze_image_label, self._freeze_white_stage
            if (preview is None or label is None or padding is None or self._freeze_photo is None
                    or not all(w.winfo_exists() and w.winfo_viewable() for w in (preview, label, padding))
                    or self._own_top_hwnd(preview) != mapping.own_stage.top_hwnd
                    or int(label.winfo_id()) != mapping.own_stage.image_hwnd
                    or int(padding.winfo_id()) != mapping.own_stage.padding_hwnd
                    or label.cget("image") != str(self._freeze_photo)
                    or (self._freeze_photo.width(), self._freeze_photo.height()) != (frame.width, frame.height)):
                return False
            return stage_mapping_is_current(frame, mapping)
        except (tk.TclError, OSError, ValueError, RuntimeError):
            return False

    def request_stage_verification(self) -> str:
        """Queue one actual own-stage pixel check on the existing process worker."""
        self._require_ui_thread()
        if self._closed or self._closing:
            raise RuntimeError("程序正在退出，不能开始新的核验。")
        if self._active_stage_verify_id is not None:
            raise RuntimeError("当前画面正在核验，请稍候。")
        mapping = self.describe_frozen_stage()
        request_id = str(uuid4())
        self._pending_stage_verify = _StageVerifyRequest(
            self._frozen_frame, mapping, request_id, time.monotonic_ns() + 2_000_000_000,
        )
        self._active_stage_verify_id = request_id
        self._stage_verification = None
        self._stage_verification_error = None
        return request_id

    def get_stage_verification(self) -> StagePixelVerification | None:
        self._require_ui_thread()
        result = self._stage_verification
        if result is None or not self.frozen_stage_matches(result.mapping):
            return None
        return result

    def request_freeze(self, _event: tk.Event | None = None, *, native_entered_ns: int | None = None,
                       native_by_button: bool = False, expected_wechat_digest: bytes | None = None) -> str:
        if getattr(self,'_measurement',None) is not None: return 'break'
        if self._closed or self._closing:
            return "break"
        if native_entered_ns is not None and not self._wechat_ready(refresh=True):
            return 'break'
        self._invalidate_source()
        self._auto_internal_pause = native_entered_ns is not None
        if self._native_job is not None and not self._native_job.done.is_set():
            self._set_status("上一次微信操作正在收尾，请稍候再截取新画面。", warning=True)
            return "break"
        self._pending_image = None
        if not self.finder._visible:
            self._set_status("请先显示取景框，再截取当前画面。", warning=True)
            return "break"
        if self.finder._drag or self.finder._resize:
            self._set_status("请先松开取景框，再点击识别。", warning=True)
            return "break"
        try:
            bbox = self.finder.capture_bbox(clip_to_desktop=False)
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                raise ValueError("取景区域为空，请调整取景框。")
            references = self._own_dpi_reference_hwnds()
        except (OSError, ValueError) as exc:
            self._set_status("无法暂停画面：" + str(exc)[:160], warning=True)
            return "break"
        # Pause before arming a new generation; do not call toggle_pause(),
        # which intentionally cancels every previously armed freeze request.
        self.paused.set(True)
        self.finder._draw()
        requested_at = time.monotonic()
        deadline = None  # Recognition/preparation have no fixed overall time limit.
        request = _FreezeRequest(
            generation=self._generation, request_id=str(uuid4()), bbox=bbox,
            dpi_reference_hwnds=references, requested_monotonic=requested_at,
            deadline_monotonic=deadline, native_entered_ns=native_entered_ns,
            manual_scan=native_by_button,
            expected_wechat_digest=expected_wechat_digest,
        )
        self._wechat_active_digest = expected_wechat_digest
        if expected_wechat_digest is not None:
            getattr(self, "_wechat_outcomes", {}).pop(expected_wechat_digest, None)
        self._pending_freeze = self._active_freeze = request
        if native_entered_ns is not None:
            self._auto_handoff_request = request
            self._auto_stage_settling = True
        # Existing decoded text does not belong to this independently captured
        # raw frame. Freeze never invokes decoding or browser/application input.
        self._results, self._result_key = [], ()
        self._set_payload("截取后将显示取景框内的原始画面。")
        self._refresh_actions()
        self._set_status("正在截取当前取景画面…")
        return "break"

    def _freeze_matches(self, request_id: str, generation: int) -> bool:
        request = self._active_freeze
        return bool(
            not self._closed and request is not None
            and request.request_id == request_id and request.generation == generation
            and generation == self._generation and self.paused.get()
            and self.finder._visible and (request.deadline_monotonic is None or time.monotonic() <= request.deadline_monotonic)
        )

    def _show_frozen_frame(self, frame: FrozenFrame) -> None:
        # Preserve a one-physical-pixel to one-image-pixel preview. Reject an
        # oversized view instead of silently resizing pixels to make it fit.
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        required_padding = 2 * LIVE_NATIVE_WHITE_PADDING_PHYSICAL_PX
        if (frame.width + required_padding > screen_width
                or frame.height + required_padding > screen_height):
            raise _StageDoesNotFit("请缩小取景框，只框住完整二维码；仍放不下时缩小图片显示比例")
        self._finder_hidden_for_native = False
        preview = tk.Toplevel(self.root)
        preview.withdraw()
        self._freeze_preview = preview
        self._freeze_stage_id = str(uuid4())
        self._freeze_stage_epoch += 1
        preview.bind("<Configure>", self._stage_layout_changed, add="+")
        preview.bind("<Unmap>", self._stage_layout_changed, add="+")
        preview.title("ScreenQR · 正在识别")
        preview.configure(bg=PAPER)
        preview.resizable(False, False)
        preview.attributes("-topmost", True)
        preview.protocol("WM_DELETE_WINDOW", self.cancel_freeze)
        preview.bind("<Escape>", self.cancel_freeze)
        shell = tk.Frame(preview, bg=PAPER, padx=12, pady=12)
        shell.pack(fill="both", expand=True)
        heading = self._label(shell, f"原始画面 · {frame.width} × {frame.height} 像素", font=BODY)
        heading.pack(anchor="w", pady=(0, 8))
        with Image.frombytes("RGB", (frame.width, frame.height), frame.rgb8) as image:
            self._freeze_photo = ImageTk.PhotoImage(image, master=preview)
        white_stage = tk.Frame(
            shell, bg=WHITE, bd=0, highlightthickness=0,
            padx=LIVE_NATIVE_WHITE_PADDING_PHYSICAL_PX,
            pady=LIVE_NATIVE_WHITE_PADDING_PHYSICAL_PX,
        )
        self._freeze_white_stage = white_stage
        white_stage.pack(anchor="w")
        self._freeze_image_label = tk.Label(
            white_stage, image=self._freeze_photo, bg=WHITE, bd=0,
            highlightthickness=0, padx=0, pady=0, anchor="nw",
        )
        self._freeze_image_label.pack(anchor="nw")
        footer = self._label(shell, "正在自动识别，请稍候。完成后会返回扫描。", fg=MUTED, font=SMALL)
        footer.pack(anchor="w", pady=(8, 0))
        # Large sources can omit presentation chrome, never source pixels or
        # calibration padding. Choose the final layout before showing/binding it.
        preview.update_idletasks()
        width, height = preview.winfo_reqwidth(), preview.winfo_reqheight()
        compact = width > screen_width - 32 or height > screen_height - 80
        if compact:
            heading.destroy()
            footer.destroy()
            shell.configure(padx=0, pady=0)
            preview.overrideredirect(True)
            preview.update_idletasks()
            width, height = preview.winfo_reqwidth(), preview.winfo_reqheight()
            if width > screen_width or height > screen_height:
                raise _StageDoesNotFit("请缩小取景框，只框住完整二维码；仍放不下时缩小图片显示比例")
            _position(preview, (screen_width - width) // 2,
                      (screen_height - height) // 2, width, height)
        else:
            _position(preview, max(16, (screen_width - width) // 2),
                      max(24, (screen_height - height) // 2 - 30), width, height)
        # Geometry updates can process queued Tk configure events. Recheck the
        # request after those events, before making a frame usable or visible.
        if not self._freeze_matches(frame.frame_id, frame.generation):
            raise ValueError("取景状态已变化或画面读取超时，请重新框选。")
        disable_own_stage_transitions(self._own_top_hwnd(preview))
        preview.deiconify()
        preview.lift()
        self._native_stage_focus_ready = False
        # Finish Tk image drawing before a worker can request desktop pixels.
        # DWM readiness and actual pixels are still checked by stage verification.
        preview.update_idletasks()
        if not self._freeze_matches(frame.frame_id, frame.generation):
            raise ValueError("取景状态已变化或画面读取超时，请重新框选。")
        # Keep the original foreground window alive until mapping has settled
        # and _start_auto_handoff verifies its replacement is actually active.
        # Mapping the new transient window can deliver Configure after its
        # children have already painted. Seed the actual completed layout so
        # those same-geometry notifications do not cancel the first gesture.
        self._stage_geometry = {
            widget: (widget.winfo_rootx(), widget.winfo_rooty(), widget.winfo_width(),
                     widget.winfo_height(), int(widget.winfo_ismapped()))
            for widget in (preview, self._freeze_image_label, self._freeze_white_stage)
        }
        self._frozen_frame = frame
        self._set_payload("正在识别框内的二维码，请稍候。")
        self._set_status("微信正在识别，完成后会自动返回扫描。")
        self._refresh_native_action()

    def _refresh_native_action(self) -> None:
        button = getattr(self, "_native_button", None)
        if button is None:
            return
        frame = self._frozen_frame
        job = self._native_job
        enabled = (not self._closed and not self._closing and frame is not None
            and self._auto_handoff_request is None
            and self._native_spent_frame_id != frame.frame_id
            and (job is None or job.done.is_set()))
        button.configure(state="normal" if enabled else "disabled")

    def request_native_handoff(self, *, auto_request: _FreezeRequest | None = None) -> None:
        entered_ns = time.monotonic_ns() if auto_request is None else auto_request.native_entered_ns
        self._require_ui_thread()
        if self._closed or self._closing:
            return
        if not self._wechat_ready(refresh=True):
            self._finish_wechat_attempt(getattr(self._environment,'message','微信暂时不能扫，普通码照常能扫。'))
            return
        self._poll_native_job()
        if self._native_job is not None:
            self._set_status("上一次微信操作正在收尾，请稍候。", warning=True)
            return
        frame = self._frozen_frame
        # Automatic consent is the enabled mode plus this same deliberate
        # changed gesture and immutable request, never a fabricated checkbox.
        gesture_confirmed = (auto_request is not None
            and self._auto_handoff_request is auto_request
            and (auto_request.manual_scan or self.wechat_on_release.get())
            and frame is not None and frame.frame_id == auto_request.request_id
            and frame.generation == auto_request.generation == self._generation
            and self.finder.capture_bbox(clip_to_desktop=False) == auto_request.bbox)
        # The explicit scan button selects the current displayed frame. A
        # pending gesture/button request cannot be replaced with a fresh budget.
        manual_confirmed = auto_request is None and self._auto_handoff_request is None
        if (frame is None or not (gesture_confirmed or manual_confirmed)
                or self._native_spent_frame_id == frame.frame_id):
            self._set_status("请把一个码完整放进框内，程序会自动识别；同一画面只处理一次。", warning=True)
            return
        try:
            mapping = self.describe_frozen_stage()
            signals=getattr(self,'_display_signals',None)
            epoch=signals.epoch if signals is not None else None
            job = NativeJob(frame, mapping, entered_ns=entered_ns, single_target_confirmed=True,
                profile=getattr(self,'_native_profile',DEFAULT_PROFILE),
                display_guard=(lambda signals=signals,epoch=epoch:signals.epoch==epoch) if signals is not None else None)
        except (OSError, ValueError, RuntimeError, tk.TclError):
            self._finish_wechat_attempt("无法识别：请重新框选完整二维码")
            return
        self._native_spent_frame_id = frame.frame_id  # Claim before worker start.
        self._cancel_pending_auto_handoff()
        self._native_auto_frame_id = frame.frame_id if gesture_confirmed else None
        self._native_auto_frame_key = (frame.width, frame.height, frame.capture_sha256) if gesture_confirmed else None
        if gesture_confirmed:
            self._auto_seen_frames.add((frame.width, frame.height, frame.capture_sha256))
        self._native_job = job
        self._native_outcome_digest = getattr(self, "_wechat_active_digest", None)
        self._native_source_generation = frame.generation
        self._refresh_native_action()
        self._set_status("微信正在识别，请稍候；本次不会重复打开。")
        thread = threading.Thread(target=job.run, name="ScreenQR-WeChat", daemon=True)
        self._native_thread = thread
        try:
            thread.start()
        except RuntimeError:
            job.cancel()
            self._native_job = self._native_thread = None
            self._native_outcome_digest = self._native_source_generation = None
            self._finish_wechat_attempt("无法识别：请重新框选后再试")

    def _poll_native_job(self) -> None:
        """Consume its dedicated result slot; bounded event-queue loss is harmless."""
        job = getattr(self, "_native_job", None)
        if job is None or not job.done.is_set():
            return
        result = job.result
        if result is not None:
            self._native_diagnostic(result=result)
        digest = getattr(self, "_native_outcome_digest", None)
        source_generation = getattr(self, "_native_source_generation", None)
        if source_generation is None:
            source_generation = getattr(job, "generation", self._generation)
        source_is_current = source_generation == self._generation
        self._native_outcome_digest = self._native_source_generation = None
        self._native_job = self._native_thread = None
        self._native_last_result = result
        was_auto = (getattr(self, "_native_auto_frame_id", None) is not None
                    and self._native_auto_frame_id == getattr(job, "frame_id", None))
        auto_key = getattr(self, "_native_auto_frame_key", None)
        self._native_auto_frame_id = None
        self._native_auto_frame_key = None
        if (was_auto and auto_key is not None and result is not None
                and getattr(result, "retry_safe_before_input", False) is True):
            # Release only with explicit proof of zero native input. A new
            # deliberate gesture is required; never replay or queue a request.
            self._auto_seen_frames.discard(auto_key)
        if was_auto and (result is None or result.status in ("OUTCOME_UNKNOWN", "TIMED_OUT", "FAILED")
                         or result.cleanup_status in ("UNKNOWN", "FAILED", "UNBOUND_OUTCOME_UNKNOWN")):
            self._native_requires_gesture = True
        if source_is_current and not self._closing:
            # Submission is finished; release the raw preview instead of
            # leaving it over the newly opened business window.
            self._resume_scanning()
            if result is not None and (result.status == 'DISPATCHED_UNCONFIRMED'
                    or (result.status == 'UNSUPPORTED_BUSINESS' and getattr(result, 'dispatch_count', 0) == 1)):
                # The selected code is already handed off. Do not scan the
                # opened business page or replace this outcome with a false
                # failure for a non-standard symbol such as a mini-program code.
                self._handoff_idle = True
                self.paused.set(True)
                self.finder.attributes('-topmost', False)
                self.finder.lower()
        self._refresh_native_action()
        text, warning = describe_job_result(result) if result is not None else (
            "无法识别：请先查看微信，避免重复操作", True)
        self._record_wechat_outcome(digest, text, warning)
        if source_is_current and not self._closing:
            self._set_status(text, warning=warning)

    def _resume_after_handoff(self) -> None:
        if not getattr(self, '_handoff_idle', False):
            return
        self._handoff_idle = False
        self.paused.set(False)
        self.finder.attributes('-topmost', True)

    def _native_diagnostic(self, *, result=None, preparation=None) -> None:
        sink = getattr(self, '_diagnostics', None)
        if sink is None:
            return
        try:
            if result is not None:
                sink.result(result)
            else:
                sink.preparation(preparation)
        except Exception:
            pass  # Optional diagnosis cannot change input, recovery or product state.

    def _post_event(self, event: tuple) -> bool:
        """Short synchronized commit boundary; never wait for queue capacity."""
        with self._event_lock:
            if self._stop.is_set() or self._closed:
                return False
            try:
                self._events.put_nowait(event)
            except queue.Full:
                # Let the UI clear busy/opening if a completion is lost. A
                # rejected freeze must never remain eligible for publication.
                self._dropped_event_kinds.add("browser" if event[0] == "opened" else "worker")
                return False
            return True

    def _stop_event_delivery(self) -> None:
        """Atomically reject late completions and release queued frame pixels."""
        with self._event_lock:
            self._closed = True
            self._stop.set()
            self._dropped_event_kinds.clear()
            try:
                while True:
                    self._events.get_nowait()
            except queue.Empty:
                pass

    def _work(self) -> None:
        while not self._stop.is_set():
            try: job=self._jobs.get(timeout=0.25)
            except queue.Empty: continue
            if job is None or self._stop.is_set(): break
            started=time.monotonic()
            started_ns=time.monotonic_ns() if getattr(self,'_measurement',None) is not None else None
            image=None
            cancelled=lambda: (self._stop.is_set() or self._closing or job.generation != self._generation
                or (job.display_epoch is not None and getattr(self,'_display_signals',None) is not None
                    and self._display_signals.epoch!=job.display_epoch))
            try:
                with profile_scope(job.profile):
                    if cancelled(): raise CancelledError()
                    if job.stage_verify is not None:
                        verification=verify_frozen_stage(job.stage_verify.frame,job.stage_verify.mapping,
                            deadline_monotonic_ns=job.stage_verify.deadline_monotonic_ns)
                        event=("stage_verified",job.generation,job.stage_verify.request_id,verification,job.stage_verify.deadline_monotonic_ns)
                    elif job.freeze is not None:
                        request=job.freeze
                        frame=capture_region_with_metadata(request.bbox,generation=request.generation,
                            frame_id=request.request_id,dpi_reference_hwnds=request.dpi_reference_hwnds)
                        if cancelled(): raise CancelledError()
                        with Image.frombytes("RGB",(frame.width,frame.height),frame.rgb8) as pixels:
                            # A single RGB colour cannot contain any code. Reject
                            # this exact case before decode or native UI; leave all
                            # nonuniform frames eligible, including graphical codes.
                            if all(low == high for low, high in pixels.getextrema()):
                                raise ValueError("uniform_capture_has_no_code")
                            observation=self._observe_image(pixels,(job.generation,"screen",request.bbox),cancelled)
                        event=("frozen",job.generation,request.request_id,frame,
                               observation[0],time.monotonic()-started,observation)
                        del frame
                    else:
                        before=self._source_token(job)
                        image=load_image_for_decode(job.path) if job.path else self._capture_for_job(job)
                        captured_ns=time.monotonic_ns() if started_ns is not None else None
                        observation=self._observe_image(image,before,cancelled,selected=job.selected)
                        decoded_ns=time.monotonic_ns() if started_ns is not None else None
                        if before != self._source_token(job) or cancelled(): raise CancelledError()
                        if job.confirm is not None:
                            valid=matches(job.confirm,observation[1],before,image)
                            if valid and not job.path:
                                # A long decode cannot authorize an old capture.
                                with self._capture_for_job(job) as current:
                                    valid=(current.size==image.size and current.mode==image.mode
                                        and hashlib.sha256(current.tobytes()).digest()==hashlib.sha256(image.tobytes()).digest())
                            if cancelled(): raise CancelledError()
                            event=("confirmed",job.generation,job.confirm,valid,time.monotonic_ns(),job)
                        else:
                            if not self._job_display_current(job): raise CancelledError()
                            event=("decoded",job.generation,observation[0],job.path,time.monotonic()-started,observation)
                            if started_ns is not None:
                                event=(*event,(started_ns,captured_ns,decoded_ns))
            except CancelledError:
                event=("decode_cancelled",job.generation)
            except Exception:
                if job.freeze is not None: event=("freeze_error",job.generation,job.freeze.request_id,"无法识别")
                elif job.confirm is not None: event=("confirmed",job.generation,job.confirm,False)
                elif job.stage_verify is not None: event=("stage_verify_error",job.generation,job.stage_verify.request_id,job.stage_verify.mapping,"无法识别",job.stage_verify.deadline_monotonic_ns)
                else: event=("decode_error",job.generation,"无法识别",job.path)
            finally:
                if image is not None: image.close()
            self._post_event(event)
            event=job=image=None

    @staticmethod
    def _source_token(job: _Job) -> tuple:
        if job.path:
            stat=Path(job.path).stat()
            return (job.generation,"file",job.path,(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns))
        return (job.generation,"screen",job.bbox)

    def _observe_image(self, image: Image.Image, token: tuple, cancelled, *, selected=None) -> tuple:
        instances=tuple(read_qr_instances(image,cancelled=cancelled))
        by_text={}
        for instance in instances: by_text.setdefault(instance.text,classify_payload(instance.text,instance.format))
        results=list(by_text.values())
        candidate=(build_selected_candidate(instances,token,image,selected) if selected is not None and len(instances)>1
                   else build_candidate(instances,token,image))
        preview=None
        if candidate is not None:
            with image.crop(candidate.crop_box) as crop:
                thumb=crop.convert("RGB")
                edge=round(64*self.scale)
                ratio=min(edge/thumb.width,edge/thumb.height)
                target=(max(1,round(thumb.width*ratio)),max(1,round(thumb.height*ratio)))
                if thumb.size!=target:
                    scaled=thumb.resize(target,Image.Resampling.NEAREST)
                    thumb.close()
                    thumb=scaled
                preview=(thumb.size,thumb.tobytes())
                thumb.close()
        return results,instances,candidate,preview,token

    def _route_captured_target(self, results: list[ScanResult] | None, elapsed: float) -> bool:
        if not results or (len(results)==1 and results[0].kind=="wechat"): return False
        self._resume_scanning()
        if len(results)>1: self._set_status("框里有多个码，请只框一个")
        elif results[0].kind=="wechat_payment": self._set_status("无法识别：微信不支持从图片处理这种支付码",warning=True)
        else: self._set_status("正在识别")
        return True

    @staticmethod
    def _capture_matches_automatic_intent(request: _FreezeRequest, results: list[ScanResult] | None) -> bool:
        expected = request.expected_wechat_digest
        return expected is None or bool(results is not None and len(results) == 1
            and results[0].kind == 'wechat'
            and hashlib.sha256(results[0].text.encode('utf-8')).digest() == expected)

    def _tick(self) -> None:
        if self._closed:
            return
        self._watch_environment()
        self._poll_native_job()
        if self._closing:
            return
        with self._event_lock:
            dropped = self._dropped_event_kinds.copy()
            self._dropped_event_kinds.clear()
        if "worker" in dropped:
            self._busy = False
            self._clear_confirmation()
            if self._active_freeze is not None:
                self._invalidate_source()
            self._pending_stage_verify = None
            self._active_stage_verify_id = None
            self._stage_verification = None
            self._stage_verification_error = "处理队列繁忙，请重新核验。"
            self._set_status("处理队列繁忙，本次画面未保留；请重新扫描或截取。", warning=True)
        if "browser" in dropped:
            self._opening = False
            self._refresh_actions()
        try:
            while True:
                event = self._events.get_nowait()
                kind = event[0]
                if kind in ("stage_verified", "stage_verify_error"):
                    self._busy = False
                    if event[2] != self._active_stage_verify_id:
                        continue
                    self._active_stage_verify_id = None
                    deadline_ns = event[4] if kind == "stage_verified" else event[5]
                    if (isinstance(deadline_ns, bool) or not isinstance(deadline_ns, int)
                            or time.monotonic_ns() >= deadline_ns):
                        self._stage_verification = None
                        self._stage_verification_error = "核验结果已超时，请重新核验。"
                        continue
                    mapping = event[3].mapping if kind == "stage_verified" else event[3]
                    if not self.frozen_stage_matches(mapping):
                        self._stage_verification = None
                        self._stage_verification_error = "预览状态已变化，请重新核验。"
                        continue
                    # The native metadata match above may itself take time.
                    # Publication, not only worker completion, shares the deadline.
                    if time.monotonic_ns() >= deadline_ns:
                        self._stage_verification = None
                        self._stage_verification_error = "核验结果已超时，请重新核验。"
                        continue
                    if kind == "stage_verify_error":
                        self._stage_verification = None
                        self._stage_verification_error = event[4]
                    else:
                        self._stage_verification = event[3]
                        self._stage_verification_error = None
                elif kind in ("frozen", "freeze_error"):
                    self._busy = False
                    if not self._freeze_matches(event[2], event[1]):
                        continue
                    request = self._active_freeze
                    if kind == "freeze_error":
                        self._finish_wechat_attempt("无法识别")
                        continue
                    frame = event[3]
                    if (not isinstance(frame, FrozenFrame)
                            or frame.generation != request.generation
                            or frame.frame_id != request.request_id
                            or frame.requested_bbox != request.bbox
                            or self.finder.capture_bbox(clip_to_desktop=False) != request.bbox
                            or frame.capture_started_monotonic < request.requested_monotonic
                            or (request.deadline_monotonic is not None and frame.capture_ended_monotonic > request.deadline_monotonic)):
                        self._finish_wechat_attempt("无法识别：画面已变化，请重新框选")
                        continue
                    try:
                        if not self._capture_matches_automatic_intent(request, event[4] if len(event) > 4 else None):
                            self._resume_scanning()
                            self._set_status('框里的二维码刚刚发生变化，已取消旧操作，继续扫描。')
                            continue
                        if len(event)>6 and len(event[6][1])>1:
                            self._resume_scanning()
                            self._set_status("框里有多个码，请只框一个")
                            continue
                        if len(event)>4 and len(event[4])==1 and event[4][0].kind=="wechat":
                            # A deliberate raw gesture can also locate a normal
                            # WeChat QR. Bind its completion to the independently
                            # captured content before any display/native work.
                            digest=hashlib.sha256(event[4][0].text.encode("utf-8")).digest()
                            self._wechat_active_digest=digest
                            getattr(self,"_wechat_outcomes",{}).pop(digest,None)
                            if len(self._wechat_seen_payloads)<512:
                                self._wechat_seen_payloads.add(digest)
                        if len(event) > 4 and self._route_captured_target(event[4], event[5]):
                            if len(event)>6:
                                # _resume_scanning changes generation: wait for a fresh live candidate.
                                self._next_scan=0
                            continue
                        self._show_frozen_frame(frame)
                    except (tk.TclError, OSError, ValueError) as exc:
                        message="无法识别："+str(exc) if isinstance(exc,_StageDoesNotFit) else (
                            "无法识别：请重新框选完整二维码")
                        self._finish_wechat_attempt(message)
                    else:
                        self._active_freeze = None
                        if request.native_entered_ns is not None and self._auto_handoff_request is request:
                            self._auto_handoff_callback = self.root.after_idle(
                                lambda request=request: self._start_auto_handoff(request))
                elif kind == "decode_cancelled":
                    self._busy=False
                elif kind == "confirmed":
                    self._busy=False
                    candidate=event[2]
                    eligible=(event[1]==self._generation and self._confirm_inflight==candidate
                        and self._candidate==candidate and not self._closed and not self._closing)
                    self._confirm_inflight=None
                    if eligible and candidate.source_token[1]=="screen":
                        eligible=self.finder._visible and self.finder.capture_bbox()==candidate.source_token[2]
                    if eligible and candidate.source_token[1]=="file":
                        try:
                            eligible=self._source_token(_Job(self._generation,path=candidate.source_token[2]))==candidate.source_token
                        except OSError: eligible=False
                    if eligible:
                        eligible=len(event)>4 and 0<=time.monotonic_ns()-event[4]<=500_000_000
                    if eligible and len(event)>5:
                        eligible=self._job_display_current(event[5])
                    if eligible and event[3]:
                        self._browser_grant=candidate
                        self._open_url(candidate.open_url)
                    elif eligible:
                        self._clear_confirmation()
                        self._set_status("无法识别：二维码已变化，请重新确认",warning=True)
                    self._refresh_actions()
                elif kind in ("decoded", "decode_error"):
                    self._busy = False
                    if event[1] != self._generation:
                        continue
                    if kind == "decoded":
                        measurement=getattr(self,'_measurement',None)
                        if measurement is not None and not measurement.allows(event[5][1]):
                            measurement.abort()
                            self.paused.set(True)
                            self._invalidate_source()
                            self._set_status('测试画面变了，本次测试已暂停。')
                            continue
                        self._accept_results(event[2], event[3], event[4], event[5] if len(event)>5 else None)
                        if measurement is not None and len(event)>6:
                            def measured(event=event,measurement=measurement):
                                if not self._closed and not self._closing and event[1]==self._generation:
                                    measurement.record(event[5][1],event[6],event[1])
                            self.root.after_idle(measured)
                    else:
                        self._clear_confirmation()
                        self._set_status("无法识别", warning=True)
                        if event[3] is None:
                            self._next_scan = time.monotonic() + 2.0
                elif kind == "opened":
                    self._opening = False
                    self._refresh_actions()
                    if len(event)==5:
                        self._complete_browser_open(*event[1:])
        except queue.Empty:
            pass
        now = time.monotonic()
        if self._active_freeze is not None and self._active_freeze.deadline_monotonic is not None and now > self._active_freeze.deadline_monotonic:
            self._resume_scanning()
            self._set_status("画面读取已超时；画面未保留，请重新框选。", warning=True)
        if not self._busy:
            job = None
            if self._pending_confirm is not None:
                candidate,self._pending_confirm=self._pending_confirm,None
                if candidate==self._candidate:
                    token=candidate.source_token
                    self._confirm_inflight=candidate
                    job=(self._screen_job(token[2],confirm=candidate) if token[1]=='screen' else
                         _Job(self._generation,path=token[2],confirm=candidate))
            elif self._pending_freeze is not None:
                request, self._pending_freeze = self._pending_freeze, None
                job = _Job(request.generation, freeze=request,profile=getattr(self,'_native_profile',DEFAULT_PROFILE))
            elif self._pending_stage_verify is not None:
                request, self._pending_stage_verify = self._pending_stage_verify, None
                job = _Job(request.mapping.generation, stage_verify=request,profile=getattr(self,'_native_profile',DEFAULT_PROFILE))
                del request
            elif self._pending_image:
                job = _Job(self._generation, path=self._pending_image,selected=getattr(self,'_selected_instance',None))
                self._pending_image = None
            elif (not self.paused.get() and self.finder._visible
                    and self._native_job is None and now >= self._next_scan):
                bbox = self.finder.capture_bbox()
                if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                    job = self._screen_job(bbox)
                self._next_scan = now + 0.28
            if job is not None:
                self._jobs.put_nowait(job)
                self._busy = True
        self.root.after(60, self._tick)

    def _accept_results(self, results: list[ScanResult], path: str | None, elapsed: float, observation=None) -> None:
        if self._closed or self._closing:
            return
        self._results=list(results)
        self._result_key=tuple((item.text,str(item.format)) for item in results)
        if observation is None:
            self._clear_confirmation()
            if not results: self._set_status("无法识别")
            return
        _,instances,candidate,preview,token=observation
        self._update_choices(instances,token)
        selected=getattr(self,'_selected_instance',None)
        if not instances or (len(instances)>1 and selected is None):
            self._clear_confirmation()
            self._wechat_candidate,self._wechat_candidate_count=None,0
            if len(instances)>1:
                self._show_target('点下面的一个码，再看清内容。')
            self._set_status("框里有多个码，点列表选一个" if len(instances)>1 else "无法识别")
            self._refresh_actions()
            return
        result=classify_payload(selected.text,selected.format) if len(instances)>1 else classify_payload(instances[0].text,instances[0].format)
        self._payload_text=result.text
        history=getattr(self,'_history',None)
        if history is not None:
            history.record(result)
        # 点选不放宽微信多码停交；它需要重新框成唯一目标。
        if len(instances)>1 and result.kind in ('wechat','wechat_payment'):
            self._clear_confirmation()
            self._show_target(result_line(result))
            self._set_status('付款码不处理' if result.kind=='wechat_payment' else '微信内容请缩小框，只留这一个码。')
            self._refresh_actions()
            return
        if candidate is not None:
            if candidate==self._last_opened_candidate:
                self._clear_confirmation(); self._set_status("已打开"); return
            if candidate != self._candidate:
                self._clear_confirmation()
                self._candidate=candidate
                if preview is not None:
                    with Image.frombytes("RGB",preview[0],preview[1]) as thumb:
                        self._preview_photo=ImageTk.PhotoImage(thumb,master=self.finder)
                    self._preview_label.configure(image=self._preview_photo)
                self._show_target(candidate.open_url)
            self._set_status("看清网址后点“在浏览器打开”")
            self._refresh_actions()
            return
        self._clear_confirmation(clear_view=False)
        self._show_target(result_line(result))
        if result.kind=="wechat_payment":
            self._set_status("无法识别：微信不支持从图片处理这种支付码",warning=True)
        elif result.kind=="wechat" and path is None:
            if not self._wechat_ready(): return
            digest=hashlib.sha256(result.text.encode("utf-8")).digest()
            outcome=getattr(self,"_wechat_outcomes",{}).get(digest)
            if outcome is not None:
                self._set_status(outcome[0],warning=outcome[1])
                return
            if (digest in self._wechat_seen_payloads or self._native_requires_gesture) and not (
                    getattr(self,"_wechat_active_digest",None)==digest
                    and (self._native_job is not None or self._auto_handoff_request is not None)):
                self._set_status("无法识别：请重新框选完整二维码",warning=True)
                return
            self._set_status("正在识别微信内容")
            if (digest==self._wechat_candidate): self._wechat_candidate_count+=1
            else: self._wechat_candidate,self._wechat_candidate_count=digest,1
            if (self._wechat_candidate_count>=2 and digest not in self._wechat_seen_payloads
                    and not self._native_requires_gesture and self._native_job is None
                    and self._auto_handoff_request is None and not self.paused.get()
                    and self.finder._visible and not self.finder._drag and not self.finder._resize):
                if len(self._wechat_seen_payloads)>=512:
                    self._set_status("无法识别：请重新启动程序",warning=True)
                else:
                    self._wechat_seen_payloads.add(digest)
                    self.request_freeze(native_entered_ns=time.monotonic_ns(),expected_wechat_digest=digest)
        elif result.kind=="wechat":
            self._set_status("微信码请在屏幕上显示后框扫")
        else:
            self._set_status('扫到了 · '+kind_name(result))
        self._refresh_actions()

    def _update_choices(self, instances, token):
        ordered=tuple(sorted(instances,key=lambda item:(item.points,item.format,item.text)))
        changed=ordered!=getattr(self,'_instances',()) or token!=getattr(self,'_choice_token',None)
        self._instances,self._choice_token=ordered,token
        if getattr(self,'_selected_instance',None) not in ordered:
            self._selected_instance=None
        widget=getattr(self,'_multi_list',None)
        if widget is not None and changed:
            widget.delete(0,'end')
            for index,item in enumerate(ordered):
                widget.insert('end',f'{index+1}. {result_line(classify_payload(item.text,item.format))}')
            selected=getattr(self,'_selected_instance',None)
            if selected is not None:
                widget.selection_set(ordered.index(selected))

    def _choose_instance(self,_event=None):
        if self._closed or self._closing: return
        indices=self._multi_list.curselection()
        if len(indices)!=1 or indices[0]>=len(self._instances): return
        if self._choice_token is None or self._choice_token[0]!=self._generation: return
        self._clear_confirmation()
        self._selected_instance=self._instances[indices[0]]
        result=classify_payload(self._selected_instance.text,self._selected_instance.format)
        self._payload_text=result.text
        self._show_target(result_line(result))
        self._set_status('正在看你选的这个码')
        self._pending_image=self._file_source
        self._next_scan=0
        self._refresh_actions()

    def _clear_confirmation(self, *, clear_view=True) -> None:
        self._candidate=None
        self._pending_confirm=self._confirm_inflight=self._browser_grant=None
        self._preview_photo=None
        if hasattr(self,'confirm_button'):
            self.confirm_button.place_forget()
            self._preview_label.configure(image="")
            if clear_view: self._show_target('')

    def _selected(self) -> ScanResult | None:
        if len(getattr(self,'_instances',()))>1:
            selected=getattr(self,'_selected_instance',None)
            return classify_payload(selected.text,selected.format) if selected in self._instances else None
        return self._results[0] if len(self._results)==1 else None

    @staticmethod
    def _safe_url(result: ScanResult) -> str | None:
        # Manual opening, auto-opening and button enablement share this route.
        return browser_url_for_result(result)

    def _selection_changed(self, _event: tk.Event | None = None) -> None:
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        if not hasattr(self,"confirm_button"): return
        self.confirm_button.configure(state="normal" if self._candidate is not None
            and self._pending_confirm is None and self._confirm_inflight is None and not self._opening else "disabled")
        copy=getattr(self,'copy_button',None)
        if copy is not None:
            copy.configure(state='normal' if self._selected() is not None else 'disabled')
        self._layout_panel()

    def copy_selected(self) -> None:
        if self._closed or self._closing:
            return
        result = self._selected()
        if result is None:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(result.text)
            self._set_status("内容已复制到剪贴板。")
        except tk.TclError:
            self._set_status("剪贴板暂时不可用，请稍后重试。", warning=True)

    def open_selected(self) -> None:
        if self._closed or self._closing or self._opening: return
        candidate=self._candidate
        if candidate is None or self._pending_confirm is not None or self._confirm_inflight is not None: return
        if candidate.source_token[0] != self._generation: self._clear_confirmation(); return
        self._pending_confirm=candidate
        self._refresh_actions()
        self._set_status("正在看这个码有没有变化")

    def _open_url(self, url: str) -> None:
        if getattr(self,'_measurement',None) is not None: return
        if self._closed or self._closing:
            return
        # Only a completed click-bound fresh recheck can grant one browser action.
        grant=getattr(self,"_browser_grant",None)
        self._browser_grant=None
        if grant is None or grant.open_url != url or grant.source_token[0] != self._generation:
            return
        url = browser_url_for_result(classify_payload(url))
        if url is None:
            return
        if self._opening or self._closed or self._closing:
            return
        self._opening = True
        prior_pause=self.paused.get()
        # 先停取景，再启动浏览器，避免新页面中的码获得自动微信输入资格。
        self.paused.set(True)
        self._last_opened_candidate=grant
        self._clear_confirmation()
        self._refresh_actions()
        self._set_status("正在交给默认浏览器…")

        self._launch_browser(url,resume_paused=prior_pause)

    def _complete_browser_open(self,okay,generation,history,resume_paused):
        if self._closed or self._closing or generation!=self._generation: return
        if history:
            panel=getattr(self,'_history_panel',None)
            if panel is not None and panel.winfo_exists():
                panel.note.configure(text='已交给浏览器。' if okay else '浏览器没打开，请稍后再试或复制网址。')
            return
        if okay:
            self._handoff_idle=True
            self.paused.set(True)
            self._set_status('已交给浏览器。拖动框或按空格扫下一个。')
        else:
            self.paused.set(resume_paused)
            self._set_status('浏览器没打开，请稍后再试或复制网址。')

    def _launch_browser(self,url,*,history=False,resume_paused=False):
        if getattr(self,'_measurement',None) is not None:
            self._opening=False
            return
        # 历史页只在显式点击后到此；当前画面仍必须走上面的新鲜确认凭据。
        if browser_url_for_result(classify_payload(url)) != url:
            self._opening=False
            return
        generation=self._generation
        def launch_browser() -> None:
            try:
                okay = bool(webbrowser.open(url, new=2))
            except Exception:
                okay = False
            self._post_event(("opened", okay,generation,history,resume_paused))

        threading.Thread(target=launch_browser, name="ScreenQR-browser", daemon=True).start()

    def _auto_changed(self) -> None:
        self._refresh_actions()

    def close(self) -> None:
        if self._closed or self._closing:
            return
        self._closing = True
        self._invalidate_source()
        self._pending_image = None
        try:
            while True:
                self._jobs.get_nowait()
        except queue.Empty:
            pass
        self._jobs.put_nowait(None)
        self._set_status("正在取消并等待微信操作收尾，完成后退出…")
        self._finish_close()

    def _finish_close(self) -> None:
        # Keep Tk responsive and alive until native cleanup really finishes.
        # Synchronous Win32 calls cannot safely be killed by a Python timeout.
        self._poll_native_job()
        if self._native_job is not None or self._worker.is_alive():
            self.root.after(50, self._finish_close)
            return
        self._stop_event_delivery()
        measurement=getattr(self,'_measurement',None)
        if measurement is not None: measurement.close()
        signals=getattr(self,'_display_signals',None)
        if signals is not None:
            try: signals.close()
            except (DesktopChanged,OSError): pass
        self.finder.destroy()
        self.root.destroy()


def launch(*, diagnostics=None, measurement=None) -> None:
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        app=ScreenQRApp(root, diagnostics=diagnostics,measurement=measurement)
        if measurement is not None: measurement.attach(root,app)
    except Exception as exc:
        messagebox.showerror("ScreenQR 启动失败", f"屏幕扫码工具未能启动。\n\n{exc}", parent=root)
        root.destroy()
        raise
    root.mainloop()
