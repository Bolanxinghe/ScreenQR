"""普通用户用语；格式、内部原因和安全策略不在这里改写。"""
import unicodedata


def one_line(value: str, limit=90):
    clean = ''.join(c for c in value if not unicodedata.category(c).startswith('C'))
    clean = ' '.join(clean.split())
    return clean if len(clean) <= limit else clean[:limit-1]+'…'


def kind_name(result):
    if result.kind == 'text' and result.format not in {'QR Code','Micro QR Code','rMQR Code'}:
        return '条形码'
    return {'url':'网页','wechat':'微信内容','wechat_payment':'付款码',
            'wifi':'Wi-Fi','contact':'名片','email':'邮件','phone':'电话或短信'}.get(result.kind,'文字')


def result_line(result):
    if result.kind == 'wechat_payment':
        return '付款码不处理'
    return f'{kind_name(result)} · {one_line(result.open_url or result.text)}'
