"""本地微信校准配置；配置数据不会成为输入许可，也不执行下载或系统查询。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import json
import ntpath
from pathlib import Path
import re

# 安全期限不属于可调校准项。配置不能延长期限或放宽证据门槛。
INPUT_BUDGET_NS = 2_000_000_000
RESULT_BUDGET_NS = 10_000_000_000


@dataclass(frozen=True, slots=True)
class ToolbarCalibration:
    band_height: int = 160
    width: tuple[int, int] = (1500, 1800)
    height: tuple[int, int] = (88, 104)
    gap_max: int = 64
    finder_side: tuple[int, int] = (8, 22)
    icon_side: tuple[int, int] = (28, 64)
    quiet_pixels: int = 4
    neutral_max: int = 20
    dark_max: int = 176
    light_min: int = 224


@dataclass(frozen=True, slots=True)
class BoundaryCalibration:
    gray: tuple[int, int, int] = (119, 119, 119)
    green: tuple[int, int, int] = (0, 195, 117)
    white: tuple[int, int, int] = (254, 254, 254)
    outer: int = 12
    inner: int = 6
    tangent: int = 8
    green_run: int = 6
    guard: int = 16
    padding: int = 32


@dataclass(frozen=True, slots=True)
class NativeProfile:
    id: str
    version: tuple[int, int, int, int]
    dpi: int
    screen: tuple[int, int, int, int]
    executable: str
    window_class: str
    title: str
    border_id: str
    toolbar_id: str
    display_id: str
    toolbar: ToolbarCalibration = ToolbarCalibration()
    boundary: BoundaryCalibration = BoundaryCalibration()
    model: str = 'strict-v1'

    @property
    def width(self):
        return self.screen[2] - self.screen[0]

    @property
    def height(self):
        return self.screen[3] - self.screen[1]


# 只用于纯算法的兼容默认值及损坏配置下的安全常量，不能绕过启动匹配。
# 真实程序配置损坏时禁用微信通道，基础扫码仍能启动。
DEFAULT_PROFILE = NativeProfile(
    'wechat-4.1.15.13-dpi192-3200x2000', (4, 1, 15, 13), 192,
    (0, 0, 3200, 2000), r'C:\Program Files\Tencent\Weixin\Weixin.exe',
    'Qt51514QWindowIcon', 'Weixin',
    'wechat-4.1.15.13-dpi192-3200x2000-border-v1',
    'original-asymmetric-qr-v2-wechat-4.1.15.13-dpi192',
    'wechat4.1.15.13-dpi192-3200x2000-max-v-minus-1-v1')

_current: ContextVar[NativeProfile] = ContextVar('screenqr_native_profile', default=DEFAULT_PROFILE)


def current_profile() -> NativeProfile:
    return _current.get()


@contextmanager
def profile_scope(profile: NativeProfile):
    """每个请求绑定不可变配置，线程间隔离；退出时恢复，不能中途改全局坐标。"""
    if not isinstance(profile, NativeProfile):
        raise TypeError('typed_native_profile_required')
    token = _current.set(profile)
    try:
        yield profile
    finally:
        _current.reset(token)


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError('profile_unknown_or_missing_field')


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('profile_integer_outside_allowlist')
    return value


def _tuple(value, count, low, high):
    if type(value) is not list or len(value) != count:
        raise ValueError('profile_shape_invalid')
    return tuple(_integer(v, low, high) for v in value)


def _identifier(value):
    if type(value) is not str or re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) is None:
        raise ValueError('profile_identifier_invalid')
    return value


def profile_from_record(record) -> NativeProfile:
    """白名单只接受数据；没有模块名、脚本、任意路径、按键或宽松校验开关。"""
    _keys(record, asdict(DEFAULT_PROFILE))
    if record['model'] != 'strict-v1':
        raise ValueError('profile_model_unsupported')
    version = _tuple(record['version'], 4, 0, 65535)
    dpi = _integer(record['dpi'], 96, 384)
    if dpi % 24:
        raise ValueError('profile_dpi_unsupported')
    screen = _tuple(record['screen'], 4, 0, 16384)
    if screen[:2] != (0, 0) or min(screen[2:]) < 320 or screen[2] * screen[3] > 160_000_000:
        raise ValueError('profile_screen_unsupported')
    executable = record['executable']
    # 首次迁移保持旧路径白名单，未把任意安装文件当作可控制的微信。
    if type(executable) is not str or ntpath.normcase(executable) != ntpath.normcase(DEFAULT_PROFILE.executable):
        raise ValueError('profile_executable_outside_allowlist')
    if record['title'] != 'Weixin' or record['window_class'] != 'Qt51514QWindowIcon':
        raise ValueError('profile_window_kind_unsupported')
    tb = record['toolbar']; _keys(tb, asdict(DEFAULT_PROFILE.toolbar))
    pairs = {key: _tuple(tb[key], 2, 1, 16384) for key in ('width','height','finder_side','icon_side')}
    if any(low > high for low, high in pairs.values()):
        raise ValueError('profile_range_reversed')
    toolbar = ToolbarCalibration(
        _integer(tb['band_height'], 64, 512), **pairs,
        gap_max=_integer(tb['gap_max'], 0, 128),
        quiet_pixels=_integer(tb['quiet_pixels'], 4, 16),
        neutral_max=_integer(tb['neutral_max'], 0, 20),
        dark_max=_integer(tb['dark_max'], 0, 176),
        light_min=_integer(tb['light_min'], 224, 255))
    if (toolbar.width[1] >= screen[2] or toolbar.height[1] >= toolbar.band_height
            or 2 * toolbar.band_height >= screen[3]
            or toolbar.finder_side[1] > 64 or toolbar.icon_side[1] > 128):
        raise ValueError('profile_toolbar_outside_screen')
    bd = record['boundary']; _keys(bd, asdict(DEFAULT_PROFILE.boundary))
    # strict-v1 是已有精确八截面算法；改变结构需新算法及独立验证。
    if any(bd[key] != getattr(DEFAULT_PROFILE.boundary,key) for key in
           ('outer','inner','tangent','green_run','guard','padding')):
        raise ValueError('profile_boundary_algorithm_unsupported')
    colors={key:_tuple(bd[key],3,0,255) for key in ('gray','green','white')}
    if (len(set(colors.values())) != 3 or len(set(colors['gray'])) != 1
            or min(colors['white']) < 224 or len(set(colors['white'])) != 1
            or not colors['green'][1] > max(colors['green'][0],colors['green'][2])):
        raise ValueError('profile_boundary_colors_invalid')
    boundary=BoundaryCalibration(**colors)
    return NativeProfile(_identifier(record['id']),version,dpi,screen,executable,
        record['window_class'],record['title'],_identifier(record['border_id']),
        _identifier(record['toolbar_id']),_identifier(record['display_id']),toolbar,boundary)


@dataclass(frozen=True, slots=True)
class ProfileRegistry:
    profiles: tuple[NativeProfile, ...]
    error: str | None = None

    def match(self, version, screen, dpi, monitor_count):
        found = tuple(p for p in self.profiles if p.version == version and p.screen == screen
                      and p.dpi == dpi and monitor_count == 1)
        return found[0] if len(found) == 1 and self.error is None else None


def load_profiles(path: Path | None = None) -> ProfileRegistry:
    path = path or Path(__file__).resolve().parent.parent/'assets/native-profiles.json'
    try:
        if path.stat().st_size > 65_536:
            raise ValueError('profile_file_too_large')
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result: raise ValueError('profile_duplicate_json_key')
                result[key]=value
            return result
        data=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=unique)
        _keys(data, ('schema_version','profiles'))
        if type(data['schema_version']) is not int or data['schema_version'] != 1:
            raise ValueError('profile_schema_unsupported')
        if type(data['profiles']) is not list or not 1 <= len(data['profiles']) <= 16:
            raise ValueError('profile_count_invalid')
        profiles=tuple(profile_from_record(p) for p in data['profiles'])
        if len({p.id for p in profiles}) != len(profiles) or len({(p.version,p.screen,p.dpi) for p in profiles}) != len(profiles):
            raise ValueError('profile_duplicate_identity')
        return ProfileRegistry(profiles)
    except (OSError,ValueError,TypeError,KeyError,RecursionError):
        return ProfileRegistry((), 'profile_configuration_unavailable')
