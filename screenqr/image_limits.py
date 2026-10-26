"""Explicit per-image resource bounds shared by file loading and decoding."""

MAX_IMAGE_PIXELS = 160_000_000
MAX_IMAGE_EDGE = 65_536


def validate_image_size(size: tuple[int, int]) -> None:
    width, height = size
    if width < 1 or height < 1:
        raise ValueError("图片没有可读取的像素。")
    if width > MAX_IMAGE_EDGE or height > MAX_IMAGE_EDGE or width * height > MAX_IMAGE_PIXELS:
        raise ValueError(
            "图片太大，已停止读取以避免占满内存。"
            "请先裁出二维码和周围留白，再从图片识别；"
            "单张图片最多支持1.6亿像素，最长边65536像素。"
        )
