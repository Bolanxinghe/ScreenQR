"""在真实桌面展示公开样本，启动实际 EXE；不模拟产品界面、不发送输入。

开发验收工具，不随用户便携包分发。控制文件只控制此公开展示板。
"""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime,timezone
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import tkinter as tk
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image,ImageTk
import qrcode
import zxingcpp
from screenqr.capture import enable_dpi_awareness
from screenqr.measurement import sample_text
from screenqr.release import file_sha256


def percentile(values,p):
    if not values: return None
    values=sorted(values); index=(len(values)-1)*p
    lo,hi=math.floor(index),math.ceil(index)
    return values[lo]+(values[hi]-values[lo])*(index-lo)


class ProcessStats:
    class Memory(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in
            ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage',
             'QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage','PrivateUsage')]

    def __init__(self,pid):
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.psapi=ctypes.WinDLL('psapi',use_last_error=True)
        self.kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        self.kernel.OpenProcess.restype=wintypes.HANDLE
        self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        self.psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(self.Memory),wintypes.DWORD]
        self.kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
        self.handle=self.kernel.OpenProcess(0x1000|0x10,False,pid)
        if not self.handle: raise OSError('process_query_failed')

    def read(self):
        memory=self.Memory(); memory.cb=ctypes.sizeof(memory)
        times=[wintypes.FILETIME() for _ in range(4)]
        if not self.psapi.GetProcessMemoryInfo(self.handle,ctypes.byref(memory),memory.cb): raise OSError('memory_query_failed')
        if not self.kernel.GetProcessTimes(self.handle,*(ctypes.byref(v) for v in times)): raise OSError('cpu_query_failed')
        cpu=sum((v.dwHighDateTime<<32)+v.dwLowDateTime for v in times[2:])/10_000_000
        return dict(private_bytes=memory.PrivateUsage,working_set_bytes=memory.WorkingSetSize,cpu_seconds=cpu)

    def close(self):
        if self.handle: self.kernel.CloseHandle(self.handle); self.handle=None


