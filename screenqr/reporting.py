"""只读环境报告；不采屏、不读聊天、凭据或机器名称，不联网。"""
from __future__ import annotations
from dataclasses import asdict
import json
from pathlib import Path
import platform
import struct

from . import __version__


def collect(own_hwnd=None):
    from .capture import enable_dpi_awareness, _DisplayApi
    from .environment import probe_desktop, assess_environment, DesktopChanged
    from .profiles import load_profiles
    from .release import executable_checksum
    root=None
    if own_hwnd is None:
        import tkinter as tk
        enable_dpi_awareness()
        root=tk.Tk(); root.withdraw(); root.update_idletasks()
        api=_DisplayApi()
        own_hwnd=int(api.user.GetAncestor(root.winfo_id(),2) or 0)
    try:
        result=dict(version=__version__,platform=platform.system(),os_version=platform.version(),
            architecture_bits=struct.calcsize('P')*8,machine_architecture=platform.machine(),
            screen_captured=False,input_sent=False,network_requests=0,
            executable_sha256=executable_checksum())
        try:
            state=probe_desktop(own_hwnd); result['display']=asdict(state)
        except DesktopChanged:
            state=None; result['display']=None
        assessment=assess_environment(load_profiles(),state)
        result.update(wechat_version=assessment.version,reason=assessment.reason,
            matched_profile=assessment.profile.id if assessment.profile else None,message=assessment.message)
        return result
    finally:
        if root is not None: root.destroy()


def save_report(path, *, own_hwnd=None):
    report=collect(own_hwnd)
    # 不悄悄覆盖已有文件；用户可以选一个新名字保存。
    with Path(path).open('x',encoding='utf-8') as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2)
        stream.write('\n')
    return report


def save_dialog(parent=None, *, own_hwnd=None):
    from datetime import datetime
    import tkinter as tk
    from tkinter import filedialog,messagebox
    owned_root=None
    if parent is None:
        from .capture import enable_dpi_awareness
        enable_dpi_awareness()
        owned_root=tk.Tk(); owned_root.withdraw(); parent=owned_root
    try:
        path=filedialog.asksaveasfilename(parent=parent,title='保存环境报告',
            initialfile='ScreenQR-环境报告-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.json',
            defaultextension='.json',filetypes=[('环境报告','*.json')],confirmoverwrite=False)
        if not path: return False
        try: save_report(path,own_hwnd=own_hwnd)
        except FileExistsError: messagebox.showinfo('换个文件名','这个文件名已存在，请换个名字保存。',parent=parent); return False
        except Exception: messagebox.showerror('没能保存','报告没能保存，请换个能写入的文件夹再试。',parent=parent); return False
        messagebox.showinfo('已保存','环境报告已保存，不含截图、聊天或扫码内容。',parent=parent)
        return True
    finally:
        if owned_root is not None: owned_root.destroy()
