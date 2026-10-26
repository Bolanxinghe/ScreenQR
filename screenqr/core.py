"""Decode locally and describe QR contents without executing them.

This module does not capture screens, access a network, open a browser, or log
payloads. The user interface chooses when to use an explicitly allowed URL.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import CancelledError
from dataclasses import dataclass
import ipaddress
import re
import unicodedata
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from PIL import Image

from .decoding import read_qr_instances


# Larger than a standard QR's text capacity, but finite for direct API callers.
# The original payload is always preserved; this limit disables URL actions only.
MAX_URL_CHARACTERS = 16_384
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)
_LOGIN_UUID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z", re.ASCII)
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


@dataclass(frozen=True, slots=True)
class ScanResult:
    text: str
    format: str
    kind: str
    open_url: str | None
    summary: str


def _allowed_url(text: str) -> str | None:
    """Return a normalized HTTP(S) URL, or None for everything ambiguous.

    Python and Windows browsers normalize some malformed URLs differently. Reject
    those forms before parsing, especially backslashes, credentials, control
    characters and non-canonical IPv4 forms. Encoded path/query text is retained.
    """
    if not text or len(text) > MAX_URL_CHARACTERS:
        return None
    if any(c == "\\" or c.isspace() or unicodedata.category(c).startswith("C") for c in text):
        return None
    try:
        parts = urlsplit(text)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            return None
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            return None
        hostname = parts.hostname
        port = parts.port  # Validates the port syntax and 0..65535 range.
        if not hostname or port == 0 or "%" in hostname:
            return None
        # A colon with no port is ambiguous and should not be opened silently.
        if parts.netloc.endswith(":"):
            return None

        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if ":" in hostname or re.fullmatch(r"[0-9.]+", hostname):
                return None
            ascii_host = hostname.encode("idna").decode("ascii").lower()
            domain = ascii_host[:-1] if ascii_host.endswith(".") else ascii_host
            if not domain or len(domain) > 253 or not all(_LABEL.fullmatch(label) for label in domain.split(".")):
                return None
            # Reject legacy browser interpretations such as 127.1 and 0x7f000001.
            # A hostname with a numeric final label is treated as an IPv4 number
            # by common browser URL parsers, even if Python sees a domain.
            last_label = domain.rsplit(".", 1)[-1]
            if last_label.isdecimal() or re.fullmatch(r"0x[0-9a-f]+", last_label):
                return None
            netloc = ascii_host
        else:
            if isinstance(address, ipaddress.IPv6Address):
                if not parts.netloc.startswith("["):
                    return None
                netloc = f"[{address.compressed}]"
            else:
                # ipaddress is deliberately stricter than browser IPv4 parsing.
                if hostname != str(address):
                    return None
                netloc = str(address)
        if port is not None:
            netloc += f":{port}"
        return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment))
    except (ValueError, UnicodeError):
        return None


def _wechat_login_confirm_policy(url: str) -> bool | None:
    """Classify the observed login endpoint after general URL validation.

    None means a different route. False means this endpoint is malformed and
    must offer neither browser opening nor native input. A True result verifies
    only an opaque parameter's syntax, not a signed credential or usable login.
    Percent-encoded unreserved UUID characters are accepted; the caller always
    retains the original payload, including query parameters and their order.
    """
    parts = urlsplit(url)
    if (parts.hostname is None
            or parts.hostname.removesuffix(".") != "open.weixin.qq.com"
            or parts.path != "/connect/confirm"):
        return None
    default_port = 443 if parts.scheme == "https" else 80
    if parts.port not in (None, default_port) or _BAD_PERCENT_ESCAPE.search(parts.query):
        return False
    try:
        parameters = parse_qsl(parts.query, keep_blank_values=True,
                               errors="strict", max_num_fields=256)
    except (ValueError, UnicodeError):
        return False
    values = [value for key, value in parameters if key == "uuid"]
    return len(values) == 1 and _LOGIN_UUID.fullmatch(values[0]) is not None


def is_wechat_login_confirm(text: str) -> bool:
    """Identify only a well-formed observed login-confirm QR payload.

    This pure policy is shared by routing and scoped native-result inspection.
    It does not authenticate the UUID, authorize login, or access any network.
    """
    if not isinstance(text, str):
        return False
    url = _allowed_url(text)
    return url is not None and _wechat_login_confirm_policy(url) is True


def _known_wechat_business_url(url: str) -> bool:
    """Recognize observed native WeChat URL families, including personal cards.

    Hostnames are case-insensitive and a final DNS dot is equivalent. Paths are
    case-sensitive; do not infer other WeChat hosts or unobserved routes.
    The payload's particular WeChat business type is deliberately not guessed.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").removesuffix(".")
    if host == "u.wechat.com":
        # Observed in the owner's real personal-card QR. This identifies only
        # the route; WeChat still validates its opaque token and business result.
        return re.fullmatch(r"/[A-Za-z0-9_-]+", parts.path, re.ASCII) is not None
    return host == "weixin.qq.com" and any(
        parts.path.startswith(prefix) and len(parts.path) > len(prefix)
        for prefix in ("/x/", "/g/")
    )


