"""显式开启的本机公开样本计时；日志不含扫码原文、图像或其指纹。"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import time

MAX_EVENTS=50_000


def sample_text(run_id: str,case: int) -> str:
    if re.fullmatch(r'[0-9A-F]{16}',run_id) is None or type(case) is not int or not 0<=case<100:
        raise ValueError('invalid_public_measurement_sample')
    check=hashlib.sha256(f'{run_id}/{case:03}'.encode('ascii')).hexdigest()[:8].upper()
    return f'SQRT/{run_id}/{case:03}/{check}'


class Measurement:
    def __init__(self,path,run_id):
        sample_text(run_id,0)
        self.run_id=run_id
        self._stream=Path(path).open('x',encoding='utf-8',buffering=1)
        self._count=0; self._failed=False; self._after=None; self._root=None
        self._write(dict(event='started',clock_ns=time.monotonic_ns()))

    def _write(self,row):
        if self._failed or self._count>=MAX_EVENTS: return
        try:
            self._stream.write(json.dumps(row,separators=(',',':'))+'\n')
            self._count+=1
        except OSError:
            self._failed=True

    def _case(self,text):
        if not isinstance(text,str): return None
        parts=text.split('/')
        if len(parts)!=4 or parts[0]!='SQRT' or parts[1]!=self.run_id or not re.fullmatch('[0-9]{3}',parts[2]): return None
        case=int(parts[2])
        return case if case<100 and text==sample_text(self.run_id,case) else None

    def allows(self,instances):
        return all(self._case(item.text) is not None for item in instances)

    def abort(self):
        self._write(dict(event='aborted',clock_ns=time.monotonic_ns(),reason='unexpected_source'))

    def record(self,instances,timings,generation):
        started,captured,decoded=timings
        now=time.monotonic_ns()
        case=self._case(instances[0].text) if len(instances)==1 else None
        expected='QR Code' if case is not None and case<80 else 'Code 128' if case is not None and case<90 else 'Code 39'
        self._write(dict(event='frame',case=case,correct=case is not None and instances[0].format==expected,
            count=len(instances),generation=generation,start_ns=started,captured_ns=captured,
            decoded_ns=decoded,ui_idle_ns=now,capture_ms=(captured-started)/1e6,
            decode_ms=(decoded-captured)/1e6,product_ms=(now-started)/1e6))

    def attach(self,root,app):
        self._root=root
        def pulse():
            self._after=None
            if app._closed or app._closing: return
            self._write(dict(event='heartbeat',clock_ns=time.monotonic_ns(),paused=app.paused.get(),busy=app._busy))
            self._after=root.after(1000,pulse)
        self._after=root.after(1000,pulse)

    def close(self):
        if self._after is not None:
            self._root.after_cancel(self._after); self._after=None
        self._write(dict(event='closed',clock_ns=time.monotonic_ns()))
        self._stream.close()
