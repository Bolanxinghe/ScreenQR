"""低频历史页：默认不占主页面；网络动作仍交回应用的内容门禁。"""
from __future__ import annotations

import tkinter as tk
from .core import browser_url_for_result
from .presentation import result_line

PAPER = '#F3F7FA'
INK = '#182B3A'
MUTED = '#60788B'
FONT = ('Microsoft YaHei UI',10)


def position_dialog(window, app, width, height):
    from .app import _position
    p = app.finder.px
    state = getattr(app, '_desktop_state', None)
    area = next((m.work for m in state.monitors if m.rect == state.own_monitor), None) if state else None
    if area is None:
        area = (0,0,app.finder.winfo_screenwidth(),app.finder.winfo_screenheight())
    w,h = min(p(width),max(p(260),area[2]-area[0]-p(24))), min(p(height),max(p(240),area[3]-area[1]-p(60)))
    x = max(area[0],min(app.finder.winfo_rootx()+p(12),area[2]-w))
    y = max(area[1],min(app.finder.winfo_rooty()+p(12),area[3]-h-p(32)))
    _position(window,x,y,w,h)


class HistoryPanel(tk.Toplevel):
    def __init__(self, app, on_close):
        super().__init__(app.finder)
        self.app, self.store, self.on_close = app, app._history, on_close
        self._ids = ()
        self._entry = None
        self.title('ScreenQR · 历史记录')
        self.transient(app.finder); self.attributes('-topmost',True)
        self.configure(bg=PAPER); self.minsize(app.finder.px(280),app.finder.px(340))
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.bind('<Escape>',lambda _e:self.close())
        self.bind('<Control-c>',lambda _e:self.copy())
        self.rowconfigure(2,weight=2); self.rowconfigure(3,weight=3); self.columnconfigure(0,weight=1)
        self.heading=tk.Label(self,text='只在这台电脑保存，最近 100 条。',bg=PAPER,fg=MUTED,
                 font=FONT,wraplength=app.finder.px(420),justify='left',anchor='w')
        self.heading.grid(row=0,column=0,sticky='ew',padx=12,pady=(10,4))
        settings=tk.Frame(self,bg=PAPER); settings.grid(row=1,column=0,sticky='ew',padx=8)
        self.enabled=tk.BooleanVar(value=self.store.enabled)
        tk.Checkbutton(settings,text='保存扫码记录',variable=self.enabled,command=self.toggle,
                       font=FONT,bg=PAPER,fg=INK).pack(side='left')
        app._button(settings,'清空记录',self.clear,padx=8,pady=4).pack(side='right')
        list_frame=tk.Frame(self,bg=PAPER); list_frame.grid(row=2,column=0,sticky='nsew',padx=12,pady=8)
        self.list=tk.Listbox(list_frame,exportselection=False,font=FONT,activestyle='dotbox',
                             bg='white',fg=INK,bd=0,highlightthickness=1,selectmode='browse')
        scroll=tk.Scrollbar(list_frame,command=self.list.yview)
        scroll.pack(side='right',fill='y'); self.list.pack(side='left',fill='both',expand=True)
        self.list.configure(yscrollcommand=scroll.set); self.list.bind('<<ListboxSelect>>',self.select)
        details=tk.Frame(self,bg=PAPER); details.grid(row=3,column=0,sticky='nsew',padx=12)
        self.text=tk.Text(details,wrap='word',font=FONT,bg='white',fg=INK,relief='flat',height=5)
        scroll_text=tk.Scrollbar(details,command=self.text.yview)
        scroll_text.pack(side='right',fill='y'); self.text.pack(side='left',fill='both',expand=True)
        self.text.configure(yscrollcommand=scroll_text.set,state='disabled')
        actions=tk.Frame(self,bg=PAPER); actions.grid(row=4,column=0,sticky='ew',padx=12,pady=8)
        self.copy_button=app._button(actions,'复制',self.copy,padx=10,pady=6)
        self.copy_button.pack(side='right')
        self.open_button=app._button(actions,'在浏览器打开',self.open,primary=True,padx=10,pady=6)
        self.open_button.pack(side='left')
        self.note=tk.Label(self,bg=PAPER,fg=MUTED,font=FONT,justify='left',anchor='w',wraplength=270)
        self.note.grid(row=5,column=0,sticky='ew',padx=12,pady=(0,10))
        self.bind('<Configure>',self.layout)
        self.refresh()
        position_dialog(self,app,460,620)

    def layout(self,event):
        if event.widget is self:
            self.note.configure(wraplength=max(1,self.winfo_width()-24))
            self.heading.configure(wraplength=max(1,self.winfo_width()-24))

    def refresh(self):
        self._ids=tuple(e.id for e in self.store.entries)
        self.list.delete(0,'end')
        for entry in self.store.entries:
            self.list.insert('end',f'{entry.time[5:10]} {entry.time[11:19]}  {result_line(entry.result)}')
        self.note.configure(text=('记录暂时存不了，扫码照常用。' if self.store.error else
            '记录里可能有 Wi-Fi 密码或名片；共用电脑可以关闭记录。' if self.store.enabled else
            '已停止保存新记录。已有记录仍在，可点“清空记录”。'))
        self._entry=None
        self._show('点一条记录查看全文。' if self._ids else '还没有记录。扫到的内容会出现在这里。')
        self.copy_button.configure(state='disabled'); self.open_button.configure(state='disabled')

    def _show(self,text):
        self.text.configure(state='normal'); self.text.delete('1.0','end')
        self.text.insert('1.0',text); self.text.configure(state='disabled')

    def select(self,_event=None):
        indices=self.list.curselection()
        self._entry=self.store.get(self._ids[indices[0]]) if len(indices)==1 and indices[0]<len(self._ids) else None
        entry=self._entry
        self._show(entry.text if entry else '这条记录已不在了。')
        self.copy_button.configure(state='normal' if entry else 'disabled')
        self.open_button.configure(state='normal' if entry and browser_url_for_result(entry.result) else 'disabled')

    def copy(self):
        entry=self.store.get(self._entry.id) if self._entry else None
        if entry is None: return 'break'
        try:
            self.clipboard_clear(); self.clipboard_append(entry.text)
            self.note.configure(text='已复制。')
        except tk.TclError:
            self.note.configure(text='暂时复制不了，请稍后再试。')
        return 'break'

    def open(self):
        if self._entry:
            self.app.open_history_entry(self._entry.id)

    def clear(self):
        okay=self.store.clear(); self.refresh()
        self.note.configure(text='记录已清空。' if okay else '没能清空磁盘上的记录，请检查这个用户文件夹能否写入。')

    def toggle(self):
        okay=self.store.set_enabled(self.enabled.get()); self.refresh()
        if not okay: self.note.configure(text='这次设置没存好，重启后请再检查。扫码照常用。')

    def close(self):
        callback,self.on_close=self.on_close,None
        self.destroy()
        if callback: callback()
