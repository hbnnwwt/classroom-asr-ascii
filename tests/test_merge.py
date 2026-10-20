"""merge.py 单测：编码签名判定 + 拼接/转码编排（monkeypatch 假 ffmpeg，不跑真实进程）。

merge 的核心不变式：concat 只在全部段落签名一致时执行；
不一致时全部段落按同一目标参数转码后再 concat——绝无"部分转码部分拷贝"的混合。
"""
import subprocess
from pathlib import Path

import pytest

from asr import merge
from asr.media import MediaInfo


# ---------------------------------------------------------------------------
# 构造辅助：ffprobe 流字典的形状与真实输出一致（sample_rate 是字符串）
# ---------------------------------------------------------------------------
def _vstream(codec="h264", profile="High", w=1920, h=1080, pix="yuv420p", fps="30/1"):
    return {
        "codec_type": "video", "codec_name": codec, "profile": profile,
        "width": w, "height": h, "pix_fmt": pix, "avg_frame_rate": fps,
    }


def _astream(codec="aac", sr="48000", ch=2):
    return {"codec_type": "audio", "codec_name": codec, "sample_rate": sr, "channels": ch}


def _info(path, v=None, a=None, dur=60.0):
    return MediaInfo(
        path=Path(path), duration_s=dur,
        audio_streams=[a if a is not None else _astream()],
        video_streams=[v if v is not None else _vstream()],
    )


def _install_fake_ffmpeg(monkeypatch, calls, outputs=None):
    """把 merge 模块的 ffmpeg 依赖换成假实现：记录命令 + 生成输出文件。"""
    monkeypatch.setattr(merge, "which_or_die", lambda name: name)

    def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"FAKE")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(merge, "run_cmd", fake_run_cmd)


def _install_fake_probe(monkeypatch, infos):
    """按路径返回预置 MediaInfo。"""
    table = {str(Path(i.path)): i for i in infos}
    monkeypatch.setattr(merge, "probe", lambda p: table[str(Path(p))])


# ---------------------------------------------------------------------------
# signature：编码签名
# ---------------------------------------------------------------------------
def test_signature_identical_for_same_params():
    """相同参数 → 相同签名元组。"""
    assert merge.signature(_info("a.mp4")) == merge.signature(_info("b.mp4"))


def test_signature_differs_on_resolution():
    a = _info("a.mp4")
    b = _info("b.mp4", v=_vstream(w=1280, h=720))
    assert merge.signature(a) != merge.signature(b)


def test_signature_differs_on_fps():
    a = _info("a.mp4", v=_vstream(fps="30/1"))
    b = _info("b.mp4", v=_vstream(fps="25/1"))
    assert merge.signature(a) != merge.signature(b)


def test_signature_parses_fractional_fps():
    """avg_frame_rate "30000/1001" → 29.97（与 30/1 不同签名）。"""
    a = _info("a.mp4", v=_vstream(fps="30000/1001"))
    b = _info("b.mp4", v=_vstream(fps="30/1"))
    assert merge.signature(a) != merge.signature(b)
    # 签名里 fps 取 3 位小数
    assert any(abs(x - 29.97) < 0.001 for x in merge.signature(a) if isinstance(x, float))


def test_signature_differs_on_audio_params():
    a = _info("a.mp4", a=_astream(sr="44100"))
    b = _info("b.mp4", a=_astream(sr="48000"))
    assert merge.signature(a) != merge.signature(b)


# ---------------------------------------------------------------------------
# copy_compatible：流拷贝白名单 + 签名一致
# ---------------------------------------------------------------------------
def test_copy_compatible_all_whitelisted_and_identical():
    infos = [_info("a.mp4"), _info("b.mp4"), _info("c.mp4")]
    assert merge.copy_compatible(infos) is True


def test_copy_compatible_rejects_pcm_audio():
    """pcm 音频进 mp4 容器会产出坏文件 → 不允许流拷贝。"""
    infos = [_info("a.mp4"), _info("b.mp4", a=_astream(codec="pcm_s16le"))]
    assert merge.copy_compatible(infos) is False


def test_copy_compatible_rejects_vp9_video():
    infos = [_info("a.mp4"), _info("b.mp4", v=_vstream(codec="vp9"))]
    assert merge.copy_compatible(infos) is False


