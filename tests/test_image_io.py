"""Local file fidelity and header-before-allocation checks; no network or UI."""
import hashlib
from io import BytesIO
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from screenqr.image_io import grayscale_on_white, load_image_for_decode
from screenqr.image_limits import MAX_IMAGE_PIXELS, validate_image_size


class ImageLoadingTests(unittest.TestCase):
    def test_rgba_and_palette_transparency_is_white_without_mutating_source(self):
        with Image.new("RGBA", (3, 1)) as rgba:
            rgba.putdata([(0, 0, 0, 0), (0, 0, 0, 128), (0, 0, 0, 255)])
            original = rgba.tobytes()
            with grayscale_on_white(rgba) as result:
                self.assertEqual(list(result.get_flattened_data()), [255, 127, 0])
            self.assertEqual(rgba.tobytes(), original)
        with Image.new("P", (2, 1)) as palette:
            palette.putpalette([0, 0, 0, 0, 0, 0] + [255] * 762)
            palette.putdata([0, 1])
            for transparency in (0, bytes([0, 255])):
                palette.info["transparency"] = transparency
                with grayscale_on_white(palette) as result:
                    self.assertEqual(list(result.get_flattened_data()), [255, 0])

    def test_exif_orientation_is_applied_in_memory_and_encoded_source_is_unchanged(self):
        encoded = BytesIO()
        with Image.new("L", (2, 3), 0) as image:
            image.putpixel((0, 0), 255)
            exif = Image.Exif()
            exif[274] = 6
            image.save(encoded, format="PNG", exif=exif)
        before = hashlib.sha256(encoded.getvalue()).hexdigest()
        encoded.seek(0)
        with load_image_for_decode(encoded) as loaded:
            self.assertEqual(loaded.mode, "L")
            self.assertEqual(loaded.size, (3, 2))
            self.assertEqual(loaded.getpixel((2, 0)), 255)
        self.assertEqual(hashlib.sha256(encoded.getvalue()).hexdigest(), before)

    def test_oversized_header_is_rejected_before_pixel_load_or_exif_copy(self):
        source = Mock()
        source.size = (65_537, 10)
        context = Mock()
        context.__enter__ = Mock(return_value=source)
        context.__exit__ = Mock(return_value=False)
        with patch("screenqr.image_io.Image.open", return_value=context), \
             patch("screenqr.image_io.ImageOps.exif_transpose") as transpose:
            with self.assertRaisesRegex(ValueError, "图片太大"):
                load_image_for_decode("not-opened.png")
            source.load.assert_not_called()
            source.convert.assert_not_called()
            transpose.assert_not_called()

    def test_16k_wide_image_dimensions_are_admitted_but_budget_is_finite(self):
        validate_image_size((16_384, 8_192))
        with self.assertRaises(ValueError):
            validate_image_size((16_384, 16_384))
        with self.assertRaises(ValueError):
            validate_image_size((1, MAX_IMAGE_PIXELS))

    def test_pillow_hard_bomb_protection_is_not_disabled(self):
        before = Image.MAX_IMAGE_PIXELS
        with patch("screenqr.image_io.Image.open", side_effect=Image.DecompressionBombError("test")):
            with self.assertRaisesRegex(ValueError, "图片太大"):
                load_image_for_decode("not-opened.png")
        self.assertEqual(Image.MAX_IMAGE_PIXELS, before)


if __name__ == "__main__":
    unittest.main()
