"""Real decoder fixtures and URL action boundaries. No external network calls."""

import unittest

from PIL import Image, ImageOps
import qrcode
import zxingcpp

from screenqr.core import MAX_URL_CHARACTERS, classify_payload, decode_image


def make_qr(text, box_size=6):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box_size, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


class ClassifyTests(unittest.TestCase):
    def test_valid_web_urls(self):
        for text in (
            "https://example.com/path?a=1#two",
            "http://localhost:8765/test",
            "http://127.0.0.1:8765/scan",
            "https://[::1]:8765/test",
            "https://example.com./",
            "https://例子.中国/测试",
        ):
            with self.subTest(text=text):
                result = classify_payload(text)
                self.assertEqual(result.kind, "url")
                self.assertIsNotNone(result.open_url)
                self.assertEqual(result.text, text)

    def test_idn_and_scheme_are_normalized(self):
        result = classify_payload("HTTPS://例子.中国/测试?q=中文")
        self.assertEqual(result.open_url, "https://xn--fsqu00a.xn--fiqs8s/测试?q=中文")

    def test_unsafe_or_ambiguous_urls_cannot_open(self):
        values = [
            "javascript:alert(1)", "file:///C:/Windows/system.ini", "data:text/html,x",
            "ms-settings:privacy", "weixin://dl/scan", "shell:AppsFolder",
            "https://user:password@example.com", "https://example.com@evil.test/",
            "https://example.com\\@evil.test/", "https://example.com/a b",
            " https://example.com", "https://example.com\n", "https://example.com/\u202eabc",
            "https://example.com/\x00", "https://example.com:99999/",
            "https://example.com:bad/", "https://example.com:/", "https://example.com:0/",
            "https:///example.com", "https:example.com", "https://[::1",
            "https://[::1]junk/", "https://[fe80::1%25eth0]/",
            "https://%65xample.com/", "https://bad_domain.test/", "https://-bad.test/",
            "https://bad..test/", "http://127.1/", "http://2130706433/",
            "http://0177.0.0.1/", "http://0x7f000001/", "http://127.0.0.999/",
            "https://example.123/", "https://example.0x7f/",
        ]
        for text in values:
            with self.subTest(text=text):
                result = classify_payload(text)
                self.assertIsNone(result.open_url)
                self.assertEqual(result.text, text)

    def test_long_payload_is_preserved(self):
        text = "https://example.com/" + "a" * MAX_URL_CHARACTERS
        result = classify_payload(text)
        self.assertIsNone(result.open_url)
        self.assertEqual(result.text, text)

    def test_non_web_payloads_remain_copyable(self):
        pairs = [
            ("WIFI:T:WPA;S:example;P:secret;;", "wifi"),
            ("BEGIN:VCARD\nVERSION:3.0\nFN:Test\nEND:VCARD", "contact"),
            ("MECARD:N:Test;;", "contact"), ("mailto:test@example.com", "email"),
            ("tel:123456", "phone"), ("sms:123456", "phone"),
            ("中文文本\n第二行", "text"), ("", "text"),
        ]
        for text, kind in pairs:
            with self.subTest(kind=kind):
                result = classify_payload(text)
                self.assertEqual(result.kind, kind)
                self.assertEqual(result.text, text)
                self.assertIsNone(result.open_url)


class DecodeTests(unittest.TestCase):
    def test_real_unicode_rotated_and_inverted_qr(self):
        payload = "ScreenQR 本机扫码测试 ✓\n第二行 / 123"
        base = make_qr(payload)
        variants = [base, base.rotate(90, expand=True), ImageOps.invert(base), base.convert("L")]
        for index, image in enumerate(variants):
            with self.subTest(variant=index):
                self.assertEqual([r.text for r in decode_image(image)], [payload])

    def test_multiple_symbols_and_duplicate_payload(self):
        first = make_qr("https://example.com/synthetic-one")
        second = make_qr("SCREENCAM-SYNTHETIC-TWO")
        canvas = Image.new("RGB", (first.width + second.width + first.width + 100, max(first.height, second.height) + 40), "white")
        canvas.paste(first, (10, 10))
        canvas.paste(second, (first.width + 40, 10))
        canvas.paste(first, (first.width + second.width + 70, 10))
        found = decode_image(canvas)
        self.assertEqual(len(found), 2)
        self.assertEqual({r.text for r in found}, {"https://example.com/synthetic-one", "SCREENCAM-SYNTHETIC-TWO"})
        self.assertEqual(sum(r.open_url is not None for r in found), 1)

    def test_transparent_and_palette_images(self):
        image = make_qr("SCREENQR-TRANSPARENCY").convert("RGBA")
        image.putdata([(0, 0, 0, 255) if r < 128 else (255, 255, 255, 0) for r, g, b, a in image.get_flattened_data()])
        self.assertEqual(decode_image(image)[0].text, "SCREENQR-TRANSPARENCY")
        self.assertEqual(decode_image(image.convert("P"))[0].text, "SCREENQR-TRANSPARENCY")

    def test_micro_and_rectangular_qr(self):
        for barcode_format, payload in [(zxingcpp.MicroQRCode, "12345"), (zxingcpp.RMQRCode, "SCREENQR-RMQR")]:
            with self.subTest(format=str(barcode_format)):
                barcode = zxingcpp.create_barcode(payload, barcode_format)
                image = Image.fromarray(zxingcpp.write_barcode_to_image(barcode, scale=8))
                result = decode_image(image)
                self.assertEqual([r.text for r in result], [payload])

    def test_not_a_qr_and_wrong_input(self):
        self.assertEqual(decode_image(Image.new("RGB", (320, 240), "white")), [])
        with self.assertRaises(TypeError):
            decode_image(b"not an image")


if __name__ == "__main__":
    unittest.main()
