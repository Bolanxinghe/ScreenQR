"""Read one already-authorized result HWND for one fixed platform notice.

The caller must prove the current request's unique fresh foreground host before
and after calling this function, on a background COM thread. This module neither
finds windows nor asserts business success. It does not activate controls, read
values, follow links, use the desktop root, or return/log page text.

The small COM ABI below was checked against Microsoft's UIAutomationClient.h:
https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/UIAutomationClient.h
UIA_HWND is pointer-sized; HRESULT/BOOL/CONTROLTYPEID remain 32-bit. CUIAutomation8
and IUIAutomation2 are required so provider calls have explicit safeguards. These
limits bound one read-only observation, not the QR recognition task's duration.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import sys
import time
from typing import Iterable, Literal
import unicodedata
from uuid import UUID


NoticeStatus = Literal["CAMERA_REQUIRED", "UNCONFIRMED", "UNAVAILABLE"]
_PHRASES = (
    "该二维码仅支持在扫一扫时，通过摄像头来识别",
    "暂不支持通过长按图片、扫一扫时从相册读取图片等方式来识别",
)
_MAX_NODES = 128
_MAX_DEPTH = 20
_MAX_CHARACTERS = 8192
_MAX_ELEMENT_CHARACTERS = 2048
_OBSERVATION_SECONDS = 4.0
_TEXT_CONTROL_TYPE = 50020
# No names or descendants are inspected for editable, link or password controls.
_SKIP_CONTROL_TYPES = frozenset((50003, 50004, 50005))


@dataclass(frozen=True, slots=True)
class NoticeResult:
    status: NoticeStatus
    progress_sha256: str | None = None


class _Unavailable(Exception):
    pass


class _Limit(Exception):
    pass


def _normalize(text: str) -> str:
    return "".join(character for character in text
                   if not character.isspace()
                   and not unicodedata.category(character).startswith("P"))


def classify_notice_texts(texts: Iterable[str], *, complete: bool = True) -> NoticeResult:
    """Pure bounded classifier. Only punctuation and whitespace are ignored.

    The content is transient. The return value contains only a fixed enum and a
    digest. Exceeding a bound never permits a positive classification.
    """
    parts: list[str] = []
    characters = 0
    try:
        for index, text in enumerate(texts):
            if index >= _MAX_NODES or not isinstance(text, str):
                complete = False
                break
            if len(text) > _MAX_ELEMENT_CHARACTERS or characters + len(text) > _MAX_CHARACTERS:
                complete = False
                break
            characters += len(text)
            parts.append(_normalize(text))
    except Exception:
        return NoticeResult("UNAVAILABLE")
    normalized = "".join(parts)
    try:
        digest = hashlib.sha256(("complete:" if complete else "bounded:").encode()
                                + normalized.encode("utf-8")).hexdigest()
    except UnicodeError:
        return NoticeResult("UNAVAILABLE")
    found = complete and all(_normalize(phrase) in normalized for phrase in _PHRASES)
    return NoticeResult("CAMERA_REQUIRED" if found else "UNCONFIRMED", digest)


def _scan(reader, hwnd: int, *, clock=time.monotonic) -> NoticeResult:
    """Injectable traversal for synthetic tests; every acquired element is freed."""
    stack: list[tuple[object, int, bool]] = []
    texts: list[str] = []
    characters = 0
    nodes = 0
    complete = True
    started = clock()
    try:
        root = reader.root(hwnd)
        if root is None:
            return NoticeResult("UNAVAILABLE")
        stack.append((root, 0, True))
        while stack:
            if nodes >= _MAX_NODES or clock() - started >= _OBSERVATION_SECONDS:
                raise _Limit
            node, depth, is_root = stack.pop()
            try:
                nodes += 1
                # Never request the root's sibling or parent: traversal cannot
                # leave the single caller-selected HWND through this walker.
                if not is_root:
                    sibling = reader.next_sibling(node)
                    if sibling is not None:
                        stack.append((sibling, depth, False))
                if reader.is_offscreen(node) or reader.is_password(node):
                    continue
                control_type = reader.control_type(node)
                if control_type in _SKIP_CONTROL_TYPES:
                    continue
                if control_type == _TEXT_CONTROL_TYPE:
                    text = reader.name(node)
                    if len(text) > _MAX_ELEMENT_CHARACTERS or characters + len(text) > _MAX_CHARACTERS:
                        raise _Limit
                    characters += len(text)
                    texts.append(text)
                child = reader.first_child(node)
                if child is not None:
                    stack.append((child, depth + 1, False))
                    if depth >= _MAX_DEPTH:
                        raise _Limit
            finally:
                reader.release(node)
        return classify_notice_texts(texts)
    except _Limit:
        complete = False
    except Exception:
        return NoticeResult("UNAVAILABLE")
    finally:
        for node, _, _ in stack:
            reader.release(node)
    return classify_notice_texts(texts, complete=complete)


class _Guid(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str):
        return cls.from_buffer_copy(UUID(text).bytes_le)


class _Com:
    def __init__(self, pointer: ctypes.c_void_p):
        if not pointer.value:
            raise _Unavailable
        self.pointer = pointer

    def invoke(self, slot: int, argument_types: tuple, *arguments) -> None:
        vtable = ctypes.cast(self.pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        function = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, *argument_types)(vtable[slot])
        if function(self.pointer, *arguments) < 0:
            raise _Unavailable

    def release(self) -> None:
        if self.pointer.value:
            pointer, self.pointer = self.pointer, ctypes.c_void_p()
            vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(vtable[2])(pointer)

    def integer(self, slot: int) -> int:
        output = ctypes.c_int32()
        self.invoke(slot, (ctypes.POINTER(ctypes.c_int32),), ctypes.byref(output))
        return output.value


class _Reader:
    """Thread-owned UIA objects; no COM object escapes this observation."""
    def __init__(self):
        self.initialized = False
        self.automation: _Com | None = None
        self.walker: _Com | None = None
        self.ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        self.oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)
        self.ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.ole32.CoInitializeEx.restype = ctypes.c_int32
        self.ole32.CoUninitialize.argtypes = []
        self.ole32.CoUninitialize.restype = None
        self.ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_Guid), ctypes.c_void_p, ctypes.c_uint32,
                                               ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p)]
        self.ole32.CoCreateInstance.restype = ctypes.c_int32
        self.oleaut32.SysStringLen.argtypes = [ctypes.c_void_p]
        self.oleaut32.SysStringLen.restype = ctypes.c_uint32
        self.oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
        self.oleaut32.SysFreeString.restype = None

    def __enter__(self):
        # RPC_E_CHANGED_MODE does not authorize uninitializing the caller's COM.
        if self.ole32.CoInitializeEx(None, 0) < 0:
            raise _Unavailable
        self.initialized = True
        try:
            output = ctypes.c_void_p()
            clsid = _Guid.parse("e22ad333-b25f-460c-83d0-0581107395c9")
            iid = _Guid.parse("34723aff-0c9d-49d0-9896-7ab52df8cd8a")
            if self.ole32.CoCreateInstance(ctypes.byref(clsid), None, 1,
                                           ctypes.byref(iid), ctypes.byref(output)) < 0:
                raise _Unavailable
            self.automation = _Com(output)
            # IUIAutomation2: disable automatic focus and bound individual
            # provider connections/transactions. Never call an action pattern.
            self.automation.invoke(59, (ctypes.c_int32,), 0)
            self.automation.invoke(61, (ctypes.c_uint32,), 2000)
            self.automation.invoke(63, (ctypes.c_uint32,), 2000)
            walker = ctypes.c_void_p()
            self.automation.invoke(14, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(walker))
            self.walker = _Com(walker)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        try:
            if self.walker is not None:
                self.walker.release()
        finally:
            try:
                if self.automation is not None:
                    self.automation.release()
            finally:
                if self.initialized:
                    self.initialized = False
                    self.ole32.CoUninitialize()

    def root(self, hwnd: int):
        output = ctypes.c_void_p()
        self.automation.invoke(6, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                               ctypes.c_void_p(hwnd), ctypes.byref(output))
        element = _Com(output)
        try:
            returned = ctypes.c_void_p()
            element.invoke(36, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(returned))
            if returned.value != hwnd:
                raise _Unavailable
            return element
        except Exception:
            element.release()
            raise

    def _navigate(self, slot: int, node: _Com):
        output = ctypes.c_void_p()
        self.walker.invoke(slot, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                           node.pointer, ctypes.byref(output))
        return _Com(output) if output.value else None

    def first_child(self, node):
        return self._navigate(4, node)

    def next_sibling(self, node):
        return self._navigate(6, node)

    @staticmethod
    def is_offscreen(node):
        return bool(node.integer(38))

    @staticmethod
    def is_password(node):
        return bool(node.integer(35))

    @staticmethod
    def control_type(node):
        return node.integer(21)

    def name(self, node):
        output = ctypes.c_void_p()
        try:
            node.invoke(23, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(output))
            size = self.oleaut32.SysStringLen(output)
            if size > _MAX_ELEMENT_CHARACTERS:
                raise _Limit
            return ctypes.wstring_at(output, size) if size else ""
        finally:
            if output.value:
                self.oleaut32.SysFreeString(output)

    @staticmethod
    def release(node):
        node.release()


def read_notice(hwnd: int) -> NoticeResult:
    """Read a single verified host on the calling background thread, fail closed."""
    if (sys.platform != "win32" or not isinstance(hwnd, int) or isinstance(hwnd, bool)
            or hwnd <= 0 or hwnd >= 1 << (ctypes.sizeof(ctypes.c_void_p) * 8)):
        return NoticeResult("UNAVAILABLE")
    try:
        with _Reader() as reader:
            return _scan(reader, hwnd)
    except Exception:
        return NoticeResult("UNAVAILABLE")
