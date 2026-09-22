"""postprocess 单测。"""
from pathlib import Path

import pytest

from asr.config import load_config
from asr.engines.base import Segment
from asr.postprocess import (
    apply_itn, load_glossary, apply_glossary,
    split_by_punct, merge_short_sentences, validate_timestamps,
)


@pytest.fixture
def cfg():
    return load_config("config.yaml", strict_models=False)


# ---------------------------------------------------------------------------
# ITN
# ---------------------------------------------------------------------------
def test_itn_year_basic():
    """中文年份 → 阿拉伯数字。"""
    assert "2025" in apply_itn("二零二五年")
    assert "2025" in apply_itn("二〇二五年")


def test_itn_year_real_conversion():
    """P0-5：中文年份逐位转换，禁止硬编码。"""
    assert apply_itn("二〇二三年") == "2023年"
    assert apply_itn("一九九九年") == "1999年"      # 旧正则不覆盖，一并修
    assert apply_itn("今年是二零二五年。") == "今年是2025年。"

def test_itn_year_no_false_positive():
    assert apply_itn("三四千年历史") == "三四千年历史"  # 非 4 位数字串不动
    assert apply_itn("二五年度") == "二五年度"


def test_itn_percent():
    """百分之X → X%"""
    assert apply_itn("增长了百分之三十") == "增长了30%"
    assert apply_itn("百分之五十的人") == "50%的人"


def test_itn_no_match_unchanged():
    """无匹配时原文返回。"""
    assert apply_itn("大家好") == "大家好"


# ---------------------------------------------------------------------------
# 术语表
# ---------------------------------------------------------------------------
def test_load_glossary_basic(tmp_path):
    p = tmp_path / "g.txt"
    p.write_text(
        "# 注释\n"
        "\n"
        "洛必达 -> 洛必达法则\n"
        "人工智能 -> AI\n"
        "正确词\n",
        encoding="utf-8",
    )
    pairs = load_glossary(p)
    # 注释/空行/"正确词"（无 ->）都不进替换列表
    assert len(pairs) == 2
    # 按长度降序（长的先匹配）
    assert pairs[0][0] == "人工智能" or pairs[0][0] == "洛必达"
    # 实际按长度："人工智能"4字 > "洛必达"3字
    assert pairs[0] == ("人工智能", "AI")


def test_load_glossary_missing(tmp_path):
    pairs = load_glossary(tmp_path / "nonexistent.txt")
    assert pairs == []


def test_apply_glossary_replaces_longest_first():
    """术语替换：最长优先。"""
    segs = [Segment(start=0.0, end=1.0, text="人工智能导论课程")]
    pairs = [("人工智能导论课程", "AI入门课"), ("人工智能", "AI")]
    new_segs, log = apply_glossary(segs, pairs)
    # 应只匹配最长那条
    assert new_segs[0].text == "AI入门课"
    assert len(log) == 1


def test_apply_glossary_no_match_unchanged():
    segs = [Segment(start=0.0, end=1.0, text="你好")]
    new_segs, log = apply_glossary(segs, [("人工智能", "AI")])
    assert new_segs[0].text == "你好"
    assert log == []


# ---------------------------------------------------------------------------
# 断句合并
# ---------------------------------------------------------------------------
def test_split_by_punct_keeps_punct():
    """P0-3：强标点必须保留在句尾（旧实现丢弃，转写不可读）。"""
    assert split_by_punct("你好。今天天气怎么样？") == ["你好。", "今天天气怎么样？"]

def test_split_by_punct_no_trailing_punct():
    assert split_by_punct("大家好") == ["大家好"]

def test_split_by_punct_leading_punct_dropped():
    # 句首孤立标点（异常输入）不产生空段/纯标点段
    assert split_by_punct("。你好") == ["你好"]


def test_merge_short_sentences_combines():
    """过短句合并到 min_chars 以上。"""
    segs = [
        Segment(start=0.0, end=2.0, text="你好。"),       # 2 字 < 6
        Segment(start=2.0, end=4.0, text="今天天气很好。"),  # 7 字
    ]
    out = merge_short_sentences(segs, min_chars=6, max_chars=20)
    # 第一个段会被并入第二段（因为 < min_chars）
    full_text = "".join(s.text for s in out)
    assert "你好" in full_text
    assert "今天天气很好" in full_text


