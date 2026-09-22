"""media.py 单测：probe 边界行为（monkeypatch 假 ffprobe，不跑真实 ffmpeg）。"""
import json
import subprocess
from pathlib import Path

import pytest

from asr import media


def _fake_probe_env(monkeypatch, payload: dict) -> None:
    """把 asr.media 的 ffprobe 依赖替换为返回 payload 的假实现。"""
    fake = subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0,
        stdout=json.dumps(payload), stderr="",
    )
    monkeypatch.setattr(media, "which_or_die", lambda name: name)
    monkeypatch.setattr(media, "run_cmd", lambda cmd, **kw: fake)


@pytest.mark.parametrize("raw_dur", ["N/A", None, "not-a-number"])
def test_probe_invalid_duration_returns_zero(monkeypatch, raw_dur):
    """P1-9：ffprobe duration 为 "N/A"/缺失/非法时不得抛异常，降级为 0.0。"""
    format_obj = {} if raw_dur is None else {"duration": raw_dur}
    _fake_probe_env(monkeypatch, {"format": format_obj, "streams": []})
    info = media.probe(Path("dummy.mp4"))
    assert info.duration_s == 0.0
    assert not info.has_audio


def test_probe_valid_duration(monkeypatch):
    """正常路径：字符串时长正确解析为 float。"""
    _fake_probe_env(
        monkeypatch,
        {"format": {"duration": "12.5"}, "streams": [{"codec_type": "audio"}]},
    )
    info = media.probe(Path("dummy.mp4"))
    assert info.duration_s == 12.5
    assert info.has_audio
