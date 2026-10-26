"""Self-drawn structural fixtures only; no native capture or real acceptance."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from uuid import uuid4

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screenqr.handoff import (FrameBinding, NativeSnapshot, ObservationReceipt,
                             SelectionExtentReceipt, StageReceipt)
from screenqr.native_toolbar import (NativeToolbarBand, ToolbarBinding,
                                     detect_toolbar_icon)

SELECTION = (1194, 628, 2004, 1234)
WHITE, DARK, BACKGROUND = (248, 248, 248), (72, 72, 72), (48, 56, 64)


def binding():
    request_id = str(uuid4())
    item = dict(hwnd=901, pid=700, process_created_filetime_100ns=123456,
                executable_path=r'C:\Program Files\Tencent\Weixin\Weixin.exe', title='Weixin',
                window_class='Qt51514QWindowIcon', window_rect=[0, 0, 3200, 2000],
                client_screen_rect=[0, 0, 3200, 2000], dpi=192, foreground_match=True)
    probe = dict(status='UNIQUE', errors=[], candidates=[item], foreground_hwnd_at_start=901,
                 foreground_hwnd_at_end=901, monitor_count=1, primary_screen_rect=[0, 0, 3200, 2000],
                 thread_dpi_awareness=2)
    before = NativeSnapshot.from_probe(probe, started_ns=120, finished_ns=130,
                                       fixed_file_version=(4, 1, 15, 13))
    after = NativeSnapshot.from_probe(probe, started_ns=175, finished_ns=185,
                                      fixed_file_version=(4, 1, 15, 13))
    source = FrameBinding(str(uuid4()), 'a' * 64, 1, 'b' * 64, 2, 2)
    stage = StageReceipt(source, str(uuid4()), 1, (1200, 630, 1202, 632), SELECTION, 'c' * 64, 90)
    evidence = dict(schema_version=1, selection_extent_status='VERIFIED',
        observed_selection_bbox_physical=SELECTION, extent_verification_method='native-client-observed-boundaries-v1',
        frame_id=source.frame_id, stage_id=stage.stage_id, stage_epoch=1, generation=1,
        source_capture_sha256=source.frame_sha256, topology_sha256=source.topology_sha256,
        started_monotonic_ns=100, finished_monotonic_ns=110,
        native_identity_before=probe, native_identity_after=probe,
        session_id=request_id, evidence_id=str(uuid4()))
    extent = SelectionExtentReceipt.from_native_extent_evidence(stage=stage, native_instance=before.instance,
                                                               evidence=evidence)
    return ToolbarBinding(request_id, 1, extent, before, after, 10_000)


def icon(draw, x, y, variant='qr'):
    if variant.startswith('asymmetric'):
        return asymmetric_icon(draw,x,y,variant)
    positions = [(0, 0), (20, 0), (0, 20)]
    if variant == 'missing_ring':
        positions.pop()
    if variant == 'four_rings':
        positions.append((20, 20))
    for dx, dy in positions:
        draw.rectangle((x + dx, y + dy, x + dx + 11, y + dy + 11), fill=DARK)
        if variant != 'filled_squares':
            draw.rectangle((x + dx + 2, y + dy + 2, x + dx + 9, y + dy + 9), fill=WHITE)
    if variant not in ('no_matrix', 'four_rings'):
        for dx, dy in ((20, 20), (27, 20), (20, 27), (27, 27)):
            draw.rectangle((x + dx, y + dy, x + dx + 2, y + dy + 2), fill=DARK)


def asymmetric_icon(draw,x,y,variant):
    """Self-drawn rectangles express topology, never a captured pixel mask."""
    draw.rectangle((x,y,x+19,y+19),fill=DARK)
    draw.rectangle((x+4,y+4,x+15,y+15),fill=WHITE)
    for dx,dy in ((24,0),(0,24),(24,24)):
        draw.rectangle((x+dx,y+dy,x+dx+11,y+dy+11),fill=DARK)
        draw.rectangle((x+dx+3,y+dy+3,x+dx+8,y+dy+8),fill=WHITE)
    points=[(8,8),(24,16),(32,16),(16,24),(16,32)]
    if variant=='asymmetric_missing_dot':
        points.pop()
    elif variant=='asymmetric_missing_center':
        points.pop(0)
    elif variant=='asymmetric_four_empty_frames':
        points=[]
    for dx,dy in points:
        draw.rectangle((x+dx,y+dy,x+dx+3,y+dy+3),fill=DARK)
    if variant=='asymmetric_broken_small_frame':
        draw.line((x+24,y+5,x+27,y+5),fill=WHITE)
    elif variant=='asymmetric_broken_large_frame':
        draw.line((x,y+6,x+4,y+6),fill=WHITE)
    elif variant=='asymmetric_shifted_dot':
        draw.rectangle((x+24,y+16,x+27,y+19),fill=WHITE)
        draw.rectangle((x+25,y+16,x+28,y+19),fill=DARK)
    elif variant=='asymmetric_extra_ink':
        draw.point((x+22,y+2),fill=DARK)


def bands(variant='qr', *, second_icon=False, second_icon_variant=None, second_toolbar=False, clipped=False):
    result = []
    for side, bbox, times in ((0, (0, 468, 3200, 628), (140, 150)),
                              (1, (0, 1234, 3200, 1394), (155, 165))):
        with Image.new('RGB', (3200, 160), BACKGROUND) as image:
            if side == 0 or second_toolbar:
                top = (70 if clipped else 48) if side == 0 else 16
                draw = ImageDraw.Draw(image)
                draw.rectangle((800, top, 2499, top + 95), fill=WHITE)
                if variant != 'blank':
                    icon(draw, 900, top + 32, variant)
                if second_icon:
                    icon(draw, 1000, top + 32, second_icon_variant or variant)
            result.append(NativeToolbarBand(bbox, image.tobytes(), *times))
    return tuple(result)


def observe(pixels, bound=None, *, deadline=10_000, clock=lambda: 200):
    return detect_toolbar_icon(pixels, binding=bound or binding(), started_ns=115,
                               capture_completed_ns=170, deadline_ns=deadline, clock=clock)


class NativeToolbarTests(unittest.TestCase):
    def test_run_components_preserve_connectivity_merges_holes_and_seed_order(self):
        from screenqr.native_toolbar import _components
        cases = [
            (("101", "010", "101"), [(0,0,1,1,1),(2,0,3,1,1),(1,1,2,2,1),(0,2,1,3,1),(2,2,3,3,1)]),
            (("11011", "01010", "01110"), [(0,0,5,3,9)]),
            (("111", "101", "111"), [(0,0,3,3,8)]),
        ]
        for rows, expected in cases:
            with self.subTest(rows=rows):
                mask=bytes(7 if value == '1' else 0 for row in rows for value in row)
                self.assertEqual(_components(mask,len(rows[0]),len(rows)),expected)

    def test_asymmetric_profile_has_full_glyph_box_without_screen_position_assumption(self):
        result=observe(bands('asymmetric'))
        self.assertEqual(result.status,'READ_ONLY_CANDIDATE',result.reason)
        self.assertEqual(result.candidate_bbox_physical,(900,548,936,584))
        self.assertTrue(result.read_only)
        self.assertFalse(result.raw_pixels_persisted)

    def test_asymmetric_missing_points_broken_frames_four_empty_frames_and_extra_ink_fail(self):
        for variant in ('asymmetric_missing_dot','asymmetric_missing_center',
                'asymmetric_broken_small_frame','asymmetric_broken_large_frame',
                'asymmetric_four_empty_frames','asymmetric_shifted_dot','asymmetric_extra_ink'):
            with self.subTest(variant=variant):
                result=observe(bands(variant))
                self.assertEqual(result.status,'NOT_READY',result.reason)
                self.assertIsNone(result.candidate_bbox_physical)

    def test_asymmetric_duplicates_and_mixed_hypotheses_share_ambiguity_gate(self):
        for second in ('asymmetric','qr'):
            with self.subTest(second=second):
                result=observe(bands('asymmetric',second_icon=True,second_icon_variant=second))
                self.assertEqual(result.status,'AMBIGUOUS',result.reason)
                self.assertIsNone(result.candidate_bbox_physical)

    def test_self_drawn_qr_has_exact_physical_box_and_readonly_receipt(self):
        result = observe(bands())
        self.assertEqual(result.status, 'READ_ONLY_CANDIDATE', result.reason)
        self.assertEqual(result.candidate_bbox_physical, (900, 548, 932, 580))
        self.assertTrue(result.read_only)
        self.assertFalse(result.raw_pixels_persisted)
        receipt = ObservationReceipt.from_native_observation(result, observation_id=1)
        self.assertEqual(receipt.candidate_bbox_physical, result.candidate_bbox_physical)
        metadata = result.to_dict()
        self.assertNotIn('rgb8', str(metadata))
        self.assertNotIn('commit_authorization', metadata)
        self.assertNotIn('click', metadata)

    def test_missing_finder_filled_squares_four_rings_or_no_matrix_fail(self):
        for variant in ('blank', 'missing_ring', 'filled_squares', 'four_rings', 'no_matrix'):
            with self.subTest(variant=variant):
                result = observe(bands(variant))
                self.assertEqual(result.status, 'NOT_READY', result.reason)
                self.assertIsNone(result.candidate_bbox_physical)

    def test_multiple_icons_and_multiple_toolbars_are_ambiguous(self):
        for kwargs in (dict(second_icon=True), dict(second_toolbar=True)):
            with self.subTest(kwargs=kwargs):
                result = observe(bands(**kwargs))
                self.assertEqual(result.status, 'AMBIGUOUS', result.reason)
                self.assertIsNone(result.candidate_bbox_physical)

    def test_partial_toolbar_at_capture_boundary_is_not_a_candidate(self):
        result = observe(bands(clipped=True))
        self.assertEqual(result.status, 'NOT_READY', result.reason)
        self.assertIsNone(result.candidate_bbox_physical)

    def test_extent_identity_profile_and_capture_binding_are_required(self):
        pixels, bound = bands(), binding()
        variants = [
            (pixels, replace(bound, extent=replace(bound.extent, _observed_boundary_proof=None))),
            (pixels, replace(bound, request_id=str(uuid4()))),
            (pixels, replace(bound, identity_after=replace(bound.identity_after,
                instance=replace(bound.identity_after.instance, hwnd=902)))),
            (pixels, replace(bound, identity_after=replace(bound.identity_after,
                instance=replace(bound.identity_after.instance, fixed_file_version=(4, 1, 16, 0))))),
            ((replace(pixels[0], bbox_physical=(0, 470, 3200, 630)), pixels[1]), bound),
            ((replace(pixels[0], capture_started_ns=125), pixels[1]), bound),
            ((replace(pixels[0], rgb8=pixels[0].rgb8[:-1]), pixels[1]), bound),
        ]
        for supplied, binding_value in variants:
            with self.subTest(binding=binding_value):
                result = observe(supplied, binding_value)
                self.assertEqual(result.status, 'REJECTED', result.reason)
                self.assertIsNone(result.candidate_bbox_physical)

    def test_late_computation_and_expired_extent_never_expose_candidate(self):
        ticks = iter((200, 201, 202, 1000))
        result = observe(bands(), deadline=1000, clock=lambda: next(ticks))
        self.assertEqual(result.status, 'STALE')
        self.assertIsNone(result.candidate_bbox_physical)
        result = observe(bands(), replace(binding(), max_evidence_age_ns=50))
        self.assertEqual(result.status, 'STALE')
        self.assertIsNone(result.candidate_bbox_physical)


if __name__ == '__main__':
    unittest.main(verbosity=2)