def test_copy_compatible_rejects_signature_mismatch():
    """编码都在白名单内但分辨率不同 → 仍然不能流拷贝。"""
    infos = [_info("a.mp4"), _info("b.mp4", v=_vstream(w=1280, h=720))]
    assert merge.copy_compatible(infos) is False


# ---------------------------------------------------------------------------
# merge_videos：兼容路径（流拷贝）
# ---------------------------------------------------------------------------
def test_merge_compatible_uses_stream_copy(monkeypatch, tmp_path):
    """签名一致 → concat demuxer + -c copy，一次 ffmpeg，零转码。"""
    calls = []
    _install_fake_ffmpeg(monkeypatch, calls)
    parts = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for p in parts:
        p.write_bytes(b"X")
    _install_fake_probe(monkeypatch, [_info(p) for p in parts])
    out = tmp_path / "merged.mp4"

    result = merge.merge_videos(parts, out)

    assert result == out
    assert out.exists()
    assert len(calls) == 1, "兼容路径只应有 1 次 ffmpeg 调用（concat）"
    cmd = calls[0]
    assert "-f" in cmd and "concat" in cmd
    assert "-c" in cmd and "copy" in cmd[cmd.index("-c") + 1]


def test_merge_incompatible_without_target_raises(monkeypatch, tmp_path):
    """签名不一致且未选转码目标 → IncompatibleMediaError。"""
    calls = []
    _install_fake_ffmpeg(monkeypatch, calls)
    parts = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    _install_fake_probe(monkeypatch, [
        _info(parts[0]), _info(parts[1], v=_vstream(w=1280, h=720)),
    ])

    with pytest.raises(merge.IncompatibleMediaError):
        merge.merge_videos(parts, tmp_path / "merged.mp4")
    assert calls == [], "失败路径不得启动任何 ffmpeg"


# ---------------------------------------------------------------------------
# merge_videos：转码路径（统一参数重编码全部段 → concat）
# ---------------------------------------------------------------------------
def test_merge_preset_h264_transcodes_all_then_concat(monkeypatch, tmp_path):
    """target=h264：每段都用 libx264+aac 统一参数转码，再 concat -c copy。"""
    calls = []
    _install_fake_ffmpeg(monkeypatch, calls)
    parts = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    # 第二段分辨率不同（触发不兼容的典型场景）
    _install_fake_probe(monkeypatch, [
        _info(parts[0]), _info(parts[1], v=_vstream(w=1280, h=720)),
    ])
    out = tmp_path / "merged.mp4"

    merge.merge_videos(parts, out, target="h264")

    assert len(calls) == 3, "2 段转码 + 1 次 concat"
    transcodes = calls[:2]
    for cmd in transcodes:
        assert "libx264" in cmd
        assert "aac" in cmd
        # 锚点参数（第一段的 1920x1080@30）落到每一段
        i = cmd.index("-vf")
        assert cmd[i + 1] == "scale=1920:1080"
        assert cmd[cmd.index("-r") + 1] == "30"
    concat = calls[2]
    assert "concat" in concat and "copy" in concat


def test_merge_preset_h265_uses_libx265(monkeypatch, tmp_path):
    calls = []
    _install_fake_ffmpeg(monkeypatch, calls)
    parts = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    _install_fake_probe(monkeypatch, [
        _info(parts[0]), _info(parts[1], v=_vstream(codec="hevc")),
    ])

    merge.merge_videos(parts, tmp_path / "merged.mp4", target="h265")

    for cmd in calls[:2]:
        assert "libx265" in cmd


def test_merge_target_first_maps_anchor_codec(monkeypatch, tmp_path):
    """target=first：编码器取第一段的编码家族（hevc → libx265），参数锚定第一段。"""
    calls = []
    _install_fake_ffmpeg(monkeypatch, calls)
    parts = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    _install_fake_probe(monkeypatch, [
        _info(parts[0], v=_vstream(codec="hevc", w=1280, h=720, fps="25/1")),
        _info(parts[1], v=_vstream(codec="h264")),
    ])

    merge.merge_videos(parts, tmp_path / "merged.mp4", target="first")

    for cmd in calls[:2]:
        assert "libx265" in cmd, "第一段是 hevc → 全部用 libx265"
        assert cmd[cmd.index("-vf") + 1] == "scale=1280:720", "分辨率锚定第一段"
        assert cmd[cmd.index("-r") + 1] == "25", "帧率锚定第一段"