def _wechat_protocol_kind(text: str) -> str | None:
    """Describe a WeChat protocol without executing it or claiming validity.

    No OS protocol dispatch is offered. Unknown WeChat routes can only be
    handed as original pixels to WeChat after explicit user selection.
    """
    if (not text or len(text) > MAX_URL_CHARACTERS
            or any(c == "\\" or c.isspace() or unicodedata.category(c).startswith("C") for c in text)):
        return None
    try:
        parts = urlsplit(text)
        if (parts.scheme.lower() != "weixin" or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.port is not None or parts.netloc.endswith(":")
                or not all(_LABEL.fullmatch(label) for label in parts.hostname.lower().split("."))):
            return None
        host = parts.hostname.lower()
        if ((host == "wxpay" and parts.path == "/bizpayurl")
                or (host in {"pay.weixin.qq.com", "pay.wechatpay.cn"}
                    and parts.path == "/bizpayurl/up")):
            return "wechat_payment"
        return "wechat"
    except ValueError:
        return None


def classify_payload(text: str, format: str = "QR Code") -> ScanResult:
    """Preserve payloads and expose only a validated web URL as an action.

    Wi-Fi credentials, contact cards and app-specific codes remain local text.
    Recognizing a login/payment URL does not authorize that platform operation.
    """
    if not isinstance(text, str):
        raise TypeError("QR payload must be text")
    url = _allowed_url(text)
    if url is not None:
        login_confirm = _wechat_login_confirm_policy(url)
        if login_confirm is True:
            return ScanResult(text, format, "wechat", None, "微信登录二维码 · 需微信处理")
        if login_confirm is False:
            return ScanResult(text, format, "text", None, "微信登录二维码格式异常 · 无法识别")
        if _known_wechat_business_url(url):
            return ScanResult(text, format, "wechat", None, "微信专用二维码 · 需微信处理")
        host = urlsplit(url).netloc
        return ScanResult(text, format, "url", url, f"网页 · {host}")
    protocol_kind = _wechat_protocol_kind(text)
    if protocol_kind == "wechat_payment":
        return ScanResult(text, format, "wechat_payment", None, "微信Native支付码 · 当前截图识码路线不支持")
    if protocol_kind == "wechat":
        return ScanResult(text, format, "wechat", None, "微信专用协议 · 可交给微信尝试，业务有效性未核验")
    upper = text.upper()
    if upper.startswith("WIFI:"):
        kind, summary = "wifi", "Wi-Fi 配置 · 可查看或复制"
    elif upper.startswith("BEGIN:VCARD") or upper.startswith("MECARD:"):
        kind, summary = "contact", "联系人 · 可查看或复制"
    elif upper.startswith("MAILTO:") or upper.startswith("MATMSG:"):
        kind, summary = "email", "邮件内容 · 可复制"
    elif upper.startswith("TEL:") or upper.startswith("SMSTO:") or upper.startswith("SMS:"):
        kind, summary = "phone", "电话或短信 · 可复制"
    elif upper.startswith(("HTTP:", "HTTPS:")):
        kind, summary = "text", "网址格式异常 · 可检查或复制"
    elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", text):
        kind, summary = "text", "应用专用内容 · 可复制"
    elif not text:
        kind, summary = "text", "空内容"
    else:
        kind, summary = "text", "文本 · 可复制"
    return ScanResult(text, format, kind, None, summary)


def browser_url_for_result(result: ScanResult) -> str | None:
    """Recheck the preserved payload at every browser action boundary.

    A hand-constructed or stale ScanResult cannot bypass content routing by
    supplying its own open_url. Both URLs must validate to the same ordinary
    web address; a known WeChat business URL never becomes a browser action.
    """
    if (not isinstance(result, ScanResult) or not isinstance(result.text, str)
            or not isinstance(result.open_url, str)):
        return None
    expected = classify_payload(result.text, result.format).open_url
    if expected is None or _allowed_url(result.open_url) != expected:
        return None
    return expected


def decode_image(image: Image.Image, *, deadline_ns: int | None = None,
                 cancelled: Callable[[], bool] | None = None) -> list[ScanResult]:
    """Find QR Model 1/2, Micro QR and rMQR symbols in a PIL image.

    Repeated identical payloads in one image produce one result. No partial/error
    decodes are accepted, and payload text is never trimmed or truncated. There
    is no default time limit. Cancellation raises CancelledError and never
    publishes an incomplete result list.
    """
    if not isinstance(image, Image.Image):
        raise TypeError("decode_image expects a PIL image")
    if image.width < 1 or image.height < 1:
        return []
    barcodes = read_qr_instances(image, deadline_ns=deadline_ns, cancelled=cancelled)
    results: list[ScanResult] = []
    seen: set[str] = set()
    for barcode in barcodes:
        if cancelled is not None and cancelled():
            raise CancelledError("已取消本次识别。")
        if not barcode.valid or barcode.text in seen:
            continue
        seen.add(barcode.text)
        results.append(classify_payload(barcode.text, str(barcode.format)))
    if cancelled is not None and cancelled():
        raise CancelledError("已取消本次识别。")
    return results
