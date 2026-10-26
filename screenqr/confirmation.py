"""Bind a browser confirmation to one observed QR and its unmodified pixels.

There is no screen capture, clock, browser action, image mutation or disk I/O
here. Callers supply COMPLETE instance lists from ``read_qr_instances`` and a
source token covering their generation and screen rectangle or file identity.
An empty, changed or ambiguous observation invalidates the displayed candidate.

On click, disable the action, capture/decode again in a worker, and use
``confirmed_browser_url``. Before opening that returned URL on the UI thread,
the caller must still require the same current source token and candidate. A
worker finishing after a move/close/new request must never open its old URL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from math import ceil, floor, isfinite
from typing import Sequence

from PIL import Image

from .core import ScanResult, browser_url_for_result, classify_payload
from .decoding import QRInstance

PixelBox = tuple[int, int, int, int]
Points = tuple[tuple[float, float], ...]


def _immutable_source_token(value: object) -> bool:
    """Only immutable data tokens can cross the UI/worker boundary safely."""
    if value is None or type(value) in (str, bytes, int, bool):
        return True
    return type(value) is tuple and all(_immutable_source_token(item) for item in value)


@dataclass(frozen=True, slots=True)
class Candidate:
    # Do not include account URLs, file names or their decoded text in reprs.
    source_token: object = field(repr=False)
    result: ScanResult = field(repr=False)
    image_size: tuple[int, int]
    points: Points
    crop_box: PixelBox
    crop_sha256: str
    # 只有明确点选多码才填写；完整场景实例表也必须保持不变。
    selected: QRInstance | None = field(default=None, repr=False)
    scene: tuple[QRInstance, ...] = field(default=(), repr=False)

    @property
    def open_url(self) -> str | None:
        # Even a directly constructed Candidate cannot substitute a forged URL.
        return browser_url_for_result(self.result)


def _target_geometry(instance: QRInstance, image: Image.Image) -> tuple[Points, PixelBox] | None:
    if not isinstance(instance, QRInstance) or not isinstance(instance.text, str):
        return None
    if not isinstance(instance.points, tuple) or len(instance.points) != 4:
        return None
    points = []
    for point in instance.points:
        if not isinstance(point, tuple) or len(point) != 2:
            return None
        x, y = point
        if (type(x) not in (int, float) or type(y) not in (int, float)
                or not isfinite(x) or not isfinite(y)
                or not 0 <= x <= image.width or not 0 <= y <= image.height):
            return None
        points.append((float(x), float(y)))
    left, top = min(p[0] for p in points), min(p[1] for p in points)
    right, bottom = max(p[0] for p in points), max(p[1] for p in points)
    if right <= left or bottom <= top:
        return None
    # Include a small border outside the located symbol, bounded by the original
    # image. Work views never replace source coordinates or alter source pixels.
    box = (max(0, floor(left) - 2), max(0, floor(top) - 2),
           min(image.width, ceil(right) + 2), min(image.height, ceil(bottom) + 2))
    return tuple(points), box


def _pixel_digest(image: Image.Image, box: PixelBox) -> str:
    """Hash source-region RGBA in row order without a full giant crop copy.

    Conversion also incorporates palette and transparency changes, which a
    raw palette-index digest would miss. At most 64 source rows are converted
    per iteration. The image and its metadata stay owned by the caller.
    """
    left, top, right, bottom = box
    digest = hashlib.sha256()
    digest.update(f"RGBA:{right-left}:{bottom-top}:".encode("ascii"))
    for row in range(top, bottom, 64):
        with image.crop((left, row, right, min(row + 64, bottom))) as stripe:
            with stripe.convert("RGBA") as rgba:
                digest.update(rgba.tobytes())
    return digest.hexdigest()


def build_candidate(instances: Sequence[QRInstance], source_token: object,
                    image: Image.Image) -> Candidate | None:
    """Return one ordinary web target or None; never select the first of many.

    The instance list must not be text-deduplicated: two identical QR symbols
    are two targets and therefore cannot produce a confirmation. No hard time
    limit is imposed here. The caller determines whether decoding completed.
    """
    if not isinstance(image, Image.Image):
        raise TypeError("confirmation requires the unchanged observation image")
    if not _immutable_source_token(source_token):
        raise TypeError("source_token must contain only immutable scalar/tuple data")
    if image.width < 1 or image.height < 1 or len(instances) != 1:
        return None
    instance = instances[0]
    geometry = _target_geometry(instance, image)
    if geometry is None:
        return None
    result = classify_payload(instance.text, instance.format)
    if browser_url_for_result(result) is None:
        return None
    points, box = geometry
    return Candidate(source_token=source_token, result=result, image_size=image.size,
                     points=points, crop_box=box, crop_sha256=_pixel_digest(image, box))


def matches(candidate: Candidate | None, fresh_instances: Sequence[QRInstance],
            source_token: object, image: Image.Image) -> bool:
    """Revalidate source, multiplicity, payload, exact position and QR pixels."""
    if not isinstance(candidate, Candidate) or candidate.open_url is None:
        return False
    # Avoid pixel hashing when a simple source/dimension change already fails.
    if (candidate.source_token != source_token or not isinstance(image, Image.Image)
            or candidate.image_size != image.size):
        return False
    fresh = (build_selected_candidate(fresh_instances, source_token, image, candidate.selected)
             if candidate.selected is not None else build_candidate(fresh_instances, source_token, image))
    return candidate == fresh


def build_selected_candidate(instances: Sequence[QRInstance], source_token: object,
                             image: Image.Image, selected: QRInstance) -> Candidate | None:
    """只授权用户点中的完整实例；不会改变默认多码拒绝行为或微信门禁。"""
    if (not isinstance(selected, QRInstance) or len(instances) < 2
            or sum(item == selected for item in instances) != 1):
        return None
    candidate = build_candidate([selected], source_token, image)
    if candidate is None:
        return None
    # 排序只固定列表顺序，不合并同文实例。位置、格式、内容任一改变即撤销。
    scene = tuple(sorted(instances, key=lambda item:(item.points,item.format,item.text)))
    from dataclasses import replace
    return replace(candidate, selected=selected, scene=scene)


def confirmed_browser_url(candidate: Candidate | None,
                          fresh_instances: Sequence[QRInstance], source_token: object,
                          image: Image.Image) -> str | None:
    """Return the validated URL only after a matching fresh click observation.

    This helper does not perform or infer a user click and does not open a URL.
    The UI must call it exclusively for a still-current explicit confirmation.
    """
    return candidate.open_url if matches(candidate, fresh_instances, source_token, image) else None