class Suite:
    def __init__(self,args):
        self.args=args; self.output=Path(args.output); self.output.mkdir(parents=True,exist_ok=False)
        self.run=uuid4().hex[:16].upper()
        self.control=self.output/'control.json'; self.metrics=self.output/'product-timings.jsonl'
        self.progress=self.output/'progress.json'
        self.control.write_text('{"action":"idle"}',encoding='utf-8')
        self._last_control=''; self.offset=0; self.partial=b''
        self.state='idle'; self.case=0; self.cycles=[]; self.soak=[]; self.pulses=[]; self.resources=[]
        self.process=None; self.stats=None; self.stderr=None; self.poll_cpu_ns=0
        self._next_resource=0; self._next_progress=0; self.started_utc=None; self.soak_start=None
        self.cycles_poll_ns=0; self._last_soak_frame_ns=0
        enable_dpi_awareness()
        self.root=tk.Tk(); self.root.title('ScreenQR · 公开样本稳定性验收')
        self.root.attributes('-topmost',True); self.root.configure(bg='white')
        scale=max(1.0,float(self.root.tk.call('tk','scaling'))/(96/72))
        sw,sh=self.root.winfo_screenwidth(),self.root.winfo_screenheight()
        fw,fh=min(round(420*scale),sw-32),min(round(620*scale),sh-80)
        fx,fy=max(8,(sw-fw)//2),max(8,(sh-fh)//2-30)
        width,height=min(sw,fw+128),min(sh,fh+200)
        x,y=max(0,fx-64),max(0,fy-180)
        self.root.geometry(f'{width}x{height}+{x}+{y}')
        self.title=tk.Label(self.root,text='公开测试样本 · 不发送消息、不打开业务',font=('Microsoft YaHei UI',10),bg='white')
        self.title.place(x=12,y=15)
        self.label=tk.Label(self.root,bg='white',bd=0,highlightthickness=0)
        self.root.update_idletasks()
        self.cx=fx+fw//2-self.root.winfo_rootx()
        self.cy=fy+round(44*scale)+(fh-round(288*scale))//2-self.root.winfo_rooty()
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.show(0); self.write_progress(); self.root.after(40,self.tick)

    def show(self,case):
        began=time.monotonic_ns()
        text=sample_text(self.run,case)
        if case<80:
            code=qrcode.QRCode(box_size=(1,2,4,6)[case%4],border=4)
            code.add_data(text); code.make(fit=True); image=code.make_image().convert('RGB')
            if case%8==5:
                rotated=image.rotate(90,expand=True); image.close(); image=rotated
        else:
            raw=zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(text,zxingcpp.Code128 if case<90 else zxingcpp.Code39),scale=1)
            image=Image.frombytes('L',(raw.shape[1],raw.shape[0]),bytes(raw)).convert('RGB')
        with image:
            photo=ImageTk.PhotoImage(image,master=self.root)
            self.label.place(x=self.cx-image.width//2,y=self.cy-image.height//2)
        self.label.configure(image=photo); self.label.image=photo
        self.root.update_idletasks()
        self.visible_ns=time.monotonic_ns()
        self.setup_ms=(self.visible_ns-began)/1e6
        self.case=case

    def start(self):
        self.show(0); self.state='cycles'
        self.started_utc=datetime.now(timezone.utc).isoformat()
        env=os.environ.copy(); env.pop('PYTHONPATH',None); env.pop('PYTHONHOME',None)
        env['LOCALAPPDATA']=str(self.output/'isolated-user')
        self.stderr=(self.output/'product-stderr.log').open('wb')
        self.process=subprocess.Popen([self.args.exe,'--measurement-file',str(self.metrics),'--measurement-run',self.run,
            '--diagnostics-file',str(self.output/'native-diagnostic.json')],env=env,cwd=self.output,
            stdout=subprocess.DEVNULL,stderr=self.stderr,creationflags=subprocess.CREATE_NO_WINDOW)
        self.stats=ProcessStats(self.process.pid)

    def read_events(self):
        began=time.monotonic_ns()
        if not self.metrics.exists(): return []
        with self.metrics.open('rb') as stream:
            stream.seek(self.offset); data=stream.read(); self.offset+=len(data)
        parts=(self.partial+data).split(b'\n'); self.partial=parts.pop()
        events=[json.loads(row) for row in parts if row]
        self.poll_cpu_ns+=time.monotonic_ns()-began
        return events

    def write_progress(self):
        now=time.monotonic_ns()
        if now<self._next_progress: return
        self._next_progress=now+1_000_000_000
        elapsed=(time.monotonic_ns()-self.soak_start)/1e9 if self.soak_start else 0
        row=dict(state=self.state,completed_cycles=len(self.cycles),soak_elapsed_seconds=round(elapsed,2),
            soak_required_seconds=self.args.seconds,valid_soak_frames=len(self.soak),product_pid=self.process.pid if self.process else None)
        self.progress.write_text(json.dumps(row,indent=2),encoding='utf-8')

    def tick(self):
        try:
            control=self.control.read_text(encoding='utf-8')
            if control!=self._last_control:
                self._last_control=control; command=json.loads(control)
                if command['action']=='start' and self.state=='idle': self.start()
                elif command['action']=='case' and self.state=='idle': self.show(command['case'])
                elif command['action']=='close': self.close(); return
            if self.state in ('cycles','soak'):
                if self.process.poll() is not None: self.finish('product_exited')
                else:
                    for event in self.read_events():
                        if event['event']=='aborted': self.finish('unexpected_source'); break
                        if event['event']=='heartbeat': self.pulses.append(event); continue
                        if event['event']!='frame': continue
                        if self.state=='cycles' and event['case']==self.case and event['start_ns']>=self.visible_ns:
                            if not event['correct']: self.finish('incorrect_result'); break
                            now=time.monotonic_ns()
                            self.cycles.append(dict(case=self.case,product_ms=event['product_ms'],capture_ms=event['capture_ms'],
                                decode_ms=event['decode_ms'],wait_to_capture_ms=(event['start_ns']-self.visible_ns)/1e6,
                                harness_tail_ms=(now-event['ui_idle_ns'])/1e6,observed_end_to_end_ms=(now-self.visible_ns)/1e6,
                                harness_sample_setup_ms=self.setup_ms))
                            if self.case==99:
                                self.cycles_poll_ns=self.poll_cpu_ns
                                self.state='soak'; self.show(42); self.soak_start=self.visible_ns
                                self._last_soak_frame_ns=self.soak_start
                                self._next_resource=0
                            else: self.show(self.case+1)
                        elif self.state=='soak' and event['start_ns']>=self.soak_start:
                            if not event['correct']: self.finish('incorrect_soak_result'); break
                            self.soak.append(event)
                            self._last_soak_frame_ns=event['ui_idle_ns']
                    if self.state=='cycles' and (time.monotonic_ns()-self.visible_ns)>60_000_000_000:
                        self.finish('harness_case_watchdog_no_result')
                    if self.state=='soak':
                        now=time.monotonic_ns(); elapsed=(now-self.soak_start)/1e9
                        if now>=self._next_resource:
                            self.resources.append(dict(elapsed_seconds=elapsed,**self.stats.read()))
                            self._next_resource=now+1_000_000_000
                        recent=self.pulses[-1] if self.pulses and self.pulses[-1]['clock_ns']>=self.soak_start else None
                        if recent and (now-recent['clock_ns'])>10_000_000_000: self.finish('ui_heartbeat_stalled')
                        elif recent and recent['paused']: self.finish('product_paused_or_environment_interrupted')
                        elif now-self._last_soak_frame_ns>10_000_000_000: self.finish('capture_stalled_or_source_hidden')
                        elif elapsed>=self.args.seconds: self.finish(None)
            self.write_progress()
        except Exception:
            if self.state not in ('completed','failed'): self.finish('harness_error')
        self.root.after(40,self.tick)

    def finish(self,reason):
        if self.state in ('completed','failed'): return
        elapsed=(time.monotonic_ns()-self.soak_start)/1e9 if self.soak_start else 0
        rows=self.cycles
        pulse_times=[p['clock_ns'] for p in self.pulses if self.soak_start and p['clock_ns']>=self.soak_start]
        gap=max(((b-a)/1e9 for a,b in zip(pulse_times,pulse_times[1:])),default=0)
        early=[r['private_bytes'] for r in self.resources if 60<=r['elapsed_seconds']<120]
        late=[r['private_bytes'] for r in self.resources if r['elapsed_seconds']>=max(120,elapsed-60)]
        growth=statistics.median(late)-statistics.median(early) if early and late else None
        p90=percentile([r['product_ms'] for r in rows],.9)
        e2e_p90=percentile([r['observed_end_to_end_ms'] for r in rows],.9)
        native=(self.output/'native-diagnostic.json').exists()
        self.state='completed' if reason is None else 'failed'
        passed=(reason is None and len(rows)==100 and elapsed>=self.args.seconds and self.args.seconds>=1800
                and p90 is not None and p90<=1000 and e2e_p90 is not None and e2e_p90<=2000
                and gap<=5 and growth is not None and growth<=50*1024*1024
                and len(self.soak)>=self.args.seconds*.5 and not native)
        report=dict(executable_sha256=file_sha256(Path(self.args.exe)),started_utc=self.started_utc,state=self.state,
            passed=passed,reason=reason,thresholds=dict(cycles_correct=100,product_p90_ms=1000,observed_e2e_p90_ms=2000,
                soak_seconds=1800,max_ui_heartbeat_gap_seconds=5,max_private_growth_bytes=50*1024*1024,min_soak_hz=.5),
            cycles=dict(completed=len(rows),correct=len(rows),success_rate=len(rows)/100,
                product_p50_ms=percentile([r['product_ms'] for r in rows],.5),product_p90_ms=p90,
                decoder_p50_ms=percentile([r['decode_ms'] for r in rows],.5),decoder_p90_ms=percentile([r['decode_ms'] for r in rows],.9),
                framework_observed_e2e_p50_ms=percentile([r['observed_end_to_end_ms'] for r in rows],.5),
                framework_observed_e2e_p90_ms=e2e_p90,
                harness_tail_p50_ms=percentile([r['harness_tail_ms'] for r in rows],.5),
                harness_tail_p90_ms=percentile([r['harness_tail_ms'] for r in rows],.9),
                wait_to_capture_p50_ms=percentile([r['wait_to_capture_ms'] for r in rows],.5),
                harness_sample_setup_total_ms=sum(r['harness_sample_setup_ms'] for r in rows),
                harness_poll_active_ms=(self.cycles_poll_ns or self.poll_cpu_ns)/1e6),
            soak=dict(elapsed_seconds=elapsed,valid_frames=len(self.soak),max_ui_heartbeat_gap_seconds=gap,
                product_p50_ms=percentile([r['product_ms'] for r in self.soak],.5),product_p90_ms=percentile([r['product_ms'] for r in self.soak],.9),
                private_growth_bytes=growth,process_resources=self.resources),
            product_timing_definition='capture start to Tk idle after result publication; excludes measurement file write; not physical monitor photon time',
            framework_e2e_definition='public fixture display commit to harness observation; includes scan cadence and polling',
            cycle_success_definition='100 samples reach their correct visible result; not a claim that every first camera frame succeeds',
            harness_total_poll_active_ms=self.poll_cpu_ns/1e6,
            native_or_browser_business_tested=False,native_diagnostic_created=native,QR_text_or_screenshots_in_metrics=False)
        (self.output/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        (self.output/'cycle-latencies.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
        self.title.configure(text='测试完成，等待收起展示板。' if self.state=='completed' else '测试已停止，结果已保存。')

    def close(self):
        if self.state in ('cycles','soak'): self.finish('user_closed_fixture')
        if self.stats: self.stats.close()
        if self.stderr: self.stderr.close()
        self.root.destroy()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--exe',required=True); parser.add_argument('--output',required=True)
    parser.add_argument('--seconds',type=int,default=1800)
    args=parser.parse_args()
    if args.seconds<1: parser.error('seconds must be positive')
    suite=Suite(args); suite.root.mainloop()