def test_merge_target_first_unsupported_anchor_codec_raises(monkeypatch, tmp_path):
    """第一段编码无映射（如 mpeg4）→ first 目标不可用，明确报错。"""
    calls = []
    _install_fake_ffmpeg(monkeypatch, calls)
    parts = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    _install_fake_probe(monkeypatch, [
        _info(parts[0], v=_vstream(codec="mpeg4")),
        _info(parts[1], v=_vstream(codec="h264")),
    ])

    with pytest.raises(ValueError, match="第一段"):
        merge.merge_videos(parts, tmp_path / "merged.mp4", target="first")


def test_merge_rejects_part_without_video_stream(monkeypatch, tmp_path):
    """音频文件混进视频拼接 → 明确报错（信息含文件名）。"""
    _install_fake_ffmpeg(monkeypatch, [])
    parts = [tmp_path / "a.mp4", tmp_path / "b.mp3"]
    _install_fake_probe(monkeypatch, [
        _info(parts[0]),
        MediaInfo(path=parts[1], duration_s=10.0,
                  audio_streams=[_astream()], video_streams=[]),
    ])

    with pytest.raises(ValueError, match="b.mp3"):
        merge.merge_videos(parts, tmp_path / "merged.mp4")


def test_merge_rejects_part_without_audio_stream(monkeypatch, tmp_path):
    """无音轨的视频 → 报错（转写流程必须有音轨）。"""
    _install_fake_ffmpeg(monkeypatch, [])
    parts = [tmp_path / "a.mp4"]
    _install_fake_probe(monkeypatch, [
        MediaInfo(path=parts[0], duration_s=10.0,
                  audio_streams=[], video_streams=[_vstream()]),
    ])

    with pytest.raises(ValueError, match="音轨"):
        merge.merge_videos(parts, tmp_path / "merged.mp4")


def test_merge_ffmpeg_failure_raises(monkeypatch, tmp_path):
    """ffmpeg 返回非 0 → RuntimeError 带 stderr。"""
    monkeypatch.setattr(merge, "which_or_die", lambda name: name)
    monkeypatch.setattr(merge, "run_cmd", lambda cmd, **kw: subprocess.CompletedProcess(
        args=cmd, returncode=1, stdout="", stderr="boom"))
    parts = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    _install_fake_probe(monkeypatch, [_info(p) for p in parts])

    with pytest.raises(RuntimeError, match="boom"):
        merge.merge_videos(parts, tmp_path / "merged.mp4")


def test_concat_list_escapes_single_quotes(monkeypatch, tmp_path):
    """文件名含单引号 → concat 列表正确转义（否则 ffmpeg 解析错乱）。"""
    calls = []
    captured = {}

    def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        # 捕获 concat 列表文件内容
        if "concat" in cmd:
            list_file = Path(cmd[cmd.index("-i") + 1])
            captured["list"] = list_file.read_text(encoding="utf-8")
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"FAKE")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(merge, "which_or_die", lambda name: name)
    monkeypatch.setattr(merge, "run_cmd", fake_run_cmd)
    parts = [tmp_path / "it's a.mp4", tmp_path / "b.mp4"]
    _install_fake_probe(monkeypatch, [_info(p) for p in parts])

    merge.merge_videos(parts, tmp_path / "merged.mp4")

    assert "file '" in captured["list"]
    assert "\\'" in captured["list"], "单引号必须转义为 '\\''"


def test_merge_reports_progress(monkeypatch, tmp_path):
    """on_progress 收到 0..1 单调递增的进度，完成时到 1.0。"""
    calls = []
    _install_fake_ffmpeg(monkeypatch, calls)
    parts = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    _install_fake_probe(monkeypatch, [
        _info(parts[0]), _info(parts[1], v=_vstream(w=1280, h=720)),
    ])
    ratios = []

    merge.merge_videos(parts, tmp_path / "merged.mp4", target="h264",
                       on_progress=lambda r, msg: ratios.append(r))

    assert ratios[0] == 0.0
    assert ratios == sorted(ratios), "进度单调递增"
    assert ratios[-1] == 1.0
