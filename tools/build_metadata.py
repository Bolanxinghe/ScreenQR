"""离线生成本次构建事实；不把历史真机报告冒充当前构建验收。"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import importlib.metadata
import json
import marshal
import os
from pathlib import Path
import re
import sys
from types import CodeType

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from screenqr import __version__
from screenqr.release import file_sha256


def normalized(code):
    return code.replace(co_filename='',co_consts=tuple(normalized(c) if isinstance(c,CodeType) else c for c in code.co_consts))


def frozen_source_check(source,exe):
    from PyInstaller.archive.readers import CArchiveReader
    archive=CArchiveReader(str(exe)); pyz=archive.open_embedded_archive('PYZ.pyz')
    verified=[]; unused=[]; hashes={}
    for path in sorted((source/'screenqr').glob('*.py')):
        name='screenqr' if path.stem=='__init__' else 'screenqr.'+path.stem
        hashes[path.relative_to(source).as_posix()]=file_sha256(path)
        if name not in pyz.toc:
            unused.append(name); continue
        expected=compile(path.read_bytes(),str(path),'exec',dont_inherit=True,optimize=0)
        if normalized(pyz.extract(name))!=normalized(expected): raise ValueError('frozen_source_mismatch:'+name)
        verified.append(name)
    if set(verified)!={name for name in pyz.toc if name=='screenqr' or name.startswith('screenqr.')}:
        raise ValueError('unexpected_frozen_module')
    entry=source/'main.py'
    if normalized(marshal.loads(archive.extract('main')))!=normalized(compile(entry.read_bytes(),str(entry),'exec',dont_inherit=True,optimize=0)):
        raise ValueError('frozen_entry_mismatch')
    hashes['main.py']=file_sha256(entry)
    return dict(frozen_runtime_modules_equal_source=verified,main_equal_source=True,
                unused_source_modules_not_bundled=unused,source_sha256=hashes)


def generate(source,package,log):
    raw=log.read_bytes()
    text=raw.decode('utf-16' if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig',errors='replace')
    matches=re.findall(r'Ran (\d+) tests? in ([\d.]+)s\s+OK(?:\s|$)',text)
    if len(matches)!=1: raise ValueError('unambiguous_successful_test_log_required')
    count,seconds=matches[0]
    if re.search(r'FAILED \(|^FAIL:|^ERROR:',text,re.M): raise ValueError('failed_tests_in_log')
    exe=package/'ScreenQR.exe'
    equality=frozen_source_check(source,exe)
    profile='assets/native-profiles.json'
    if (source/profile).read_bytes()!=(package/'_internal'/profile).read_bytes(): raise ValueError('profile_copy_mismatch')
    commit=os.environ.get('GITHUB_SHA','')
    return dict(schema_version=1,version=__version__,metadata_generated_utc=datetime.now(timezone.utc).isoformat(),
        platform='Windows x64',sha256_exe=file_sha256(exe),signature='unsigned',
        source_checks=dict(passed=int(count),seconds=float(seconds),log_sha256=file_sha256(log)),
        runtime=dict(Python=sys.version.split()[0],**{p:importlib.metadata.version(p) for p in ('Pillow','zxing-cpp','PyInstaller')}),
        **equality,profile_sha256=file_sha256(source/profile),
        source_commit=commit if re.fullmatch('[0-9a-fA-F]{40}',commit) else None,
        github_provenance='pending separate CI attestation; this JSON is not a signature',
        real_desktop_acceptance='not performed by build.ps1; see version-specific report and its EXE hash',
        public_samples='format and image tests are not valid business credentials',
        privacy=dict(screenshots_in_metadata=False,QR_contents_in_metadata=False,network_requests=0))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('package',type=Path); parser.add_argument('--test-log',type=Path,required=True)
    args=parser.parse_args(); source=Path(__file__).resolve().parents[1]
    result=generate(source,args.package,args.test_log)
    output=args.package/'docs/build-info.json'; output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(dict(version=result['version'],source_checks=result['source_checks']['passed'],
        runtime_modules=len(result['frozen_runtime_modules_equal_source'])+1,sha256_exe=result['sha256_exe'])))
