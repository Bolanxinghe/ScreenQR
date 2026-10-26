"""程序内用法、自测与信任说明，普通用户不必阅读外部文件。"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog,ttk

from . import __version__
from .panels import position_dialog
from .release import executable_checksum,verify_checksums

PAPER='#F3F7FA'
INK='#182B3A'
MUTED='#60788B'
FONT=('Microsoft YaHei UI',10)


def sections(app,checksum):
    env=getattr(app,'_environment',None); desktop=getattr(app,'_desktop_state',None)
    version='.'.join(map(str,env.version)) if env is not None and env.version else '暂时读不到'
    display=f'{len(desktop.monitors)} 块屏幕，当前缩放 {desktop.dpi*100//96}%' if desktop else '暂时读不到'
    profiles=getattr(app,'_profiles',None)
    ranges='；'.join(f'微信 {".".join(map(str,p.version))}，单屏 {p.width}×{p.height}，{p.dpi*100//96}% 缩放'
        for p in profiles.profiles) if profiles and profiles.profiles else '微信支持列表没读好，微信扫码先停用'
    message=env.message if env is not None and env.message else '这台电脑的微信在支持列表中，仍需查看它实际打开的页面。'
    return {
        '怎么用':
            '1. 拖动取景框上栏，把完整的码和四周空白放进框里。条形码也能扫。\n\n'
            '2. 网址会先显示出来，看清后点“在浏览器打开”；点“复制”可保留原文。\n\n'
            '3. 微信内容会尝试交给已登录的微信，不用点截图工具。出现微信窗口不代表登录、入群或付款已完成。\n\n'
            '多个码：点列表里的一个，再看清网址后打开；也可以缩小框。相同内容的两张码仍分开列出。微信码请只框一个。\n\n'
            '打开后：程序会等你重新取景，避免继续扫新页面里的码。拖动框或按空格扫下一个。\n\n'
            '历史：顶栏点“历史”看全文、复制或打开网址。最近 100 条只存在这台电脑，可停止保存或清空；关闭保存后，已有记录要另点清空。记录可能含 Wi-Fi 密码和名片，共用电脑建议关闭。\n\n'
            '快捷键：空格暂停或继续，Ctrl+O 选图片，Ctrl+C 复制当前选择，Esc 退出。F1 或顶栏右键打开本页。\n\n'
            '没扫出来：先放大原图，移开遮挡，完整框住四个角和留白。模糊、缺角或屏幕外的部分补不回来。付款码不处理；部分登录码只允许手机摄像头扫。',
        '支持范围':
            '面向 Windows 10（2004 起）/11 的 x64 电脑。普通扫码可在单屏、多屏和常见缩放下使用；其它电脑仍需实际试用。\n\n'
            '跨屏拖动时会先停一下，把框完整移入一块屏幕后重新扫。改缩放、换分辨率、插拔屏幕或唤醒后，请重新框好；程序不会沿用旧位置猜着扫。\n\n'
            f'微信的支持范围较窄：{ranges}。其它版本、多屏或缩放不相符时，微信扫码停用，普通扫码保留。\n\n'
            f'这台电脑\n微信：{version}\n屏幕：{display}\n{message}\n\n'
            '不保证 Windows 7、32 位、ARM64、云电脑、远程桌面。不同显卡、多屏混合缩放、插拔屏幕和睡眠恢复仍需在对应电脑验证。\n\n'
            '受系统保护的画面，例如加密视频、银行安全页、锁屏，可能无法读取。请换回普通桌面；不能只凭画面空白就断定是系统保护。\n\n'
            '不保证所有微信业务可用。过期码、需要手机的登录码或平台不支持的业务，电脑版也可能打不开。程序不会确认登录、入群、付款或领券。',
        '自己检查':
            '换一台电脑，可以按这一页检查，无需安装 Python。\n\n'
            '□ 找一个可信的普通网页码，框住后先看地址。确认前浏览器不应打开，点击后应只打开所选地址一次。\n\n'
            '□ 同屏放两个不同码，程序不应擅自选第一个。点选一个，再核对地址。移动框或换码后，旧的打开按钮应失效。\n\n'
            '□ 找一张清楚的条形码和一张较小的二维码，确认内容正确，复制后核对原文。\n\n'
            '□ 打开历史，退出并重启，记录应还在。取消“保存扫码记录”后扫新内容，不应新增记录。点清空后旧记录不应回来。\n\n'
            '□ 有多块屏幕时，在每块屏幕里分别框扫；跨屏、改缩放、换分辨率或唤醒后应重新取景，不能扫到旧位置。\n\n'
            '□ 如需试微信，只用自己的有效测试码。只核对初始页，随后关闭；不要用付款码，不要确认登录、入群或操作业务。\n\n'
            '□ 遇到问题，点下方“保存环境报告”。报告只有系统、屏幕、程序和微信版本，不含截图、聊天、扫码内容、机器名称或账号。报告留在本机，由你决定是否分享。\n\n'
            '请记下：Windows 版本、缩放、屏幕数量、哪一步不符合预期。未经实际测试的环境都应记为“待验证”。',
        '关于':
            f'ScreenQR {__version__}\n\n文件校验码（SHA-256）\n{checksum or "源码运行时不提供 EXE 校验码；以打包程序为准。"}\n\n'
            '点“检查文件”可与随包清单核对。校验码只说明文件是否相同，不能单独证明安全或发布者身份；请同时与可信发布页上的校验码比对。\n\n'
            '本版尚未做数字签名。Windows 可能提示发布者未知。先核对下载来源和文件；若系统或杀毒软件明确提示威胁，请暂停并核查，不要关闭防护或盲目放行。企业策略也可能禁止运行。\n\n'
            '有效签名能显示发布者，但新版本仍可能出现 Windows 提示，不保证买了证书就立刻消除提示。\n\n'
            '隐私：画面和扫码内容留在本机，不上传。历史可关闭和清空，不保存截图。打开网址后，浏览器会访问你确认的地址；微信收到所选画面后按自己的规则处理。\n\n'
            '原创代码采用 MIT。Python、Pillow、ZXing 和打包工具版本固定，第三方许可随包保留。没有包含 AndroMeld、微信程序、聊天数据或私人码样本。'
    }


class HelpWindow(tk.Toplevel):
    def __init__(self,app,on_close):
        super().__init__(app.finder)
        self.app,self.on_close=app,on_close
        self._queue=queue.SimpleQueue(); self._job=None; self._poll_after=None
        self._stop=threading.Event()
        self.title('ScreenQR · 帮助 / 关于'); self.configure(bg=PAPER)
        self.transient(app.finder); self.attributes('-topmost',True)
        self.minsize(app.finder.px(300),app.finder.px(340))
        self.protocol('WM_DELETE_WINDOW',self.close)
        self.bind('<Escape>',lambda _e:(self.close(),'break')[-1])
        self.book=ttk.Notebook(self); self.book.pack(fill='both',expand=True,padx=6,pady=6)
        self.pages={}
        for title,content in sections(app,executable_checksum()).items():
            frame=tk.Frame(self.book,bg=PAPER); self.book.add(frame,text=title)
            text=tk.Text(frame,wrap='word',font=FONT,bg=PAPER,fg=INK,relief='flat',padx=12,pady=12)
            scroll=tk.Scrollbar(frame,command=text.yview); scroll.pack(side='right',fill='y')
            text.pack(fill='both',expand=True); text.configure(yscrollcommand=scroll.set)
            text.insert('1.0',content); text.configure(state='disabled'); self.pages[title]=text
        actions=tk.Frame(self,bg=PAPER); actions.pack(fill='x',padx=8,pady=4)
        self.save_button=app._button(actions,'保存环境报告',self.save,padx=8,pady=6)
        self.save_button.pack(side='left')
        self.check_button=app._button(actions,'检查文件',self.check,padx=8,pady=6)
        self.check_button.pack(side='right')
        self.status=tk.Label(self,bg=PAPER,fg=MUTED,font=FONT,anchor='w',justify='left',text='报告只保存到你选择的位置，不会自动发送。')
        self.status.pack(fill='x',padx=12,pady=(2,10)); self.bind('<Configure>',self.layout)
        position_dialog(self,app,480,650)

    def layout(self,event):
        if event.widget is self: self.status.configure(wraplength=max(1,self.winfo_width()-24))

    def _run(self,kind,worker):
        if self._job is not None: return
        self.save_button.configure(state='disabled'); self.check_button.configure(state='disabled')
        self.status.configure(text='正在保存报告…' if kind=='report' else '正在核对文件…')
        def target():
            try: self._queue.put((kind,worker(),None))
            except Exception as exc: self._queue.put((kind,None,'exists' if isinstance(exc,FileExistsError) else 'failed'))
        self._job=threading.Thread(target=target,name='ScreenQR-file-check',daemon=True)
        self._job.start(); self._poll_after=self.after(100,self._poll)

    def _poll(self):
        self._poll_after=None
        try: kind,result,error=self._queue.get_nowait()
        except queue.Empty:
            self._poll_after=self.after(100,self._poll); return
        self._job=None
        self.save_button.configure(state='normal'); self.check_button.configure(state='normal')
        if error:
            message='文件名已存在，请换个名字保存。' if error=='exists' else (
                '没能保存报告，请换个能写入的文件夹。' if kind=='report' else '没能核对，请确认使用的是完整发布包。')
        elif kind=='report': message='报告已保存，不含截图、聊天或扫码内容。'
        else: message='文件与随包清单相同；仍请核对可信下载来源。' if result.okay else '这个文件夹与发布包不同，请换用完整发布包核对。'
        self.status.configure(text=message)

    def save(self):
        path=filedialog.asksaveasfilename(parent=self,title='保存环境报告',
            initialfile='ScreenQR-环境报告-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.json',
            defaultextension='.json',filetypes=[('环境报告','*.json')],confirmoverwrite=False)
        if not path: return
        from .reporting import save_report
        hwnd=self.app._own_hwnd
        self._run('report',lambda:save_report(path,own_hwnd=hwnd))

    def check(self):
        root=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
        self._run('check',lambda:verify_checksums(root,cancelled=self._stop.is_set))

    def close(self):
        self._stop.set()
        if self._poll_after is not None:
            self.after_cancel(self._poll_after); self._poll_after=None
        callback,self.on_close=self.on_close,None
        self.destroy()
        if callback: callback()
