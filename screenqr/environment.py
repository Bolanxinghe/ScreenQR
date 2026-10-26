"""只读环境事实与屏幕取景保护；不读聊天、不采屏、不修改系统设置。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys
import threading

from .capture import DisplayTopologyError, _DisplayApi, _MONITORINFOEXW, _DEVMODEW
from .profiles import NativeProfile, ProfileRegistry


class DesktopChanged(RuntimeError):
    pass


class DisplaySignals:
    """只监听本程序窗口消息；回调只改代次，不在 Win32 回调里操作 Tk。"""
    def __init__(self, hwnd):
        self._lock=threading.Lock(); self._epoch=0; self._hwnd=hwnd
        self._library=ctypes.WinDLL('comctl32',use_last_error=True)
        result=ctypes.c_ssize_t; uintptr=ctypes.c_size_t
        callback_type=ctypes.WINFUNCTYPE(result,wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM,uintptr,uintptr)
        library=self._library
        library.SetWindowSubclass.argtypes=[wintypes.HWND,callback_type,uintptr,uintptr]
        library.SetWindowSubclass.restype=wintypes.BOOL
        library.RemoveWindowSubclass.argtypes=[wintypes.HWND,callback_type,uintptr]
        library.RemoveWindowSubclass.restype=wintypes.BOOL
        library.DefSubclassProc.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
        library.DefSubclassProc.restype=result
        def callback(window,message,wparam,lparam,subclass_id,reference):
            try:
                if message in (0x007e,0x02e0,0x001a) or (message==0x0218 and wparam in (4,6,7,18)):
                    with self._lock: self._epoch+=1
            finally:
                return library.DefSubclassProc(window,message,wparam,lparam)
        self._callback=callback_type(callback)
        self._id=0x53515250
        if not library.SetWindowSubclass(hwnd,self._callback,self._id,0):
            raise DesktopChanged('display_notification_unavailable')

    @property
    def epoch(self):
        with self._lock: return self._epoch

    def close(self):
        if self._hwnd:
            if not self._library.RemoveWindowSubclass(self._hwnd,self._callback,self._id):
                raise DesktopChanged('display_notification_cleanup_failed')
            self._hwnd=None


@dataclass(frozen=True, slots=True)
class MonitorState:
    rect: tuple[int,int,int,int]
    work: tuple[int,int,int,int]
    primary: bool
    rotation: int


@dataclass(frozen=True, slots=True)
class DesktopState:
    virtual: tuple[int,int,int,int]
    monitors: tuple[MonitorState,...]
    own_monitor: tuple[int,int,int,int]
    dpi: int
    interactive: bool = True

    def permits(self,bbox):
        """跨屏区域先停下，完整移入一块屏幕后重新映射；不推测另一屏缩放。"""
        return (self.interactive and type(bbox) is tuple and len(bbox)==4
            and all(type(v) is int for v in bbox)
            and self.own_monitor[0] <= bbox[0] < bbox[2] <= self.own_monitor[2]
            and self.own_monitor[1] <= bbox[1] < bbox[3] <= self.own_monitor[3])

    @property
    def primary_rect(self):
        found=tuple(m.rect for m in self.monitors if m.primary)
        return found[0] if len(found)==1 else None


def _interactive(api):
    user=api.user
    user.OpenInputDesktop.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    user.OpenInputDesktop.restype=wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
    user.GetUserObjectInformationW.restype=wintypes.BOOL
    user.CloseDesktop.argtypes=[wintypes.HANDLE]; user.CloseDesktop.restype=wintypes.BOOL
    desktop=user.OpenInputDesktop(0,False,1)
    if not desktop: return False
    try:
        buffer=ctypes.create_unicode_buffer(256); needed=wintypes.DWORD()
        return bool(user.GetUserObjectInformationW(desktop,2,buffer,ctypes.sizeof(buffer),ctypes.byref(needed))
            and 0<needed.value<=ctypes.sizeof(buffer) and buffer.value.casefold()=='default')
    finally:
        if not user.CloseDesktop(desktop): raise DesktopChanged('desktop_query_cleanup_failed')


def probe_desktop(own_hwnd: int) -> DesktopState:
    """每次用真实自有窗口 DPI；枚举前后复核，不缓存旧屏幕坐标。"""
    if sys.platform!='win32': raise DesktopChanged('windows_required')
    try:
        api=_DisplayApi(); context=api.require_thread_pm()
        virtual=api.virtual_bbox(); count=int(api.user.GetSystemMetrics(80))
        if not 1<=count<=64: raise DesktopChanged('monitor_count_unknown')
        own,dpi=api.own_window_dpi_evidence(own_hwnd)
        interactive=_interactive(api)
        found=[]; errors=[]; own_rect=None; seen=set()
        def visit(raw,dc,callback_rect,data):
            nonlocal own_rect
            try:
                handle=int(raw or 0)
                if not handle or handle in seen: raise DesktopChanged('monitor_identity_unknown')
                seen.add(handle)
                info=_MONITORINFOEXW(); info.cbSize=ctypes.sizeof(info)
                if not api.user.GetMonitorInfoW(handle,ctypes.byref(info)): raise DesktopChanged('monitor_bounds_unknown')
                rect=tuple(int(getattr(info.rcMonitor,key)) for key in ('left','top','right','bottom'))
                work=tuple(int(getattr(info.rcWork,key)) for key in ('left','top','right','bottom'))
                if (not callback_rect or rect!=tuple(int(getattr(callback_rect.contents,key)) for key in ('left','top','right','bottom'))
                    or info.dwFlags & ~1): raise DesktopChanged('monitor_changed')
                mode=_DEVMODEW(); mode.dmSize=ctypes.sizeof(mode)
                if not api.user.EnumDisplaySettingsW(str(info.szDevice),0xffffffff,ctypes.byref(mode)):
                    raise DesktopChanged('display_mode_unknown')
                if mode.dmFields & 0x1800a0 != 0x1800a0: raise DesktopChanged('display_mode_incomplete')
                mode_rect=(int(mode.mode.display.dmPosition.x),int(mode.mode.display.dmPosition.y),
                    int(mode.mode.display.dmPosition.x+mode.dmPelsWidth),int(mode.mode.display.dmPosition.y+mode.dmPelsHeight))
                rotation=int(mode.mode.display.dmDisplayOrientation)
                if mode_rect!=rect or rotation not in (0,1,2,3): raise DesktopChanged('display_mode_changed')
                found.append(MonitorState(rect,work,bool(info.dwFlags & 1),rotation*90))
                if handle==own: own_rect=rect
                return True
            except Exception:
                errors.append('display_query_incomplete'); return False
        callback=api.monitor_callback_type(visit)
        complete=api.user.EnumDisplayMonitors(None,None,callback,0)
        if (not complete or errors or len(found)!=count or own_rect is None
                or api.virtual_bbox()!=virtual or int(api.user.GetSystemMetrics(80))!=count
                or api.own_window_dpi_evidence(own_hwnd)!=(own,dpi)
                or not api.user.AreDpiAwarenessContextsEqual(context,api.require_thread_pm())
                or _interactive(api)!=interactive):
            raise DesktopChanged('display_query_changed')
        result=DesktopState(virtual,tuple(sorted(found,key=lambda m:m.rect)),own_rect,dpi,interactive)
        if result.primary_rect is None: raise DesktopChanged('primary_monitor_unknown')
        return result
    except (OSError,AttributeError,DisplayTopologyError) as exc:
        raise DesktopChanged('display_query_unavailable') from None


@dataclass(frozen=True, slots=True)
class RuntimeAssessment:
    profile: NativeProfile | None
    version: tuple[int,int,int,int] | None
    reason: str
    message: str


def assess_environment(registry: ProfileRegistry, desktop: DesktopState | None, *, version_reader=None) -> RuntimeAssessment:
    """版本只读自内置允许路径；结果只控制微信开关，不影响普通扫码。"""
    if registry.error or not registry.profiles:
        return RuntimeAssessment(None,None,'configuration','微信配置没读好，普通码照常能扫。')
    if desktop is None or not desktop.interactive:
        return RuntimeAssessment(None,None,'display','暂时读不到屏幕，换回普通桌面再扫。')
    try:
        if version_reader is None:
            from .native_capture import _Gdi
            version_reader=_Gdi().fixed_version
        version=version_reader(registry.profiles[0].executable)
        if type(version) is not tuple or len(version)!=4 or any(type(v) is not int for v in version):
            raise ValueError('invalid_version')
    except Exception:
        return RuntimeAssessment(None,None,'unavailable','没找到可用的微信，普通码照常能扫。')
    supported=registry.match(version,desktop.primary_rect,desktop.dpi,len(desktop.monitors))
    if (supported is not None and desktop.virtual==supported.screen and desktop.own_monitor==supported.screen
            and all(m.rect==supported.screen and m.rotation==0 for m in desktop.monitors)):
        return RuntimeAssessment(supported,version,'matched','')
    reason='version' if not any(p.version==version for p in registry.profiles) else 'display'
    message=('这个微信版本暂不能扫，普通码照常能扫。' if reason=='version'
        else '这套屏幕设置暂不能用微信扫码，普通码照常能扫。')
    return RuntimeAssessment(None,version,reason,message)


def capture_checked(bbox, own_hwnd, expected, *, capture=None, probe=None):
    """取像前后都核对同一屏幕事实；变化即丢弃，不能发布旧画面。"""
    from .capture import capture_region
    probe=probe or probe_desktop; capture=capture or capture_region
    before=probe(own_hwnd)
    if before!=expected or not before.permits(bbox): raise DesktopChanged('source_display_changed')
    image=capture(bbox)
    try:
        if probe(own_hwnd)!=before: raise DesktopChanged('display_changed_across_capture')
        return image
    except BaseException:
        image.close(); raise
