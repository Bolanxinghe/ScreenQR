"""便携包优先调用同包 EXE；源码目录中也可用 Python 只读检查。"""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]

if __name__=='__main__' and (ROOT/'ScreenQR.exe').is_file():
    import subprocess
    raise SystemExit(subprocess.run([str(ROOT/'ScreenQR.exe'),'--save-environment-report']).returncode)

sys.path.insert(0,str(ROOT))
from screenqr.reporting import collect

if __name__=='__main__':
    import json
    print(json.dumps(collect(),ensure_ascii=True,indent=2))
