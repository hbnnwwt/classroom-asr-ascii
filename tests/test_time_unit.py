"""时间单位探测与归一化的单测。"""
from asr.utils import normalize_time_unit, clamp


def test_normalize_seconds_unchanged():
    """已经是秒的小值，原样返回。"""
    assert normalize_time_unit(1.5) == 1.5
    assert normalize_time_unit(0.0) == 0.0
    assert normalize_time_unit(123.456) == 123.456


def test_normalize_milliseconds_absolute():
    """绝对大值（>1e6）判定为毫秒并除 1000。"""
    # 1500 秒 = 1_500_000 毫秒 → /1000 = 1500
    assert normalize_time_unit(1_500_000) == 1500.0
    # 11.5 天（>1e6 秒）视为毫秒
    assert normalize_time_unit(2_000_000) == 2000.0


def test_normalize_small_value_without_duration():
    """没有 duration 且值小时，按"已是秒"处理，原样返回。"""
    # 12345 < 1e6 且无 duration → 视作秒
    assert normalize_time_unit(12345) == 12345.0
    # 但显式给 duration 表明这是 12.345 秒
    assert normalize_time_unit(12345, duration_s=60.0) == 12.345


def test_normalize_milliseconds_relative():
    """相对判定：值超过 duration_s * 1.2 时视为毫秒。"""
    # 时长 60s，给 75000（当作 75s 来理解），超 60*1.2=72 → /1000 = 75
    assert normalize_time_unit(75000, duration_s=60.0) == 75.0
    # 给 70（< 72），不转换 → 仍是 70
    assert normalize_time_unit(70, duration_s=60.0) == 70.0
    # 边界：正好 1.2 倍不转换（用 ≤ 判断更安全）
    assert normalize_time_unit(72.0, duration_s=60.0) == 72.0


def test_normalize_ambiguous_without_duration():
    """没有 duration 时，小于 1e6 的值原样返回（视作秒）。"""
    # 30000 < 1e6 且无 duration → 视作秒，原样返回
    assert normalize_time_unit(30000) == 30000.0
    # 但显式给 duration 表明这是 30 秒
    assert normalize_time_unit(30000, duration_s=600.0) == 30.0


def test_normalize_negative_clamped():
    """负值归零。"""
    assert normalize_time_unit(-5.0) == 0.0


def test_normalize_int_input():
    """int 也应能处理。"""
    assert normalize_time_unit(30) == 30.0
    assert normalize_time_unit(30) == 30.0  # 不带 duration 视作秒


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10
