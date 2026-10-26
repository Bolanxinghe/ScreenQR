"""构建后生成可公开分发的离线校验表，不进行签名或联网。"""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from screenqr.release import write_checksums,verify_checksums

if __name__=='__main__':
    root=Path(sys.argv[1])
    count=write_checksums(root)
    result=verify_checksums(root)
    if not result.okay: raise SystemExit('Package verification failed')
    print(f'SHA-256 manifest verified: {count} files')
