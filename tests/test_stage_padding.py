"""Declared stage geometry only: no Tk root, Win32 query, UI or real capture.

Synthetic edge decorations test separation, not real WeChat handle dimensions.
The actual native payload must still match its strict display receipt.
"""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from PIL import Image, ImageDraw

from screenqr.app import ScreenQRApp
from screenqr.frozen_frame import FrozenFrame, MonitorDescriptor, TopologyDescriptor, TOPOLOGY_KIND
from screenqr.stage import (FrozenStageMapping, OwnStageSnapshot, StageValidationError,
    GUARD_PHYSICAL_PX, WHITE_PADDING_PHYSICAL_PX, LIVE_NATIVE_GUARD_PHYSICAL_PX,
    LIVE_NATIVE_WHITE_PADDING_PHYSICAL_PX, describe_frozen_stage, even_transport_bbox)


class StagePaddingTests(unittest.TestCase):
    def setUp(self):
        self.topology=TopologyDescriptor(TOPOLOGY_KIND,(0,0,3200,2000),
            (MonitorDescriptor('DISPLAY1',(0,0,3200,2000),192,192,True,0),))
        self.frame=FrozenFrame(str(uuid4()),3,(0,0,41,33),(0,0,41,33),
            41,33,bytes((i%251 for i in range(41*33*3))),1.0,1.1,self.topology)

    def describe(self,guard=8,padding=24,*,origin=(101,201),defaults=False):
        left,top=origin
        payload=(left,top,left+41,top+33)
        white=(left-padding,top-padding,left+41+padding,top+33+padding)
        own=OwnStageSnapshot(101,102,103,104,(40,140,210,310),
            (50,150,200,300),payload,white,192)
        kwargs={} if defaults else dict(guard_physical_px=guard,white_padding_physical_px=padding)
        with patch('screenqr.stage._StageApi') as api, \
             patch('screenqr.stage.read_display_topology',return_value=self.topology):
            api.return_value.query.return_value=own
            mapping=describe_frozen_stage(self.frame,stage_id=str(uuid4()),stage_epoch=4,
                top_hwnd=101,image_hwnd=102,padding_hwnd=103,dpi_reference_hwnds=(101,),**kwargs)
        return mapping

    def test_legacy_default_and_live_pair_preserve_exact_padding_and_even_geometry(self):
        self.assertEqual((GUARD_PHYSICAL_PX,WHITE_PADDING_PHYSICAL_PX),(8,24))
        self.assertEqual((LIVE_NATIVE_GUARD_PHYSICAL_PX,LIVE_NATIVE_WHITE_PADDING_PHYSICAL_PX),(16,32))
        for guard,pad in ((8,24),(16,32)):
            for origin in ((100,200),(101,201)):
                with self.subTest(guard=guard,origin=origin):
                    mapping=self.describe(guard,pad,origin=origin,defaults=guard==8)
                    self.assertTrue(all(value%2==0 for value in mapping.transport_bbox_physical))
                    self.assertTrue(all(value in (guard,guard+1) for value in mapping.payload_offset))
                    metadata=mapping.metadata_only()
                    self.assertEqual((metadata['guard_physical_px'],metadata['white_padding_physical_px']),
                        (guard,pad))
                    self.assertEqual(pad-guard,16)  # Nominal outer white protection is unchanged.
                    white,transport=mapping.white_padding_bbox_physical,mapping.transport_bbox_physical
                    gaps=(transport[0]-white[0],transport[1]-white[1],
                        white[2]-transport[2],white[3]-transport[3])
                    self.assertTrue(all(gap in (15,16) for gap in gaps))  # Outward even rounding.

    def test_undeclared_pair_rejected_before_native_queries_and_mapping_cannot_be_relabelled(self):
        mapping=self.describe()
        for guard,pad in ((8,32),(16,24),(12,28),(True,24),(16,True)):
            with self.subTest(guard=guard,pad=pad):
                with patch('screenqr.stage._StageApi') as api:
                    with self.assertRaises(StageValidationError):
                        describe_frozen_stage(self.frame,stage_id=str(uuid4()),stage_epoch=4,
                            top_hwnd=101,image_hwnd=102,padding_hwnd=103,
                            dpi_reference_hwnds=(101,),guard_physical_px=guard,white_padding_physical_px=pad)
                    api.assert_not_called()
                with self.assertRaises(StageValidationError):
                    replace(mapping,guard_physical_px=guard,white_padding_physical_px=pad)
        with self.assertRaises(StageValidationError):
            even_transport_bbox(mapping.payload_bbox_physical,mapping.white_padding_bbox_physical,
                guard_physical_px=12)

    def test_live_offset_and_white_container_must_remain_exact(self):
        mapping=self.describe(16,32)
        with self.assertRaises(StageValidationError):
            replace(mapping,payload_offset=(8,8))
        white=mapping.white_padding_bbox_physical
        wrong=(white[0]-1,white[1],white[2],white[3])
        with self.assertRaises(StageValidationError):
            replace(mapping,white_padding_bbox_physical=wrong,
                own_stage=replace(mapping.own_stage,padding_bbox_physical=wrong))

    def test_wider_guard_keeps_synthetic_edge_handles_outside_unchanged_raw_pixels(self):
        before=(self.frame.rgb8,self.frame.capture_sha256,self.frame.width,self.frame.height)
        for guard,pad,expect_unchanged in ((8,24,False),(16,32,True)):
            mapping=self.describe(guard,pad)
            left,top,right,bottom=mapping.transport_bbox_physical
            offset=mapping.payload_offset
            with Image.new('RGB',(right-left,bottom-top),(255,255,255)) as canvas:
                with Image.frombytes('RGB',(self.frame.width,self.frame.height),self.frame.rgb8) as raw:
                    canvas.paste(raw,offset)
                draw=ImageDraw.Draw(canvas)
                width,height=canvas.size
                # A deliberately declared 15px synthetic inset, not a native measurement.
                for box in ((0,height//2-3,14,height//2+2),
                            (width-15,height//2-3,width-1,height//2+2),
                            (width//2-3,0,width//2+2,14),
                            (width//2-3,height-15,width//2+2,height-1)):
                    draw.rectangle(box,fill=(0,195,117))
                with canvas.crop((offset[0],offset[1],offset[0]+41,offset[1]+33)) as payload:
                    self.assertEqual(payload.tobytes()==self.frame.rgb8,expect_unchanged)
        self.assertEqual((self.frame.rgb8,self.frame.capture_sha256,self.frame.width,self.frame.height),before)

    def test_app_declares_same_live_pair_as_its_white_container(self):
        app=ScreenQRApp.__new__(ScreenQRApp)
        app._require_ui_thread=Mock()
        app._closed=False
        app._generation=self.frame.generation
        app._frozen_frame=self.frame
        app._freeze_stage_id=str(uuid4())
        app._freeze_stage_epoch=4
        app.paused=SimpleNamespace(get=lambda:True)
        app._freeze_photo=Mock()
        app._freeze_photo.width.return_value=41
        app._freeze_photo.height.return_value=33
        widgets=[Mock() for _ in range(3)]
        app._freeze_preview,app._freeze_image_label,app._freeze_white_stage=widgets
        for index,widget in enumerate(widgets):
            widget.winfo_exists.return_value=widget.winfo_viewable.return_value=True
            widget.winfo_id.return_value=101+index
        app._freeze_image_label.cget.return_value=str(app._freeze_photo)
        app._freeze_image_label.winfo_width.return_value=41
        app._freeze_image_label.winfo_height.return_value=33
        app._own_top_hwnd=Mock(return_value=101)
        app._own_dpi_reference_hwnds=Mock(return_value=(101,))
        with patch('screenqr.app.describe_stage_native') as describe:
            app.describe_frozen_stage()
        self.assertEqual((describe.call_args.kwargs['guard_physical_px'],
            describe.call_args.kwargs['white_padding_physical_px']),(16,32))


if __name__=='__main__':
    unittest.main()
