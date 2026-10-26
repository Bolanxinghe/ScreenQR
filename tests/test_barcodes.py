"""使用固定 ZXing 自带写码器生成公开条码，检查真实像素和多码边界。"""
import unittest

from PIL import Image
import zxingcpp

from screenqr.core import decode_image
from screenqr.decoding import read_qr_instances, linear_strip_boxes


def barcode(text, format=zxingcpp.Code128, scale=2):
    raw = zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(text, format), scale=scale)
    return Image.frombytes('L', (raw.shape[1], raw.shape[0]), bytes(raw))


class BarcodeTests(unittest.TestCase):
    def test_common_linear_formats_are_read_without_losing_qr_support(self):
        for format, text, expected in ((zxingcpp.Code128,'SCREENQR-PUBLIC-128','SCREENQR-PUBLIC-128'),
                (zxingcpp.Code39,'SCREENQR39','SCREENQR39'),
                (zxingcpp.Code93,'SCREENQR93','SCREENQR93'),
                (zxingcpp.EAN13,'123456789012','1234567890128'),
                (zxingcpp.EAN8,'1234567','12345670'),
                (zxingcpp.ITF,'1234567890','1234567890')):
            with self.subTest(format=str(format)), barcode(text,format) as image:
                results = decode_image(image)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].text, expected)

    def test_rotated_barcode_is_located_in_source_coordinates(self):
        with barcode('SCREENQR-VERTICAL') as image, image.rotate(90,expand=True) as rotated:
            found = read_qr_instances(rotated)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].text, 'SCREENQR-VERTICAL')
            self.assertTrue(all(0 <= x <= rotated.width and 0 <= y <= rotated.height for x,y in found[0].points))

    def test_long_thin_barcode_crossing_native_tiles_is_not_clipped(self):
        text = '1234567890'*19
        with barcode(text,scale=2) as symbol, Image.new('L',(6000,1800),255) as image:
            self.assertGreater(symbol.width, 1536)
            self.assertLess(symbol.width, 5500)
            image.paste(symbol,(350,850))
            found = read_qr_instances(image)
            self.assertEqual([item.text for item in found],[text])

    def test_repeated_payload_in_separate_bars_remains_multiple(self):
        with barcode('SCREENQR-SAME') as symbol, Image.new('L',(800,600),255) as image:
            image.paste(symbol,(50,50)); image.paste(symbol,(50,250))
            self.assertEqual(len(read_qr_instances(image)),2)
            self.assertEqual(len(decode_image(image)),1)

    def test_strip_plan_covers_edges_with_finite_count(self):
        for width,height in ((6000,1800),(65536,2441),(2441,65536)):
            boxes=linear_strip_boxes(width,height)
            self.assertLessEqual(len(boxes),1024)
            self.assertTrue(any(l==0 and r==width and t==0 for l,t,r,b in boxes))
            self.assertTrue(any(t==0 and b==height and r==width for l,t,r,b in boxes))


if __name__ == '__main__': unittest.main()
