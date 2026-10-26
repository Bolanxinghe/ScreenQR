"""仅本机、有限条数的扫码记录；不保存截图，不提供网络入口。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from .core import ScanResult, classify_payload

MAX_ENTRIES = 100
MAX_BYTES = 2_000_000
MAX_TEXT = 131_072


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    id: str
    time: str
    text: str = field(repr=False)
    format: str

    @property
    def result(self) -> ScanResult:
        # 磁盘内容不携带可执行动作，每次从原文重新执行既有内容门禁。
        return classify_payload(self.text, self.format)


def default_path() -> Path:
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local')
    return base / 'ScreenQR' / 'history.json'


class HistoryStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else default_path()
        self.enabled = True
        self.entries: tuple[HistoryEntry, ...] = ()
        self.error = False
        self._load()

    def _load(self):
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size > MAX_BYTES:
                raise ValueError()
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if (type(data) is not dict or set(data) != {'version', 'enabled', 'entries'}
                    or type(data['version']) is not int or data['version'] != 1
                    or type(data['enabled']) is not bool or type(data['entries']) is not list
                    or len(data['entries']) > MAX_ENTRIES):
                raise ValueError()
            entries = []
            ids = set()
            for row in data['entries']:
                if type(row) is not dict or set(row) != {'id', 'time', 'text', 'format'}:
                    raise ValueError()
                if (not all(type(row[k]) is str for k in row)
                        or not 1 <= len(row['id']) <= 64 or row['id'] in ids
                        or not 1 <= len(row['text']) <= MAX_TEXT
                        or not 1 <= len(row['format']) <= 128 or len(row['time']) > 40):
                    raise ValueError()
                datetime.fromisoformat(row['time'])
                entry = HistoryEntry(**row)
                if entry.result.kind == 'wechat_payment':
                    continue
                ids.add(entry.id)
                entries.append(entry)
            self.entries, self.enabled = tuple(entries), data['enabled']
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            # 不把损坏文件或其中内容写入日志；也不影响基础扫码。
            self.entries, self.enabled, self.error = (), False, True

    def _save(self):
        temporary = None
        try:
            rows = [dict(id=e.id, time=e.time, text=e.text, format=e.format) for e in self.entries]
            while True:
                data = json.dumps(dict(version=1, enabled=self.enabled, entries=rows),
                                  ensure_ascii=True, separators=(',', ':')).encode('utf-8')
                if len(data) <= MAX_BYTES:
                    break
                rows.pop()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix='.history-', suffix='.tmp', delete=False) as file:
                temporary = Path(file.name)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            temporary = None
            self.entries = self.entries[:len(rows)]
            self.error = False
            return True
        except OSError:
            self.error = True
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def record(self, result: ScanResult):
        if (not self.enabled or not result.text or len(result.text) > MAX_TEXT
                or classify_payload(result.text, result.format).kind == 'wechat_payment'):
            return False
        if self.entries and (self.entries[0].text, self.entries[0].format) == (result.text, result.format):
            return False
        entry = HistoryEntry(uuid4().hex, datetime.now().astimezone().isoformat(timespec='seconds'),
                             result.text, result.format)
        self.entries = (entry, *self.entries)[:MAX_ENTRIES]
        return self._save()

    def get(self, entry_id: str) -> HistoryEntry | None:
        return next((entry for entry in self.entries if entry.id == entry_id), None)

    def clear(self):
        self.entries = ()
        return self._save()

    def set_enabled(self, enabled: bool):
        if type(enabled) is not bool:
            raise TypeError('history enabled must be bool')
        self.enabled = enabled
        return self._save()
