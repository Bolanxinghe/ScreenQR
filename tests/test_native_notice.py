"""Public synthetic notice/ownership tests. No actual Windows UI is queried."""
from dataclasses import asdict, dataclass, field
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screenqr.native_notice import (NoticeResult, _MAX_CHARACTERS, _MAX_DEPTH,
                                    _MAX_ELEMENT_CHARACTERS, _MAX_NODES,
                                    _scan, classify_notice_texts, read_notice)


FIRST = "该二维码仅支持在扫一扫时，通过摄像头来识别。"
SECOND = "暂不支持通过长按图片、扫一扫时从相册读取图片等方式来识别。"


@dataclass
class Node:
    text: str = ""
    kind: int = 50020
    hidden: bool = False
    password: bool = False
    children: list = field(default_factory=list)
    sibling: object = None


class Reader:
    def __init__(self, root):
        self.node = root
        self.acquired, self.released, self.names = [], [], []

    def _acquire(self, node):
        if node is not None:
            self.acquired.append(id(node))
        return node

    def root(self, hwnd):
        if hwnd != 42:
            raise AssertionError("wrong synthetic target")
        return self._acquire(self.node)

    def first_child(self, node):
        for index, child in enumerate(node.children):
            child.sibling = node.children[index + 1] if index + 1 < len(node.children) else None
        return self._acquire(node.children[0] if node.children else None)

    def next_sibling(self, node):
        if node is self.node:
            raise AssertionError("must not leave the selected root")
        return self._acquire(node.sibling)

    def is_offscreen(self, node):
        return node.hidden

    def is_password(self, node):
        return node.password

    def control_type(self, node):
        return node.kind

    def name(self, node):
        self.names.append(id(node))
        return node.text

    def release(self, node):
        self.released.append(id(node))


class NativeNoticeTests(unittest.TestCase):
    def test_both_complete_sentences_allow_only_spacing_and_punctuation_variants(self):
        result = classify_notice_texts([FIRST.replace("，", ", "), "\n" + SECOND])
        self.assertEqual(result.status, "CAMERA_REQUIRED")
        self.assertRegex(result.progress_sha256, r"^[0-9a-f]{64}$")

    def test_one_partial_changed_or_unrelated_sentence_cannot_classify(self):
        for texts in ([FIRST], [SECOND], [FIRST, SECOND[:-4]],
                      [FIRST.replace("仅支持", "不支持"), SECOND], ["这是公开测试"]):
            with self.subTest(texts=texts):
                self.assertEqual(classify_notice_texts(texts).status, "UNCONFIRMED")

    def test_split_text_nodes_join_without_invented_letters(self):
        self.assertEqual(classify_notice_texts([FIRST[:10], FIRST[10:], SECOND]).status,
                         "CAMERA_REQUIRED")

    def test_incomplete_or_over_bound_read_never_positively_classifies(self):
        cases = ([FIRST, SECOND, "X" * (_MAX_ELEMENT_CHARACTERS + 1)],
                 [FIRST, SECOND] + ["X"] * _MAX_NODES,
                 [FIRST, SECOND] + ["X" * 1024] * (_MAX_CHARACTERS // 1024 + 1))
        for texts in cases:
            self.assertEqual(classify_notice_texts(texts).status, "UNCONFIRMED")
        self.assertEqual(classify_notice_texts([FIRST, SECOND], complete=False).status,
                         "UNCONFIRMED")

    def test_result_retains_no_raw_text_and_digest_tracks_content(self):
        public_text = "Synthetic text only"
        result = classify_notice_texts([public_text])
        self.assertEqual(set(asdict(result)), {"status", "progress_sha256"})
        self.assertNotIn(public_text, repr(result))
        self.assertNotEqual(result.progress_sha256,
                            classify_notice_texts([public_text + " changed"]).progress_sha256)
        self.assertEqual(classify_notice_texts(["\ud800"]), NoticeResult("UNAVAILABLE"))

    def test_only_visible_text_is_read_no_edit_link_password_or_hidden_descendants(self):
        excluded = [Node(FIRST + SECOND, kind=50004), Node(FIRST + SECOND, kind=50005),
                    Node(FIRST + SECOND, hidden=True), Node(FIRST + SECOND, password=True)]
        for node in excluded:
            node.children.append(Node(FIRST + SECOND))
        reader = Reader(Node(kind=50032, children=excluded + [Node("Public fixture")]))
        result = _scan(reader, 42)
        self.assertEqual(result.status, "UNCONFIRMED")
        self.assertEqual(len(reader.names), 1)
        self.assertCountEqual(reader.acquired, reader.released)

    def test_single_root_is_enforced_and_all_owned_nodes_are_released(self):
        reader = Reader(Node(kind=50032, children=[Node(FIRST), Node(SECOND)]))
        self.assertEqual(_scan(reader, 42).status, "CAMERA_REQUIRED")
        self.assertCountEqual(reader.acquired, reader.released)

    def test_node_depth_and_elapsed_bounds_release_pending_nodes(self):
        deep = Node(kind=50032)
        cursor = deep
        for _ in range(_MAX_DEPTH + 2):
            child = Node(kind=50032)
            cursor.children = [child]
            cursor = child
        readers = [Reader(deep), Reader(Node(kind=50032, children=[Node("X") for _ in range(_MAX_NODES + 2)]))]
        for reader in readers:
            self.assertEqual(_scan(reader, 42).status, "UNCONFIRMED")
            self.assertCountEqual(reader.acquired, reader.released)
        reader = Reader(Node(kind=50032, children=[Node(FIRST), Node(SECOND)]))
        values = iter([0.0, 0.0, 5.0])
        self.assertEqual(_scan(reader, 42, clock=lambda: next(values)).status, "UNCONFIRMED")
        self.assertCountEqual(reader.acquired, reader.released)

    def test_provider_failure_is_unavailable_without_content_or_leaked_nodes(self):
        reader = Reader(Node(kind=50032, children=[Node(FIRST), Node(SECOND)]))
        reader.name = lambda node: (_ for _ in ()).throw(RuntimeError("synthetic provider failure"))
        self.assertEqual(_scan(reader, 42), NoticeResult("UNAVAILABLE"))
        self.assertCountEqual(reader.acquired, reader.released)

    def test_non_windows_or_invalid_handle_never_loads_com(self):
        with patch("screenqr.native_notice._Reader", side_effect=AssertionError("COM must not load")):
            for handle in (None, False, 0, -1, "42", 1 << 100):
                self.assertEqual(read_notice(handle), NoticeResult("UNAVAILABLE"))
            with patch("screenqr.native_notice.sys.platform", "not-windows"):
                self.assertEqual(read_notice(42), NoticeResult("UNAVAILABLE"))


if __name__ == "__main__":
    unittest.main()
