"""Bounded multi-scale QR detection, preserving instances and source pixels.

An overview catches a symbol larger than a processing tile. Overlapping native
resolution tiles retain small symbols in a large canvas. A single nearest-neighbour
2x pass helps small modules without inventing missing QR information. All passes
are merged by location, never by payload alone, so two identical codes still count
as two targets at the native WeChat input boundary.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import CancelledError
from dataclasses import dataclass
from math import ceil
import time

from PIL import Image
import zxingcpp

from .image_io import grayscale_on_white
from .image_limits import MAX_IMAGE_EDGE, MAX_IMAGE_PIXELS, validate_image_size

TILE_EDGE = 1536
TILE_STEP = 1152
OVERVIEW_EDGE = 2560
DIRECT_PIXELS = 8_000_000
MAX_TILE_COUNT = 256
MAX_INSTANCES = 512
MAX_PYRAMID_LEVELS = 4

_QR_FORMATS = zxingcpp.BarcodeFormats(
    [zxingcpp.QRCode, zxingcpp.MicroQRCode, zxingcpp.RMQRCode]
)
_LINEAR_NAMES = {'Code 128', 'Code 39', 'Code 93', 'EAN-13', 'EAN-8',
                 'UPC-A', 'UPC-E', 'ITF', 'Codabar'}
_LINEAR_FORMATS = zxingcpp.BarcodeFormats([zxingcpp.Code128, zxingcpp.Code39,
    zxingcpp.Code93, zxingcpp.EAN13, zxingcpp.EAN8, zxingcpp.UPCA,
    zxingcpp.UPCE, zxingcpp.ITF, zxingcpp.Codabar])
_SCAN_FORMATS = zxingcpp.BarcodeFormats([zxingcpp.QRCode, zxingcpp.MicroQRCode,
    zxingcpp.RMQRCode, zxingcpp.Code128, zxingcpp.Code39, zxingcpp.Code93,
    zxingcpp.EAN13, zxingcpp.EAN8, zxingcpp.UPCA, zxingcpp.UPCE,
    zxingcpp.ITF, zxingcpp.Codabar])


@dataclass(frozen=True, slots=True)
class QRInstance:
    text: str
    format: str
    # Clockwise source-image pixel coordinates: top-left, top-right,
    # bottom-right, bottom-left. Fractional values are from scaled views.
    points: tuple[tuple[float, float], ...]

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs, ys = zip(*self.points)
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def valid(self) -> bool:
        return True


def _origins(length: int) -> tuple[int, ...]:
    if length <= TILE_EDGE:
        return (0,)
    # Anchor the final tile to the edge so no thin remainder is omitted.
    return tuple(dict.fromkeys((*range(0, length - TILE_EDGE + 1, TILE_STEP), length - TILE_EDGE)))


def tile_boxes(width: int, height: int) -> tuple[tuple[int, int, int, int], ...]:
    validate_image_size((width, height))
    xs, ys = _origins(width), _origins(height)
    if len(xs) * len(ys) > MAX_TILE_COUNT:
        raise ValueError("图片长宽比例过于极端，请先裁出完整二维码后再识别。")
    return tuple((x, y, min(x + TILE_EDGE, width), min(y + TILE_EDGE, height))
                 for y in ys for x in xs)


def pyramid_sizes(width: int, height: int) -> tuple[tuple[int, int], ...]:
    """Intermediate whole-frame levels between source tiles and the overview.

    The largest work layer is at most one quarter of the input pixel area
    (rounded edges aside). With the shared 65536 edge budget there are at most
    four such layers. Only one is retained at a time.
    """
    validate_image_size((width, height))
    sizes = []
    divisor = 2
    while max(ceil(width / divisor), ceil(height / divisor)) > OVERVIEW_EDGE:
        if len(sizes) >= MAX_PYRAMID_LEVELS:
            raise ValueError("图片需要过多缩放层，请先裁出完整二维码后再识别。")
        sizes.append((ceil(width / divisor), ceil(height / divisor)))
        divisor *= 2
    return tuple(sizes)


def _grayscale(image: Image.Image) -> Image.Image:
    # This helper is only used on a bounded view, not on an arbitrarily large
    # full-frame RGBA input. Transparent pixels have the same white backing as
    # imported pictures in the existing decoder.
    return image if image.mode == "L" and "transparency" not in image.info else grayscale_on_white(image)


def _same_instance(first: QRInstance, second: QRInstance) -> bool:
    if first.text != second.text or first.format != second.format:
        return False
    a, b = first.bbox, second.bbox
    if first.format in _LINEAR_NAMES:
        # 长条码在不同扫描条带中可能只返回一段高度；沿条纹方向相交、
        # 编码长度一致才合并。留有空白的重复条码仍是两个实例。
        axis = 0 if max(a[2]-a[0], b[2]-b[0]) >= max(a[3]-a[1], b[3]-b[1]) else 1
        other = 1-axis
        lengths = (a[axis+2]-a[axis], b[axis+2]-b[axis])
        overlap = min(a[axis+2], b[axis+2])-max(a[axis], b[axis])
        cross = min(a[other+2], b[other+2])-max(a[other], b[other])
        if min(lengths) > 0 and min(lengths)/max(lengths) >= .97 and overlap/min(lengths) >= .97 and cross > 0:
            return True
    intersection = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - intersection
    return union > 0.0 and intersection / union >= 0.5


def linear_strip_boxes(width: int, height: int):
    """整行/整列保留长条码的全部编码宽度，避免正方形分块截断。"""
    boxes = []
    for vertical in (False, True):
        long_edge, short_edge = (height, width) if vertical else (width, height)
        if long_edge <= TILE_EDGE:
            continue
        origins = tuple(dict.fromkeys((*range(0, max(1, short_edge-128+1), 96), max(0, short_edge-128))))
        for start in origins:
            end = min(short_edge, start+128)
            boxes.append((start, 0, end, height) if vertical else (0, start, width, end))
    if len(boxes) > 1024:
        raise ValueError('画面太大，请缩小取景范围后再扫。')
    return tuple(boxes)


def _linear_points(view, points):
    """底层库会合并上下同文条码；以原像素的空白间隔恢复独立实例。"""
    left, top = min(p[0] for p in points), min(p[1] for p in points)
    right, bottom = max(p[0] for p in points), max(p[1] for p in points)
    horizontal = right-left >= bottom-top
    lo, hi = (int(top), int(bottom)+1) if horizontal else (int(left), int(right)+1)
    start = None
    bands = []
    for index in range(max(0,lo), min(view.height if horizontal else view.width,hi)):
        box = (max(0,int(left)), index, min(view.width,int(right)+1), index+1) if horizontal else (
            index, max(0,int(top)), index+1, min(view.height,int(bottom)+1))
        with view.crop(box) as row:
            gray = _grayscale(row)
            try:
                low, high = gray.getextrema()
            finally:
                if gray is not row:
                    gray.close()
        if low != high and start is None:
            start = index
        if low == high and start is not None:
            bands.append((start,index-1)); start = None
    if start is not None:
        bands.append((start,min(view.height if horizontal else view.width,hi)-1))
    if len(bands) <= 1:
        return (points,)
    return tuple(((left,a),(right,a),(right,b),(left,b)) if horizontal else
                 ((a,top),(b,top),(b,bottom),(a,bottom)) for a,b in bands if b>a)


def read_qr_instances(image: Image.Image, *, deadline_ns: int | None = None,
                      cancelled: Callable[[], bool] | None = None) -> list[QRInstance]:
    """Read valid QR instances with positions in the unmodified source image.

    A finite input envelope bounds both allocation and pass count. Work views
    are discarded between tiles. There is no default elapsed-time limit. A caller
    can cancel stale work, or supply an absolute deadline for a short operation.
    Neither stop condition releases a partial target list, and finding one symbol
    never skips the remaining planned views. One native decoding call cannot be
    interrupted; cancellation is checked immediately before and after it.
    """
    if not isinstance(image, Image.Image):
        raise TypeError("read_qr_instances expects a PIL image")
    if deadline_ns is not None and type(deadline_ns) is not int:
        raise TypeError("deadline_ns must be an absolute monotonic integer deadline")
    if cancelled is not None and not callable(cancelled):
        raise TypeError("cancelled must be a callable returning a cancellation flag")

    def check_work() -> None:
        if cancelled is not None and cancelled():
            raise CancelledError("已取消本次识别。")
        if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
            raise TimeoutError("本次识别已超时，没有把未完成的结果当成完整结果。")

    check_work()
    validate_image_size(image.size)
    boxes = tile_boxes(image.width, image.height)
    instances: list[QRInstance] = []

    def scan(view: Image.Image, *, offset: tuple[float, float] = (0, 0),
             source_size: tuple[float, float] | None = None, downscale: bool = True,
             formats=_SCAN_FORMATS) -> None:
        check_work()
        gray = _grayscale(view)
        try:
            minimum, maximum = gray.getextrema()
            check_work()
            if minimum == maximum:
                return
            sx = (source_size or view.size)[0] / view.width
            sy = (source_size or view.size)[1] / view.height
            barcodes = zxingcpp.read_barcodes(
                    gray, formats=formats, try_rotate=True,
                    try_downscale=downscale, try_invert=True,
                    text_mode=zxingcpp.TextMode.Plain, return_errors=False)
        finally:
            if gray is not view:
                gray.close()
        # Native decoding calls cannot be interrupted. A late completed view
        # therefore raises, instead of releasing a partial "unique" result.
        check_work()
        for barcode in barcodes:
            check_work()
            if not barcode.valid:
                continue
            position = barcode.position
            local = tuple((p.x,p.y) for p in (position.top_left, position.top_right,
                                             position.bottom_right, position.bottom_left))
            groups = _linear_points(view,local) if str(barcode.format) in _LINEAR_NAMES else (local,)
            for group in groups:
                check_work()
                points = tuple((x*sx+offset[0], y*sy+offset[1]) for x,y in group)
                instance = QRInstance(barcode.text, str(barcode.format), points)
                same = [old for old in instances if _same_instance(instance, old)]
                if same and instance.format in _LINEAR_NAMES:
                    bounds = [instance.bbox, *(old.bbox for old in same)]
                    left, top = min(b[0] for b in bounds), min(b[1] for b in bounds)
                    right, bottom = max(b[2] for b in bounds), max(b[3] for b in bounds)
                    instance = QRInstance(instance.text, instance.format,
                        ((left,top),(right,top),(right,bottom),(left,bottom)))
                    instances[:] = [old for old in instances if old not in same]
                    instances.append(instance)
                elif not same:
                    if len(instances) >= MAX_INSTANCES:
                        raise ValueError("画面里的二维码超过512个，请缩小取景范围后再识别。")
                    instances.append(instance)

    # Preserve the old exact full-resolution pass on ordinary screen captures.
    # It avoids tile-boundary dependence for symbols between tile/overview sizes.
    direct = image.width * image.height <= DIRECT_PIXELS
    if direct:
        scan(image)
    if not direct or max(image.size) > OVERVIEW_EDGE:
        ratio = min(1.0, OVERVIEW_EDGE / max(image.size))
        overview_size = (max(1, ceil(image.width * ratio)), max(1, ceil(image.height * ratio)))
        # Convert only after resizing: large RGBA images never create a second
        # full-size RGBA/RGB copy inside the decoding pipeline.
        check_work()
        with image.resize(overview_size, Image.Resampling.BOX) as overview:
            scan(overview, source_size=image.size)

    def scan_tiles(layer: Image.Image, layer_boxes, *, scale_x: float = 1.0,
                   scale_y: float = 1.0, unscaled_already_read: bool = False) -> None:
        for box in layer_boxes:
            check_work()
            with layer.crop(box) as tile:
                gray = _grayscale(tile)
                try:
                    minimum, maximum = gray.getextrema()
                    if minimum == maximum:
                        continue
                    offset = (box[0] * scale_x, box[1] * scale_y)
                    source_size = (gray.width * scale_x, gray.height * scale_y)
                    if not unscaled_already_read:
                        scan(gray, offset=offset, source_size=source_size)
                    # Finding another symbol never skips the small-module view.
                    with gray.resize((gray.width * 2, gray.height * 2), Image.Resampling.NEAREST) as enlarged:
                        scan(enlarged, offset=offset, source_size=source_size, downscale=False)
                finally:
                    if gray is not tile:
                        gray.close()

    scan_tiles(image, boxes, unscaled_already_read=direct)
    if not direct:
        # A medium dense symbol can straddle native-resolution tiles while the
        # overview makes its modules sub-pixel. Intermediate overlapping grids
        # bridge that gap; source-coordinate mapping also deduplicates instances
        # across levels. Neither a found code nor a late layer releases a
        # partial result to the caller.
        for layer_size in pyramid_sizes(image.width, image.height):
            check_work()
            with image.resize(layer_size, Image.Resampling.BOX) as layer:
                scan_tiles(layer, tile_boxes(*layer_size),
                           scale_x=image.width / layer.width,
                           scale_y=image.height / layer.height)
        for box in linear_strip_boxes(image.width, image.height):
            check_work()
            with image.crop(box) as strip:
                scan(strip, offset=(box[0], box[1]), formats=_LINEAR_FORMATS)
    check_work()
    return instances
