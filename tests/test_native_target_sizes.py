"""Real size-mixed symbols retain native multi-target/capability protection.

No Windows API, screen capture, WeChat input, network or image files are used.
"""
import unittest
from uuid import uuid4

from PIL import Image
import qrcode

from screenqr.frozen_frame import FrozenFrame
from screenqr.native_job import count_qr_symbols, inspect_qr_target
from test_native_port import fixture


def qr(payload, box_size):
    code = qrcode.QRCode(box_size=box_size, border=4,
        error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(payload)
    code.make(fit=True)
    return code.make_image(fill_color="black", back_color="white").convert("RGB")


def frozen(image):
    topology = fixture()[0].topology
    bbox = (0, 0, image.width, image.height)
    return FrozenFrame(str(uuid4()), 1, bbox, bbox, image.width, image.height,
        image.tobytes(), 0.0, 0.001, topology)


class NativeTargetSizeTests(unittest.TestCase):
    def test_same_payload_at_small_and_large_positions_counts_twice(self):
        with Image.new("RGB", (3200, 2000), "white") as image:
            with qr("https://weixin.qq.com/g/PUBLIC_SAME", 1) as small, qr(
                    "https://weixin.qq.com/g/PUBLIC_SAME", 12) as large:
                image.paste(small, (73, 111))
                image.paste(large, (1739, 1079))
            frame = frozen(image)
        original_bytes = frame.rgb8
        self.assertEqual(count_qr_symbols(frame), 2)
        self.assertEqual(frame.rgb8, original_bytes)

    def test_one_symbol_crossing_tile_overlap_is_still_one_instance(self):
        with Image.new("RGB", (3200, 2000), "white") as image:
            with qr("SCREENQR-ONE-POSITION", 9) as code:
                image.paste(code, (1251, 1099))
            frame = frozen(image)
        self.assertEqual(count_qr_symbols(frame), 1)

    def test_tiny_payment_among_large_regular_code_keeps_restriction(self):
        with Image.new("RGB", (3200, 2000), "white") as image:
            with qr("weixin://wxpay/bizpayurl?pr=PUBLIC_NOT_AN_ORDER", 1) as payment, qr(
                    "https://example.com/public-size-test", 12) as ordinary:
                image.paste(payment, (71, 107))
                image.paste(ordinary, (1741, 1077))
            frame = frozen(image)
        inspection = inspect_qr_target(frame)
        self.assertEqual(inspection.count, 2)
        self.assertEqual(inspection.restriction, "native_payment_requires_camera_scan")


if __name__ == "__main__":
    unittest.main()
