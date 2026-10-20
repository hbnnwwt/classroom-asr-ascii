"""
视频拼接合并：编码一致 → ffmpeg concat 流拷贝（秒级、无损）；
不一致 → 全部段按同一目标参数转码后再拼。

核心不变式：concat 只在全部段落签名完全一致时执行（-c copy）。
不一致时**所有**段（含第一段）用同一组参数重编码——不猜"转码产物
恰好与某段原生参数一致"，参数一致由构造保证，不是靠运气。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from loguru import logger

from .media import MediaInfo, probe
from .utils import ensure_dir, run_cmd, which_or_die


class IncompatibleMediaError(RuntimeError):
    """段落编码不一致且未指定转码目标。"""


# 流拷贝白名单：这些编码进 mp4 容器安全，其余（pcm/vp9/...）一律走转码，
# 绝不产出"打不开"的文件。
COPY_SAFE_VIDEO = {"h264", "hevc"}
COPY_SAFE_AUDIO = {"aac", "mp3", "ac3"}

# 预设转码目标：编码器 + CRF
PRESETS = {"h264": ("libx264", 23), "h265": ("libx265", 28)}

# "以第一段为基准"：编码家族 → 编码器
ENCODER_BY_CODEC = {"h264": "libx264", "hevc": "libx265"}


# ---------------------------------------------------------------------------
# 编码签名
# ---------------------------------------------------------------------------
def parse_fps(stream: dict) -> float:
    """avg_frame_rate "30000/1001" → 29.97（3 位小数）。"""
    raw = stream.get("avg_frame_rate") or "0/1"
    try:
        num, _, den = raw.partition("/")
        den_i = int(den) if den else 1
        return round(int(num) / den_i, 3) if den_i else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def signature(info: MediaInfo) -> tuple:
    """决定流拷贝可行性的编码签名（元组，可比较可哈希）。"""
    v = info.video_streams[0]
    a = info.audio_streams[0]
    return (
        v.get("codec_name"), v.get("profile"),
        v.get("width"), v.get("height"), v.get("pix_fmt"), parse_fps(v),
        a.get("codec_name"),
        int(a.get("sample_rate") or 0),
        int(a.get("channels") or 0),
    )


def copy_compatible(infos: Sequence[MediaInfo]) -> bool:
    """全部段落白名单编码 + 签名一致 → 可流拷贝。"""
    if not infos:
        return False
    if len({signature(i) for i in infos}) != 1:
        return False
    return all(
        i.video_streams[0].get("codec_name") in COPY_SAFE_VIDEO
        and i.audio_streams[0].get("codec_name") in COPY_SAFE_AUDIO
        for i in infos
    )


def anchor_supported(info: MediaInfo) -> bool:
    """第一段能否当转码基准（编码家族有映射编码器）。"""
    return info.video_streams[0].get("codec_name") in ENCODER_BY_CODEC


def _validate_streams(infos: Sequence[MediaInfo]) -> None:
    for i in infos:
        if not i.video_streams:
            raise ValueError(f"缺少视频流，无法拼接: {i.path.name}")
        if not i.audio_streams:
            raise ValueError(f"缺少音轨，无法拼接: {i.path.name}")


# ---------------------------------------------------------------------------
# ffmpeg 命令
# ---------------------------------------------------------------------------
def _fmt_fps(fps: float) -> str:
    """30.0 → "30"，29.97 → "29.97"（ffmpeg -r 接受两者，整数更干净）。"""
    return str(int(fps)) if fps == int(fps) else str(fps)


def _write_concat_list(files: Sequence[Path], list_path: Path) -> None:
    """写 concat demuxer 列表文件（绝对路径、正斜杠、单引号转义）。"""
    lines = []
    for f in files:
        p = str(Path(f).resolve()).replace("\\", "/")
        p = p.replace("'", "'\\''")
        lines.append(f"file '{p}'")
    list_path.write_text("\n".join(lines), encoding="utf-8")


def concat(files: Sequence[Path], out: Path) -> Path:
    """流拷贝拼接（要求全部段落签名一致，调用方保证）。"""
    ffmpeg = which_or_die("ffmpeg")
    out = Path(out)
    ensure_dir(out.parent)
    list_file = out.parent / f".{out.stem}_concat.txt"
    _write_concat_list(files, list_file)
    cmd = [
        ffmpeg, "-y", "-nostdin", "-threads", "0",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", "-movflags", "+faststart",
        str(out),
    ]
    logger.info(f"拼接: {len(files)} 段流拷贝 -> {out.name}")
    try:
        proc = run_cmd(cmd, timeout=3600)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(
                f"ffmpeg 拼接失败 (rc={proc.returncode})\nstderr:\n{proc.stderr[-2000:]}"
            )
    finally:
        list_file.unlink(missing_ok=True)
    return out


def _build_transcode_cmd(
    ffmpeg: str, src: Path, dst: Path,
    encoder: str, crf: int, w: int, h: int, fps: float, sr: int, ch: int,
) -> List[str]:
    cmd = [
        ffmpeg, "-y", "-nostdin", "-threads", "0",
        "-i", str(src),
        # 直接缩放到锚点分辨率（不 pad：混拼横竖屏属病态输入，拉伸可接受）
        "-vf", f"scale={w}:{h}",
        "-r", _fmt_fps(fps),
        "-c:v", encoder, "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", str(sr), "-ac", str(ch),
    ]
    if encoder == "libx265":
        cmd += ["-tag:v", "hvc1"]  # QuickTime 兼容
    cmd.append(str(dst))
    return cmd


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
def merge_videos(
    parts: Sequence[Path],
    out: Path,
    target: Optional[str] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> Path:
    """
    拼接多段视频到 out（mp4）。
    - target=None：要求全部签名一致，否则抛 IncompatibleMediaError
    - target="h264"|"h265"：预设编码器，参数（分辨率/帧率/音频）锚定第一段
    - target="first"：编码器取第一段的编码家族，其余参数同上
    on_progress(ratio, msg)：0.0~1.0 粗粒度进度（按段）。
    """
    parts = [Path(p) for p in parts]
    out = Path(out)

    def _report(ratio: float, msg: str) -> None:
        if on_progress:
            try:
                on_progress(ratio, msg)
            except Exception:
                pass  # 回调失败不影响合并

    _report(0.0, f"探测 {len(parts)} 段视频")
    infos = [probe(p) for p in parts]
    _validate_streams(infos)
    ensure_dir(out.parent)

    # 快路径：签名一致 → 流拷贝
    if target is None:
        if not copy_compatible(infos):
            raise IncompatibleMediaError(
                "段落编码不一致，需选择转码类型后再合并"
            )
        _report(0.5, "编码一致，无损拼接")
        concat(parts, out)
        _report(1.0, "拼接完成")
        return out

    # 慢路径：全部段统一参数转码 → 拼接
    anchor = infos[0]
    if target in PRESETS:
        encoder, crf = PRESETS[target]
    elif target == "first":
        vcodec = anchor.video_streams[0].get("codec_name")
        if vcodec not in ENCODER_BY_CODEC:
            raise ValueError(
                f"第一段编码 {vcodec} 不能作为转码基准（仅支持 h264/hevc）"
            )
        encoder = ENCODER_BY_CODEC[vcodec]
        crf = 23 if encoder == "libx264" else 28
    else:
        raise ValueError(f"未知转码目标: {target}")

    v, a = anchor.video_streams[0], anchor.audio_streams[0]
    w, h, fps = v.get("width"), v.get("height"), parse_fps(v)
    sr = int(a.get("sample_rate") or 48000)
    ch = int(a.get("channels") or 2)
    ffmpeg = which_or_die("ffmpeg")

    tmpdir = Path(tempfile.mkdtemp(prefix="merge-"))
    try:
        transcoded: List[Path] = []
        n = len(parts)
        for i, p in enumerate(parts):
            _report(i / n, f"转码第 {i + 1}/{n} 段: {p.name}")
            dst = tmpdir / f"part{i:03d}.mp4"
            cmd = _build_transcode_cmd(ffmpeg, p, dst, encoder, crf, w, h, fps, sr, ch)
            logger.info(f"转码: {p.name} -> {encoder}@{w}x{h}")
            proc = run_cmd(cmd, timeout=7200)
            if proc.returncode != 0 or not dst.exists():
                raise RuntimeError(
                    f"ffmpeg 转码失败 (rc={proc.returncode}): {p.name}\n"
                    f"stderr:\n{proc.stderr[-2000:]}"
                )
            transcoded.append(dst)
        concat(transcoded, out)
        _report(1.0, "拼接完成")
        return out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
