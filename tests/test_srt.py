"""srt.py 单测。"""
from pathlib import Path

import pytest

from asr.config import OutputConfig
from asr.engines.base import Segment
from asr.srt import fmt_ts, write_srt, write_vtt, write_txt, write_md, write_json, write_all


@pytest.fixture
def cfg():
    return OutputConfig()


# ---------------------------------------------------------------------------
# fmt_ts
# ---------------------------------------------------------------------------
def test_fmt_ts_srt():
    assert fmt_ts(0.0, srt=True) == "00:00:00,000"
    assert fmt_ts(1.5, srt=True) == "00:00:01,500"
    assert fmt_ts(62.345, srt=True) == "00:01:02,345"
    assert fmt_ts(3661.999, srt=True) == "01:01:01,999"


def test_fmt_ts_vtt():
    assert fmt_ts(1.5, srt=False) == "00:00:01.500"
    assert fmt_ts(62.345, srt=False) == "00:01:02.345"


def test_fmt_ts_negative_clamped():
    assert fmt_ts(-1.0, srt=True) == "00:00:00,000"


# ---------------------------------------------------------------------------
# write_srt
# ---------------------------------------------------------------------------
def test_write_srt_basic(tmp_path, cfg):
    out = tmp_path / "test.srt"
    segs = [
        Segment(start=1.0, end=3.0, text="大家好"),
        Segment(start=4.0, end=6.0, text="欢迎来到课堂"),
    ]
    write_srt(segs, out, cfg)
    text = out.read_text(encoding=cfg.srt_encoding)
    assert "1\n00:00:01,000 --> 00:00:03,000\n大家好" in text
    assert "2\n00:00:04,000 --> 00:00:06,000\n欢迎来到课堂" in text
    # 无空条（每段以空行结束但无连续空行）
    assert "\n\n\n" not in text


def test_write_srt_long_segment_not_split(tmp_path, cfg, caplog):
    """
    超过 srt_max_duration_s 的段**不切条**，保留原始 [start, end]。

    原因：FunASR 在长音频上返回粗粒度时间戳（一段 22s 涵盖 9 字）。
    按字符硬切条产生 3 字一段不可读。SRT 层不做切条，由 postprocess
    层按 weak 标点切分长文本，或在引擎层改进 timestamp 精度。
    """
    out = tmp_path / "test.srt"
    cfg.srt_max_duration_s = 3
    segs = [Segment(start=0.0, end=10.0, text="单段长字幕保留完整文本不切条")]
    write_srt(segs, out, cfg)
    text = out.read_text(encoding=cfg.srt_encoding)
    # 不切条 → 只有 1 条
    indices = [l for l in text.split("\n") if l.strip().isdigit()]
    assert len(indices) == 1
    # 时间戳保留原始 [0, 10]
    assert "00:00:00,000 --> 00:00:10,000" in text
    # 文本完整保留
    assert "单段长字幕保留完整文本不切条" in text


def test_write_srt_empty_text_skipped(tmp_path, cfg):
    """空文本段被跳过（不产生序号空洞）。"""
    out = tmp_path / "test.srt"
    segs = [
        Segment(start=0.0, end=1.0, text="正常"),
        Segment(start=1.0, end=2.0, text="   "),  # 仅空白
        Segment(start=2.0, end=3.0, text=""),
        Segment(start=3.0, end=4.0, text="也正常"),
    ]
    write_srt(segs, out, cfg)
    text = out.read_text(encoding=cfg.srt_encoding)
    # 序号应为 1, 2（跳过空白和空）
    indices = [l for l in text.split("\n") if l.strip().isdigit()]
    assert indices == ["1", "2"]


def test_wrap_text_merges_leading_soft_punct():
    """_wrap_text: 以 weak 标点开头的行应拼到上一行末尾。"""
    from asr.srt import _wrap_text
    # max_chars_per_line=10，文本 14 字包含"，"
    # 切分后第一行"abcdefg，"第二行"，hijkl" → 第二行应以"，"开头被合并
    text = "abcdefg，hijkl"
    lines = _wrap_text(text, max_chars_per_line=10)
    # 期望：第一行 = "abcdefg，hijkl"（合并）或不以 weak 开头
    for line in lines:
        assert line.strip() and line.strip()[0] not in {"，", "、"}, \
            f"行以 weak 标点开头: {line!r}"


def test_write_srt_no_soft_punct_lonely_line(tmp_path, cfg):
    """SRT 行内切分不应产生独立的"，"行。"""
    out = tmp_path / "test.srt"
    cfg.srt_max_chars_per_line = 10
    segs = [Segment(start=0.0, end=2.0, text="abcdefghij，klmn")]
    write_srt(segs, out, cfg)
    text = out.read_text(encoding=cfg.srt_encoding)
    for line in text.split("\n"):
        if line.strip() and "-->" not in line and not line.strip().isdigit():
            assert line.strip() not in {"，", "、"}, \
                f"独立弱标点行: {line!r}"
            assert line.strip()[0] not in {"，", "、"}, \
                f"行以 weak 标点开头: {line!r}"


def test_write_srt_line_wrap(tmp_path, cfg):
    """每行字符数不超 srt_max_chars_per_line。"""
    out = tmp_path / "test.srt"
    cfg.srt_max_chars_per_line = 10
    segs = [Segment(start=0.0, end=2.0, text="这是一句超过十个字符的字幕内容示例")]
    write_srt(segs, out, cfg)
    text = out.read_text(encoding=cfg.srt_encoding)
    for line in text.split("\n"):
        # 跳过时间戳行、序号行、空行
        if "-->" in line or line.strip().isdigit() or not line.strip():
            continue
        assert len(line) <= 10


# ---------------------------------------------------------------------------
# write_vtt / write_txt / write_md / write_json
# ---------------------------------------------------------------------------
def test_write_vtt_header(tmp_path):
    out = tmp_path / "test.vtt"
    segs = [Segment(start=1.0, end=3.0, text="test")]
    write_vtt(segs, out, OutputConfig())
    assert out.read_text(encoding="utf-8").startswith("WEBVTT")


def test_write_txt_with_ts(tmp_path):
    out = tmp_path / "test.txt"
    segs = [Segment(start=1.0, end=3.0, text="hello")]
    write_txt(segs, out, with_ts=True)
    text = out.read_text(encoding="utf-8")
    assert "[00:00:01,000] hello" in text


def test_write_md_with_speaker(tmp_path):
    out = tmp_path / "test.md"
    segs = [Segment(start=1.0, end=3.0, text="老师好", spk="spk0")]
    write_md(segs, out, with_speaker=True)
    text = out.read_text(encoding="utf-8")
    assert "**" in text
    assert "老师好" in text


def test_write_json_structure(tmp_path):
    out = tmp_path / "test.json"
    segs = [Segment(start=1.0, end=3.0, text="hello", spk="spk0")]
    write_json(segs, out, meta={"engine": "funasr"})
    import json
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "meta" in data
    assert "segments" in data
    assert data["meta"]["engine"] == "funasr"
    assert data["segments"][0]["text"] == "hello"
    assert data["segments"][0]["start"] == 1.0
    assert data["segments"][0]["spk"] == "spk0"


def test_write_all_creates_requested_formats(tmp_path, cfg):
    """write_all 按 cfg.formats 写出对应文件。"""
    cfg.formats = ["txt", "json"]
    base = tmp_path / "out" / "lecture"
    segs = [Segment(start=0.0, end=1.0, text="x")]
    written = write_all(segs, base, cfg)
    assert "txt" in written
    assert "json" in written
    assert "srt" not in written
    assert written["txt"].exists()
    assert written["json"].exists()
