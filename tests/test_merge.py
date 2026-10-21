"""merge.py 单测：多段音轨拼接（wave 模块造真 WAV，纯 Python 零子进程）。

merge_audios 的核心不变式：逐段 extract_audio 产出统一格式
（16k/单声道/pcm_s16le，格式一致由构造保证），拼接 = wave 数据块顺序追加。
编码差异在音频层不存在——不是被解决，是不存在。
"""
import struct
import wave
from pathlib import Path

import pytest

from asr import merge


# ---------------------------------------------------------------------------
# 构造辅助：真 WAV（16k/mono/16bit，与 extract_audio 产出同参数）
# ---------------------------------------------------------------------------
SR = 16000


def _write_wav(path: Path, seconds: float, sample: int = 1000) -> Path:
    """写一个全部采样值相同的 WAV（内容可判别，验证拼接顺序）。"""
    n = int(SR * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(struct.pack(f"<{n}h", *[sample] * n))
    return path


def _read_wav(path: Path):
    with wave.open(str(path), "rb") as w:
        return (
            (w.getnchannels(), w.getsampwidth(), w.getframerate()),
            w.getnframes(),
            w.readframes(w.getnframes()),
        )


def _install_fake_extract(monkeypatch, calls, samples=None, seconds=None):
    """把 merge 模块的 extract_audio 换成假实现：按序生成真 WAV。

    samples: 每段的采样值（默认递增 1000/2000/...），拼接顺序可验证。
    seconds: 每段时长（默认 0.5s）。
    """
    seq = iter(samples or [1000 * (i + 1) for i in range(99)])
    secs = iter(seconds or [0.5] * 99)

    def fake_extract(src, dst, cfg, audio_track=None):
        calls.append(Path(src).name)
        return _write_wav(Path(dst), seconds=next(secs), sample=next(seq))

    monkeypatch.setattr(merge, "extract_audio", fake_extract)


CFG = object()  # 假配置：真实现只透传给 extract_audio（已 mock）


# ---------------------------------------------------------------------------
# 拼接正确性
# ---------------------------------------------------------------------------
def test_merge_audios_concatenates_in_order(monkeypatch, tmp_path):
    """段落按给定顺序字节级拼接：第 1 段采样在前，第 2 段在后。"""
    calls = []
    _install_fake_extract(monkeypatch, calls, samples=[1000, 2000],
                          seconds=[0.4, 0.6])
    parts = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    out = tmp_path / "merged.wav"

    wav_path, durations = merge.merge_audios(parts, out, CFG)

    assert wav_path == out
    assert durations == pytest.approx([0.4, 0.6]), "各段精确时长（wave 头）"
    params, nframes, data = _read_wav(out)
    assert params == (1, 2, SR), "拼接产物保持 16k/mono/16bit"
    assert nframes == int(SR * 1.0), "总帧数 = 各段之和"
    # 前 40% 是第一段（采样 1000），后 60% 是第二段（2000）
    f40 = struct.unpack(f"<{int(SR * 0.4)}h", data[: int(SR * 0.4) * 2])
    s60 = struct.unpack(f"<{int(SR * 0.6)}h", data[int(SR * 0.4) * 2:])
    assert set(f40) == {1000} and set(s60) == {2000}, "顺序 = 段落顺序"


def test_merge_audios_extracts_every_part(monkeypatch, tmp_path):
    """每段各抽一次音轨，顺序与输入一致。"""
    calls = []
    _install_fake_extract(monkeypatch, calls)
    parts = [tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "c.mp3"]

    merge.merge_audios(parts, tmp_path / "merged.wav", CFG)

    assert calls == ["a.mp4", "b.mp4", "c.mp3"]


def test_merge_audios_single_part(monkeypatch, tmp_path):
    """单段也能拼（自然退化，无特殊分支）。"""
    _install_fake_extract(monkeypatch, [], samples=[700])
    out, durations = merge.merge_audios([tmp_path / "solo.mp4"], tmp_path / "m.wav", CFG)
    _, _, data = _read_wav(out)
    assert set(struct.unpack(f"<{len(data) // 2}h", data)) == {700}
    assert durations == pytest.approx([0.5])


def test_merge_audios_rejects_mismatched_params(monkeypatch, tmp_path):
    """参数不一致（不该发生，防御性断言）→ 明确报错，不产出坏文件。"""
    rates = iter([SR, 44100])  # 第一段 16k，第二段 44.1k（参数不一致）

    def fake_extract(src, dst, cfg, audio_track=None):
        p = Path(dst)
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(next(rates))
            w.writeframes(struct.pack("<100h", *([1] * 100)))
        return p

    monkeypatch.setattr(merge, "extract_audio", fake_extract)
    parts = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    out = tmp_path / "merged.wav"

    with pytest.raises(ValueError, match="b|参数"):
        merge.merge_audios(parts, out, CFG)
    assert not out.exists(), "失败不得留下半截产物"


def test_merge_audios_extract_failure_propagates(monkeypatch, tmp_path):
    """抽音轨失败（如无音轨）→ 原样上抛。"""
    def boom(src, dst, cfg, audio_track=None):
        raise RuntimeError("视频无音轨")

    monkeypatch.setattr(merge, "extract_audio", boom)
    with pytest.raises(RuntimeError, match="无音轨"):
        merge.merge_audios([tmp_path / "a.mp4", tmp_path / "b.mp4"],
                           tmp_path / "m.wav", CFG)


def test_merge_audios_reports_progress(monkeypatch, tmp_path):
    """on_progress 收到 0..1 单调进度，完成时 1.0。"""
    _install_fake_extract(monkeypatch, [])
    ratios = []
    merge.merge_audios(
        [tmp_path / "a.mp4", tmp_path / "b.mp4"], tmp_path / "m.wav", CFG,
        on_progress=lambda r, msg: ratios.append(r),
    )
    assert ratios[0] == 0.0
    assert ratios == sorted(ratios), "进度单调递增"
    assert ratios[-1] == 1.0


def test_merge_audios_cleans_tmpdir(monkeypatch, tmp_path, tmp_path_factory):
    """中间 WAV 不留垃圾（tmpdir 用后即删）。"""
    made = []

    def fake_tmpdir(prefix="asr-"):
        d = tmp_path_factory.mktemp(prefix)
        made.append(d)
        return d

    monkeypatch.setattr(merge, "make_tmpdir", fake_tmpdir)
    _install_fake_extract(monkeypatch, [])
    merge.merge_audios([tmp_path / "a.mp4", tmp_path / "b.mp4"],
                       tmp_path / "m.wav", CFG)
    assert made and not any(p.exists() for p in made), "临时目录应被删除"


# ---------------------------------------------------------------------------
# write_part_srts：整体时间轴 → 每段一份独立 SRT（时间归零）
# ---------------------------------------------------------------------------
def _seg(start, end, text):
    return {"start": start, "end": end, "text": text}


def test_write_part_srts_splits_by_boundary(tmp_path):
    """段起点落在哪段归哪段；时间归零配原视频；跨缝段不撕开。"""
    segs = [
        _seg(0.1, 0.4, "第一段话"),   # part0
        _seg(0.9, 1.3, "跨缝话"),     # 起点在 part0 → 归 part0（不切文本）
        _seg(1.2, 1.5, "第二段话"),   # part1
    ]
    files = merge.write_part_srts(
        segs, durations=[1.0, 1.0], stems=["a", "b"], out_dir=tmp_path)

    assert [p.name for p in files] == ["00_a.srt", "01_b.srt"]
    a = (tmp_path / "00_a.srt").read_text(encoding="utf-8")
    b = (tmp_path / "01_b.srt").read_text(encoding="utf-8")

    assert "第一段话" in a and "跨缝话" in a
    assert "00:00:00,100 --> 00:00:00,400" in a, "part0 时间归零"
    assert "00:00:00,900 --> 00:00:01,300" in a, "跨缝段保留原相对时长"
    assert "第二段话" in b and "跨缝话" not in b
    assert "00:00:00,200 --> 00:00:00,500" in b, "part1 减去 1.0s 偏移"
    assert b.splitlines()[0] == "1", "每份 SRT 序号独立从 1 起"


def test_write_part_srts_empty_part_still_writes_file(tmp_path):
    """某段无语音 → 仍写出空 SRT（文件与段一一对应，索引不错位）。"""
    files = merge.write_part_srts(
        [_seg(0.1, 0.4, "只有第一段有话")],
        durations=[1.0, 1.0], stems=["a", "b"], out_dir=tmp_path)
    assert len(files) == 2
    b = (tmp_path / "01_b.srt").read_text(encoding="utf-8")
    assert b.strip() == "", "空段 → 空 SRT"
