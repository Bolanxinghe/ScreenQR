"""Generate public format fixtures and verify actual local decoding.

Needs development dependencies. Does not sign in, call APIs, access a network,
open any encoded content or claim synthetically encoded tokens are valid.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import qrcode
from screenqr.core import browser_url_for_result, decode_image


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cases = json.loads((ROOT/'tests/fixtures/qr_payload_cases.json').read_text(encoding='utf-8'))
    cases.append({
        'id': 'wechat_login_confirm_format',
        'label': '微信登录确认地址格式，公开自建UUID，非官方签发登录码',
        'payload': 'https://open.weixin.qq.com/connect/confirm?uuid=PUBLIC_SYNTHETIC_UUID_0001',
        'kind': 'wechat', 'browser': False,
    })
    records = []
    for case in cases:
        with qrcode.make(case['payload']).get_image().convert('RGB') as image:
            image.save(args.output/(case['id']+'.png'))
            decoded = decode_image(image)
        passed = (len(decoded) == 1 and decoded[0].text == case['payload']
            and decoded[0].kind == case['kind']
            and (browser_url_for_result(decoded[0]) is not None) is case['browser'])
        records.append({**case, 'sample_origin':'public_synthetic_format_only',
            'authentic_platform_business':False, 'open_content_allowed':False,
            'decode_and_route_passed':passed, 'business_open_verified':False,
            'png':case['id']+'.png'})
    report = {'schema_version':1,'generated_count':len(records),
        'passed_count':sum(item['decode_and_route_passed'] for item in records),
        'network_requests':0,'contents_opened':0,'real_screen_test':False,
        'warning':'Format fixtures are not valid login, group, payment or other signed business credentials.',
        'cases':records}
    (args.output/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key!='cases'},ensure_ascii=False))
    return 0 if report['passed_count'] == len(records) else 1


if __name__ == '__main__':
    raise SystemExit(main())
