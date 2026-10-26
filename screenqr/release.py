"""离线文件校验；只判断是否相同，不代替发布者签名或病毒检查。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
from concurrent.futures import CancelledError

MANIFEST = 'SHA256SUMS.txt'
MAX_MANIFEST_BYTES = 2_000_000
MAX_FILES = 5000


def file_sha256(path: Path, *, cancelled=None) -> str:
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            if cancelled is not None and cancelled(): raise CancelledError()
            digest.update(block)
    return digest.hexdigest()


def executable_checksum() -> str | None:
    if not getattr(sys,'frozen',False): return None
    try: return file_sha256(Path(sys.executable))
    except OSError: return None


def _relative_name(name: str) -> PurePosixPath:
    if (not name or '\\' in name or ':' in name or '\x00' in name
            or any(ord(c)<32 for c in name)):
        raise ValueError('invalid_manifest_path')
    path=PurePosixPath(name)
    if (path.is_absolute() or path.as_posix()!=name or name==MANIFEST
            or any(part in ('.','..') or part.endswith((' ','.')) for part in path.parts)):
        raise ValueError('invalid_manifest_path')
    return path


def _files(root: Path) -> dict[str,Path]:
    root=root.resolve(strict=True)
    result={}
    for path in root.rglob('*'):
        if path.is_symlink(): raise ValueError('linked_package_file')
        if not path.is_file() or path.name==MANIFEST and path.parent==root: continue
        path.resolve(strict=True).relative_to(root)
        name=path.relative_to(root).as_posix()
        _relative_name(name)
        if name.casefold() in result: raise ValueError('duplicate_package_path')
        result[name.casefold()]=path
        if len(result)>MAX_FILES: raise ValueError('too_many_package_files')
    return result


def write_checksums(root: Path) -> int:
    root=Path(root).resolve(strict=True)
    files=_files(root)
    if not files: raise ValueError('empty_package')
    lines=[f'{file_sha256(path)}  {path.relative_to(root).as_posix()}\n'
           for _,path in sorted(files.items())]
    data=''.join(lines).encode('utf-8')
    if len(data)>MAX_MANIFEST_BYTES: raise ValueError('manifest_too_large')
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=root,prefix='.checksums-',suffix='.tmp',delete=False) as stream:
            temporary=Path(stream.name); stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,root/MANIFEST); temporary=None
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)
    return len(files)


@dataclass(frozen=True,slots=True)
class FileCheck:
    checked: int
    changed: int
    missing: int
    extra: int

    @property
    def okay(self):
        return self.changed==self.missing==self.extra==0 and self.checked>0


def verify_checksums(root: Path, *, cancelled=None) -> FileCheck:
    root=Path(root).resolve(strict=True)
    manifest=root/MANIFEST
    if manifest.stat().st_size>MAX_MANIFEST_BYTES: raise ValueError('manifest_too_large')
    lines=manifest.read_text(encoding='utf-8').splitlines()
    if not 1<=len(lines)<=MAX_FILES: raise ValueError('invalid_manifest_count')
    entries={}
    for line in lines:
        match=re.fullmatch(r'([0-9a-fA-F]{64})  (.+)',line)
        if match is None: raise ValueError('invalid_manifest_line')
        name=_relative_name(match[2]).as_posix()
        if name.casefold() in entries: raise ValueError('duplicate_manifest_path')
        entries[name.casefold()]=(name,match[1].lower())
    actual=_files(root)
    changed=missing=checked=0
    for key,(name,expected) in entries.items():
        if cancelled is not None and cancelled(): raise CancelledError()
        path=root/name
        if key not in actual:
            missing+=1; continue
        path.resolve(strict=True).relative_to(root)
        checked+=1
        changed+=file_sha256(path,cancelled=cancelled)!=expected
    return FileCheck(checked,changed,missing,len(actual.keys()-entries.keys()))
