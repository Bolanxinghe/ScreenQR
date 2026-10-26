"""Bounded local image loading; never change the file or retain color copies."""
from __future__ import annotations

from pathlib import Path
import warnings

from PIL import Image, ImageOps

from .image_limits import validate_image_size


def grayscale_on_white(image: Image.Image) -> Image.Image:
    """Return an owned L image; transparent pixels composite on white.

    Decode work copies are disposable. This never changes caller pixels or the
    immutable RGB frame used for WeChat display/selection verification.
    """
    with warnings.catch_warnings():
        # We explicitly carry the palette's alpha below instead of relying on
        # Pillow to transfer transparency metadata to a grayscale work image.
        warnings.filterwarnings("ignore", message="Palette images with Transparency expressed in bytes.*", category=UserWarning)
        gray = image.convert("L")
    alpha = None
    try:
        if "A" in image.getbands():
            alpha = image.getchannel("A")
        elif image.mode == "P" and "transparency" in image.info:
            transparency = image.info["transparency"]
            if isinstance(transparency, int):
                table = [0 if i == transparency else 255 for i in range(256)]
            elif isinstance(transparency, bytes):
                table = list(transparency) + [255] * (256 - len(transparency))
            else:
                raise ValueError("图片透明度格式不支持，请另存为PNG后识别。")
            alpha = image.point(table, "L")
        elif image.mode in {"L", "RGB"} and "transparency" in image.info:
            # Pillow applies a color-key transparency when converting these
            # uncommon PNG modes to RGBA. This path remains per-image bounded.
            with image.convert("RGBA") as rgba:
                alpha = rgba.getchannel("A")
        if alpha is not None:
            white = Image.new("L", image.size, 255)
            try:
                white.paste(gray, (0, 0), alpha)
            except BaseException:
                white.close()
                raise
            gray.close()
            return white
        return gray
    except BaseException:
        gray.close()
        raise
    finally:
        if alpha is not None:
            alpha.close()


def load_image_for_decode(path: str | Path) -> Image.Image:
    """Check header dimensions BEFORE load/EXIF/copy, then keep only gray.

    Pillow's hard decompression-bomb error remains active. Its lower advisory
    warning is replaced here by our explicit 160MP/edge checks, scoped to this
    open call rather than disabling Image.MAX_IMAGE_PIXELS process-wide.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                validate_image_size(source.size)
                ImageOps.exif_transpose(source, in_place=True)
                return grayscale_on_white(source)
    except Image.DecompressionBombError:
        raise ValueError("图片太大，已停止读取；请先裁出完整二维码和周围留白。") from None
    except MemoryError:
        raise ValueError("当前可用内存不足，请先裁出完整二维码和周围留白，再识别。") from None
