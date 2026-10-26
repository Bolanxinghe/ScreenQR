"""点选多码不绕过当前画面、位置、内容、像素和网页门禁。"""
from dataclasses import replace
import unittest
from PIL import Image
import qrcode

from screenqr.confirmation import build_candidate, build_selected_candidate, matches
from screenqr.decoding import read_qr_instances


class MultiSelectionTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new('RGB',(900,450),'white')
        self.addCleanup(self.image.close)
        for index,text in enumerate(('https://example.com/a','https://example.com/b')):
            with qrcode.make(text).get_image().convert('RGB') as code:
                self.image.paste(code,(index*450,20))
        self.instances=read_qr_instances(self.image)
        self.token=(1,'screen',(0,0,900,450))

    def test_default_multiple_has_no_candidate_explicit_choice_is_exact(self):
        self.assertIsNone(build_candidate(self.instances,self.token,self.image))
        selected=next(item for item in self.instances if item.text.endswith('/b'))
        candidate=build_selected_candidate(self.instances,self.token,self.image,selected)
        self.assertEqual(candidate.open_url,'https://example.com/b')
        self.assertTrue(matches(candidate,list(reversed(self.instances)),self.token,self.image))

    def test_removed_moved_changed_or_new_instance_cancels_old_choice(self):
        selected=self.instances[0]
        candidate=build_selected_candidate(self.instances,self.token,self.image,selected)
        changed=replace(self.instances[1],text='https://example.com/changed')
        for fresh in ([selected],self.instances+[selected],
                [selected,changed],[replace(selected,points=tuple((x+1,y) for x,y in selected.points)),self.instances[1]]):
            self.assertFalse(matches(candidate,fresh,self.token,self.image))
        self.assertFalse(matches(candidate,self.instances,(2,'screen',(0,0,900,450)),self.image))

    def test_changed_target_pixels_invalidate_even_if_decoder_text_does_not(self):
        candidate=build_selected_candidate(self.instances,self.token,self.image,self.instances[0])
        with self.image.copy() as changed:
            x,y=map(int,self.instances[0].points[0]); changed.putpixel((x+2,y+2),(31,31,31))
            self.assertFalse(matches(candidate,self.instances,self.token,changed))

    def test_wechat_payment_private_protocol_and_unknown_choice_never_grant_browser(self):
        for text in ('weixin://wxpay/bizpayurl?pr=PUBLIC','weixin://dl/scan','file:///C:/x'):
            selected=replace(self.instances[0],text=text)
            self.assertIsNone(build_selected_candidate([selected,self.instances[1]],self.token,self.image,selected))
        self.assertIsNone(build_selected_candidate(self.instances,self.token,self.image,replace(self.instances[0],text='missing')))


if __name__=='__main__': unittest.main()