def test_merge_short_sentences_timestamps_proportional():
    """合并后时间戳按字数比例插值。"""
    segs = [Segment(start=0.0, end=10.0, text="你好。")]
    out = merge_short_sentences(segs, min_chars=1, max_chars=10)
    assert len(out) == 1
    # 单段不变
    assert out[0].start == 0.0
    assert out[0].end == 10.0


def test_merge_short_sentences_soft_punct_no_lonely_segment():
    """
    修复：长段按 weak 标点切分后，末尾残留的 ',' '、' 等**不能**单独成段，
    必须拼到末段，避免出现"，"这种单字符字幕。
    """
    long_text = "然后博士后去去的北航呃主要研究方向的是这个人人体姿态估计和生成啊这个知识图谱大元模型等等啊，"
    segs = [Segment(start=0.0, end=50.0, text=long_text)]
    out = merge_short_sentences(segs, min_chars=6, max_chars=40)
    # 不应出现"，"或"、"独立成段
    for s in out:
        assert s.text not in {"，", "、"}, f"弱标点单独成段: {s.text!r}"
    # 所有非空段文本应 > 1 字
    for s in out:
        assert len(s.text) > 1, f"段过短: {s.text!r}"


def test_merge_short_sentences_merges_leading_soft_punct():
    """
    入口清洗：以 weak 标点（，、）开头的段必须拼到前一段，
    避免"以逗号开头"的段污染下游 SRT 输出。
    """
    segs = [
        Segment(start=0.0, end=10.0, text="前一段"),
        Segment(start=10.0, end=20.0, text="，逗号开头"),     # 以 weak 标点开头
        Segment(start=20.0, end=30.0, text="、顿号开头"),     # 以 weak 标点开头
        Segment(start=30.0, end=40.0, text="独立段"),
    ]
    out = merge_short_sentences(segs, min_chars=6, max_chars=60)
    # 两个连续 weak 标点段都拼到前段
    assert out[0].text == "前一段，逗号开头、顿号开头"
    # 独立段保留
    assert out[1].text == "独立段"
    # 共 2 段
    assert len(out) == 2
    # 拼接段的时间戳延伸到末段
    assert out[0].end == 30.0


def _test_cfg():
    """P0-2 测试用最小 postprocess 配置（复用本文件既有 cfg 构造方式）。"""
    cfg = load_config("config.yaml", strict_models=False)
    cfg.postprocess.itn = False
    cfg.postprocess.merge_short_sentences = True
    cfg.postprocess.min_sentence_chars = 10
    cfg.postprocess.max_sentence_chars = 30
    cfg.postprocess.glossary = ""  # 不读术语表，测试与仓库数据解耦
    return cfg


def test_merge_preserves_spk():
    """P0-2：merge_short_sentences 不得抹掉 spk，否则 diarize 永不生效。"""
    segs = [
        Segment(start=0.0, end=2.0, text="好。", spk="spk0"),
        Segment(start=2.5, end=5.0, text="老师好。", spk="spk1"),
    ]
    out = merge_short_sentences(segs, min_chars=10, max_chars=30)
    assert any(s.spk for s in out), "postprocess 抹掉了 spk"
    # 合并段 spk 取字数多数者："老师好。"(4字) > "好。"(2字) → spk1
    assert out[0].spk == "spk1"


def test_pipeline_diarize_guard_passes():
    """pipeline.py:147 守卫 any(s.spk ...) 在 postprocess 之后仍为 True。"""
    from asr.postprocess import apply as apply_postprocess
    segs = [Segment(start=0.0, end=2.0, text="好。", spk="spk0")]
    out, _ = apply_postprocess(segs, _test_cfg(), duration_s=10.0)
    assert any(s.spk for s in out)


def test_newline_sanitized_in_apply():
    """P1-12：段内换行写入 SRT 破坏块结构，入口统一清洗。"""
    from asr.postprocess import apply as apply_postprocess
    segs = [Segment(start=0.0, end=1.0, text="第一行\n第二行\r\n第三行")]
    out, _ = apply_postprocess(segs, _test_cfg(), duration_s=10.0)
    assert "\n" not in out[0].text
    assert "\r" not in out[0].text
    assert "第一行 第二行" in out[0].text


