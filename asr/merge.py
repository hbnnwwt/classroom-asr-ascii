"""
多段拼接：逐段抽音轨到统一格式 WAV → wave 模块数据块顺序追加。

转写只吃音轨——不合并、不转码视频。中间格式由 extract_audio 构造保证
统一（16k/单声道/pcm_s16le，同一 AppConfig），"编码不一致"在音频层
不存在：不是被解决，是不存在。拼接零子进程。

拼接时记录各段精确时长（wave 头），识别完成后据此把整体时间轴的字幕
切成每段一份独立 SRT（write_part_srts），方便配原视频单独使用。
"""
from __future__ import annotations

import wave
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from loguru import logger

from .config import AppConfig, OutputConfig
from .media import extract_audio, make_tmpdir, safe_rmtree
from .srt import _emit_srt_entry
from .utils import ensure_dir


def _concat_wavs(wavs: Sequence[Path], out: Path) -> list[float]:
    """同参数 WAV 数据块顺序拼接（wave 模块，零子进程）。返回各段时长秒。

    参数一致性由 merge_audios 的构造保证（同一 cfg 抽的轨）；先全量校验
    再写（失败不落半截文件），校验是防御：万一不一致立刻炸，绝不静默
    产出坏文件。
    """
    params = None
    durations: list[float] = []
    for p in wavs:
        with wave.open(str(p), "rb") as w:
            cur = w.getparams()
        if params is None:
            params = cur
        elif cur[:3] != params[:3]:  # nchannels/sampwidth/framerate
            raise ValueError(f"音频参数不一致，无法拼接: {p.name}")
        durations.append(cur.nframes / float(cur.framerate))

    with wave.open(str(out), "wb") as dst:
        dst.setparams(params)
        for p in wavs:
            with wave.open(str(p), "rb") as src:
                dst.writeframes(src.readframes(src.getnframes()))
    return durations


def merge_audios(
    parts: Sequence[Path],
    out: Path,
    cfg: AppConfig,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> Tuple[Path, list[float]]:
    """逐段抽音轨（统一 16k/mono/pcm_s16le）→ 拼接为单个 WAV。

    返回 (合并 WAV 路径, 各段时长秒列表)——时长是后续切分每段 SRT 的
    时间轴边界。on_progress(ratio, msg)：0.0~1.0 粗粒度进度（按段）。
    """
    parts = [Path(p) for p in parts]
    out = Path(out)
    ensure_dir(out.parent)

    def _report(ratio: float, msg: str) -> None:
        if on_progress:
            try:
                on_progress(ratio, msg)
            except Exception:
                pass  # 回调失败不影响拼接

    tmpdir = make_tmpdir(prefix="merge-")
    try:
        wavs: List[Path] = []
        n = len(parts)
        for i, p in enumerate(parts):
            _report(i / n, f"提取音轨 {i + 1}/{n}: {p.name}")
            wavs.append(extract_audio(p, tmpdir / f"part{i:03d}.wav", cfg))
        logger.info(f"拼接音轨: {len(wavs)} 段 -> {out.name}")
        durations = _concat_wavs(wavs, out)
        _report(1.0, "拼接完成")
        return out, durations
    finally:
        safe_rmtree(tmpdir)


def write_part_srts(
    segments: Sequence[dict],
    durations: Sequence[float],
    stems: Sequence[str],
    out_dir: Path,
    output_cfg: Optional[OutputConfig] = None,
) -> List[Path]:
    """把整体时间轴的字幕按段边界切成每段一份独立 SRT（时间归零）。

    归属规则：段起点落在哪段就归哪段——跨缝段整体归起始段，不撕开
    文本（相对时长可能略超出该段，播放器无害）。空段也写出空文件，
    保证文件与段一一对应、索引不错位。
    文件名 {序号}_{段名}.srt，与上传段文件命名一致，排序即顺序。
    """
    out_dir = Path(out_dir)
    ensure_dir(out_dir)
    max_chars = output_cfg.srt_max_chars_per_line if output_cfg else 30
    encoding = output_cfg.srt_encoding if output_cfg else "utf-8"

    # 时间轴边界：offsets[i] = 前 i 段时长之和
    offsets = [0.0]
    for d in durations:
        offsets.append(offsets[-1] + d)

    written: List[Path] = []
    for i, stem in enumerate(stems):
        lo, hi = offsets[i], offsets[i + 1]
        lines: List[str] = []
        idx = 1
        for seg in segments:
            s = float(seg.get("start", 0.0))
            if lo <= s < hi or (i == len(stems) - 1 and s >= lo):
                text = str(seg.get("text", ""))
                if not text.strip():
                    continue
                _emit_srt_entry(lines, idx, s - lo,
                                float(seg.get("end", 0.0)) - lo, text, max_chars)
                idx += 1
        path = out_dir / f"{i:02d}_{stem}.srt"
        path.write_text("\n".join(lines) + "\n", encoding=encoding)
        written.append(path)
    return written
