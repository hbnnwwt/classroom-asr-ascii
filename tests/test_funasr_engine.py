"""FunASR 引擎单测（不加载真实模型，只验证 _parse_results 与降级逻辑）。"""
from pathlib import Path

import pytest

from asr.config import load_config
from asr.engines.funasr_engine import FunASREngine, _detect_time_unit
from asr.engines.base import Segment


@pytest.fixture
def cfg():
    """strict_models=False 避免测试时强校验模型路径。"""
    return load_config("config.yaml", strict_models=False)


def test_engine_class_name():
    """引擎名字正确。"""
    assert FunASREngine.name == "funasr"


def test_parse_sentence_info_basic(cfg):
    """sentence_info 路径解析为正确的 Segment 列表。"""
    engine = FunASREngine(cfg)
    # 模拟 FunASR 返回：两个句子，时间戳已是秒
    fake_results = [{
        "sentence_info": [
            {"start": 0.0, "end": 2.5, "text": "你好", "spk": "spk0"},
            {"start": 2.5, "end": 5.0, "text": "世界。", "spk": "spk0"},
        ],
        "text": "你好世界。",
    }]
    segs = engine._parse_results(fake_results)
    assert len(segs) == 2
    assert segs[0].start == 0.0
    assert segs[0].end == 2.5
    assert segs[0].text == "你好"
    assert segs[0].spk == "spk0"
    assert segs[1].text == "世界。"
    assert segs[1].end == 5.0


def test_parse_timestamp_first_last_token(cfg):
    """顶层 timestamp → Segment 时间戳取首尾 token。

    Paraformer 实测：timestamp 数组是 token 级 centisecond（1/100秒）。
    _parse_results 取首尾 token 时间区间作为整段 Segment 的时间范围。
    """
    engine = FunASREngine(cfg)
    fake_results = [{
        "text": "大家好，欢迎来到课堂。",
        # 13 token，全部 centisecond → _detect_time_unit 判定 unit_div=100
        "timestamp": [
            [150, 350], [350, 510], [510, 750], [1150, 1390], [1410, 1610],
            [1610, 1770], [1770, 2010], [2050, 2250], [2250, 2470],
            [2470, 2670], [2670, 2910], [2930, 3090], [3090, 3330],
        ],
    }]
    segs = engine._parse_results(fake_results)
    assert len(segs) == 1
    # 第一个 token 150 centisecond → 1.5s
    assert abs(segs[0].start - 1.5) < 0.01
    # 最后一个 token end 3330 centisecond → 33.3s
    assert abs(segs[0].end - 33.3) < 0.01
    assert "大家好" in segs[0].text


def test_parse_centisecond_unit_detection(cfg):
    """_detect_time_unit 探测 centisecond。"""
    # 大值 → 100
    assert _detect_time_unit([[150, 350], [350, 510]], "你好") == 100.0
    # 小值 → 1（秒）
    assert _detect_time_unit([[1.5, 3.5]], "你好") == 1.0
    # 空 → 1
    assert _detect_time_unit([], "你好") == 1.0


def test_parse_empty_results(cfg):
    """空结果 → 空列表，不崩。"""
    engine = FunASREngine(cfg)
    assert engine._parse_results([]) == []
    assert engine._parse_results([{}]) == []
    assert engine._parse_results([{"text": ""}]) == []


def test_parse_sorts_segments(cfg):
    """乱序的 sentence_info 解析后按 start 排序。"""
    engine = FunASREngine(cfg)
    fake_results = [{
        "sentence_info": [
            {"start": 5.0, "end": 7.0, "text": "后"},
            {"start": 0.0, "end": 2.0, "text": "前"},
            {"start": 2.0, "end": 4.0, "text": "中"},
        ],
    }]
    segs = engine._parse_results(fake_results)
    assert [s.text for s in segs] == ["前", "中", "后"]


def test_engine_close_idempotent(cfg):
    """close() 多次调用不报错。"""
    engine = FunASREngine(cfg)
    engine.close()
    engine.close()
    assert engine._model is None