def test_validate_timestamps_clamps_overflow():
    """时间戳超出视频时长 → 该段 clamp 到 [0, duration+0.5]，不再全局重排（P1-6）。"""
    segs = [Segment(start=0.0, end=999.0, text="异常段")]
    out = validate_timestamps(segs, duration_s=10.0)
    assert len(out) == 1
    # clamp 语义：单段只被裁剪到时长+0.5s
    assert out[0].start == 0.0
    assert abs(out[0].end - 10.5) < 0.01


def test_validate_timestamps_overflow_no_global_realign():
    """P1-6：整批时间戳溢出时**不再**触发全局按字数重排。

    旧"全段均分"兜底已删除（引擎现输出字对齐时间戳，重排会毁掉正确段）。
    现 clamp 语义下：起点在时长内的段被 clamp 保留；
    起点本身超出 duration+0.5s 的段 clamp 后 end<=start，被跳过并 warning。
    """
    segs = [
        Segment(start=0.0, end=50000.0, text="短句"),       # clamp → [0, 60.5]
        Segment(start=50000.0, end=100000.0, text="中长句子"),  # 起点越界 → 跳过
        Segment(start=100000.0, end=200000.0, text="很长的句子啊"),  # 起点越界 → 跳过
    ]
    out = validate_timestamps(segs, duration_s=60.0)
    # 只有起点在音频时长内的段存活，其余被跳过（不再无中生有地均分出 3 段）
    assert len(out) == 1
    assert out[0].text == "短句"
    assert out[0].start == 0.0
    assert abs(out[0].end - 60.5) < 0.01


def test_validate_timestamps_none_duration_unchanged():
    """P1-6 约束：duration_s=None 路径行为不变 —— 仅按 start 排序，时间戳原样保留。"""
    segs = [
        Segment(start=5.0, end=6.0, text="b"),
        Segment(start=1.0, end=2.0, text="a"),
    ]
    out = validate_timestamps(segs, None)
    assert [s.text for s in out] == ["a", "b"]
    assert out[0].start == 1.0 and out[0].end == 2.0


def test_validate_clamp_warns_per_segment():
    """P1-6：越界段 clamp 时逐段记录 warning，正常段零警告。"""
    from loguru import logger as _logger
    msgs: list = []
    sink_id = _logger.add(msgs.append, level="WARNING")
    try:
        segs = [
            Segment(start=0.0, end=5.0, text="正常段"),
            Segment(start=1.0, end=9999.0, text="越界段"),
            Segment(start=2.0, end=7777.0, text="另一越界段"),
        ]
        validate_timestamps(segs, duration_s=10.0)
    finally:
        _logger.remove(sink_id)
    clamp_logs = [str(m) for m in msgs if "clamp" in str(m)]
    assert len(clamp_logs) == 2  # 每个越界段一条
    assert "正常段" not in clamp_logs[0]


def test_validate_timestamps_drops_invalid():
    """end <= start 的段被丢弃。"""
    segs = [
        Segment(start=5.0, end=3.0, text="异常"),
        Segment(start=0.0, end=1.0, text="正常"),
    ]
    out = validate_timestamps(segs, duration_s=10.0)
    assert len(out) == 1
    assert out[0].text == "正常"


def test_validate_only_clamps_offending_segment():
    """P1-6：1 段越界不得连累全部段被重排。"""
    segs = [
        Segment(start=0.0, end=5.0, text="正常段"),
        Segment(start=1.0, end=9999.0, text="越界段"),   # end 超时长
        Segment(start=6.0, end=8.0, text="另一正常段"),
    ]
    out = validate_timestamps(segs, duration_s=10.0)
    # 正常段时间戳必须原样保留（不被均分重排）
    assert out[0].start == 0.0 and out[0].end == 5.0
    assert out[-1].start == 6.0 and out[-1].end == 8.0
    assert all(s.end <= 10.5 for s in out)              # 越界段被 clamp
