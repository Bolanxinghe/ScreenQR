"""Browser confirmation identity and invalidation; no external actions."""
from dataclasses import replace
import unittest

from PIL import Image
import qrcode

from screenqr.confirmation import build_candidate, confirmed_browser_url, matches
from screenqr.core import classify_payload
from screenqr.decoding import QRInstance, read_qr_instances


TOKEN = (4, "screen", (100, 200, 600, 700))


def symbol(text="https://example.com/confirmed"):
    qr = qrcode.QRCode(box_size=5, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    return qr.make_image().convert("RGB")


class ConfirmationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = Image.new("RGB", (500, 500), "white")
        with symbol() as qr:
            cls.image.paste(qr, (50, 60))
        cls.instances = read_qr_instances(cls.image)
        assert len(cls.instances) == 1

    @classmethod
    def tearDownClass(cls):
        cls.image.close()

    def candidate(self):
        candidate = build_candidate(self.instances, TOKEN, self.image)
        self.assertIsNotNone(candidate)
        return candidate

    def test_complete_single_observation_produces_no_action_but_validatable_url(self):
        before = self.image.tobytes()
        candidate = self.candidate()
        self.assertEqual(candidate.open_url, "https://example.com/confirmed")
        self.assertEqual(confirmed_browser_url(candidate, read_qr_instances(self.image), TOKEN, self.image),
                         "https://example.com/confirmed")
        self.assertEqual(self.image.tobytes(), before)
        self.assertNotIn("example.com", repr(candidate))

    def test_empty_and_same_payload_two_instances_invalidate_instead_of_first_target(self):
        candidate = self.candidate()
        instance = self.instances[0]
        other = replace(instance, points=tuple((x + 100, y) for x, y in instance.points))
        for instances in ([], [instance, other]):
            self.assertIsNone(build_candidate(instances, TOKEN, self.image))
            self.assertFalse(matches(candidate, instances, TOKEN, self.image))

    def test_changed_generation_rectangle_and_file_identity_invalidate(self):
        candidate = self.candidate()
        for token in ((5, "screen", TOKEN[2]), (4, "screen", (101, 200, 601, 700)),
                      (4, "file", "sample.png", (42, 99))):
            self.assertIsNone(confirmed_browser_url(candidate, self.instances, token, self.image))
        file_token = (10, "file", "sample.png", (42, 99))
        file_candidate = build_candidate(self.instances, file_token, self.image)
        self.assertTrue(matches(file_candidate, self.instances, file_token, self.image))
        self.assertFalse(matches(file_candidate, self.instances,
                                 (10, "file", "sample.png", (42, 100)), self.image))

    def test_payload_format_and_one_pixel_position_change_invalidate(self):
        candidate = self.candidate()
        first = self.instances[0]
        for changed in (replace(first, text="https://example.com/another"),
                        replace(first, format="Micro QR Code"),
                        replace(first, points=tuple((x + 1, y) for x, y in first.points))):
            self.assertFalse(matches(candidate, [changed], TOKEN, self.image))

    def test_same_decoded_text_with_different_symbol_pixels_invalidates(self):
        candidate = self.candidate()
        with self.image.copy() as changed:
            x, y = map(int, self.instances[0].points[0])
            changed.putpixel((x + 5, y + 5), (123, 45, 67))
            self.assertFalse(matches(candidate, self.instances, TOKEN, changed))
        # Irrelevant background changes must not continually reset confirmation.
        with self.image.copy() as changed:
            changed.putpixel((499, 499), (123, 45, 67))
            self.assertTrue(matches(candidate, self.instances, TOKEN, changed))

    def test_palette_color_change_cannot_reuse_same_index_bytes(self):
        first = QRInstance("https://example.com/palette", "QR Code",
                           ((4.0, 4.0), (12.0, 4.0), (12.0, 12.0), (4.0, 12.0)))
        with Image.new("P", (20, 20), 0) as image:
            image.putpalette([255, 255, 255] + [0] * 765)
            candidate = build_candidate([first], TOKEN, image)
            with image.copy() as changed:
                changed.putpalette([0] * 768)
                self.assertEqual(image.tobytes(), changed.tobytes())
                self.assertFalse(matches(candidate, [first], TOKEN, changed))

    def test_image_dimensions_change_and_malformed_geometry_reject(self):
        candidate = self.candidate()
        first = self.instances[0]
        with Image.new("RGB", (501, 500), "white") as changed:
            self.assertFalse(matches(candidate, self.instances, TOKEN, changed))
        for points in ((), ((0.0, 0.0),) * 4,
                       ((float("nan"), 0.0),) + first.points[1:],
                       ((-1.0, 0.0),) + first.points[1:]):
            self.assertIsNone(build_candidate([replace(first, points=points)], TOKEN, self.image))

    def test_wechat_text_and_malformed_urls_cannot_reach_browser(self):
        first = self.instances[0]
        for text in ("weixin://dl/scan", "https://weixin.qq.com/g/PUBLIC",
                     "hello", "javascript:alert(1)", "https://user:pass@example.com"):
            self.assertIsNone(build_candidate([replace(first, text=text)], TOKEN, self.image))
        candidate = replace(self.candidate(), result=replace(classify_payload("weixin://dl/scan"),
                            open_url="https://example.com/confirmed"))
        self.assertFalse(matches(candidate, self.instances, TOKEN, self.image))

    def test_mutable_source_tokens_are_rejected(self):
        with self.assertRaises(TypeError):
            build_candidate(self.instances, [4, "screen"], self.image)
        with self.assertRaises(TypeError):
            build_candidate(self.instances, (4, {"file": "sample.png"}), self.image)


if __name__ == "__main__":
    unittest.main()
