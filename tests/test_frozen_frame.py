"""Invariant checks for in-memory frozen frames; no desktop or app operation."""
import unittest
from uuid import uuid4

from PIL import Image

from screenqr.frozen_frame import (
    MonitorDescriptor, TopologyDescriptor, TOPOLOGY_KIND, freeze_image,
)


class FrozenFrameBoundaryTests(unittest.TestCase):
    def topology(self, dpi=192):
        return TopologyDescriptor(
            kind=TOPOLOGY_KIND, virtual_bbox=(-400, -200, 400, 400),
            monitors=(MonitorDescriptor(
                device_name="test-display", physical_bbox=(-400, -200, 400, 400),
                dpi_x=dpi, dpi_y=dpi, primary=True, rotation_degrees=0,
            ),),
        )

    def freeze(self, image, actual=(-400, -200, -398, -198), after=None):
        before = self.topology()
        return freeze_image(
            image, frame_id=str(uuid4()), generation=7,
            requested_bbox=(-401, -201, -398, -198), actual_bbox=actual,
            capture_started_monotonic=1.0, capture_ended_monotonic=1.1,
            topology_before=before, topology_after=after or before,
        )

    def test_later_pixel_changes_cannot_change_frozen_bytes_or_expose_them_in_metadata(self):
        image = Image.new("RGB", (2, 2), (1, 2, 3))
        frame = self.freeze(image)
        digest = frame.capture_sha256
        image.paste((255, 0, 0), (0, 0, 2, 2))
        preview_copy = Image.frombytes("RGB", (frame.width, frame.height), frame.rgb8)
        preview_copy.putpixel((0, 0), (20, 21, 22))
        self.assertEqual(frame.rgb8, bytes([1, 2, 3]) * 4)
        self.assertEqual(frame.capture_sha256, digest)
        self.assertNotIn("rgb8", frame.metadata_only())
        self.assertNotIn("rgb8=", repr(frame))

    def test_changed_dpi_or_wrong_actual_clip_cannot_produce_a_frame(self):
        image = Image.new("RGB", (2, 2))
        with self.assertRaises(ValueError):
            self.freeze(image, after=self.topology(dpi=144))
        with self.assertRaises(ValueError):
            self.freeze(image, actual=(-399, -200, -397, -198))

    def test_negative_clipped_coordinates_remain_explicit(self):
        frame = self.freeze(Image.new("RGB", (2, 2)))
        self.assertEqual(frame.requested_bbox, (-401, -201, -398, -198))
        self.assertEqual(frame.actual_bbox, (-400, -200, -398, -198))
        self.assertEqual((frame.width, frame.height), (2, 2))
        self.assertEqual(frame.generation, 7)


if __name__ == "__main__":
    unittest.main()
