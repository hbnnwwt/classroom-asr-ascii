"""
媒体处理：ffprobe 探测时长/音轨，ffmpeg 抽音轨、降噪、响度归一。
- 子进程统一走 asr.utils.run_cmd（shell=False，utf-8）。
- 抽音轨命令严格按方案第 5.1 节给的可复制即用串。
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from loguru import logger

from .config import AppConfig
from .utils import which_or_die, run_cmd, ensure_dir


class NoAudioStreamError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    """ffprobe 输出归一化"""
    path: Path
    duration_s: float
    audio_streams: List[dict] = field(default_factory=list)
    video_streams: List[dict] = field(default_factory=list)

    @property
    def has_audio(self) -> bool:
        return len(self.audio_streams) > 0

    def audio_track_index(self, requested: int = 0) -> int:
        """返回安全的音轨索引，越界时回退到 0。"""
        if not self.audio_streams:
            raise NoAudioStreamError(f"视频无音轨: {self.path}")
        if requested < len(self.audio_streams):
            return requested
        return 0


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------
def probe(path: Path) -> MediaInfo:
    """调 ffprobe 拿时长 + 流列表。"""
    ffprobe = which_or_die("ffprobe")
    cmd = [
        ffprobe, "-v", "error",
        "-show_format", "-show_streams",
        "-of", "json",
        str(path),
    ]
    proc = run_cmd(cmd, timeout=60, check=True)
    data = json.loads(proc.stdout)
    duration_s = float(data.get("format", {}).get("duration", 0.0) or 0.0)
    audios, videos = [], []
    for s in data.get("streams", []):
        if s.get("codec_type") == "audio":
            audios.append(s)
        elif s.get("codec_type") == "video":
            videos.append(s)
    return MediaInfo(path=path, duration_s=duration_s, audio_streams=audios, video_streams=videos)


# ---------------------------------------------------------------------------
# 抽音轨（核心：方案给的命令直接套）
# ---------------------------------------------------------------------------
def _build_extract_cmd(
    ffmpeg: str, src: Path, dst: Path, cfg: AppConfig, audio_track: int
) -> List[str]:
    """
    ffmpeg -y -i $IN -vn -ac 1 -ar 16000
           -af "highpass=f=60,loudnorm=I=-16:TP=-1.5:LRA=11"
           -c:a pcm_s16le $OUT

    差异点：
    - 方案默认抽音轨 0；多音轨时按 --audio-track 切换，用 -map 0:a:N。
    - highpass_hz=0 时跳过 highpass filter。
    - loudnorm=false 时跳过 loudnorm filter。
    """
    af_filters = []
    if cfg.media.highpass_hz > 0:
        af_filters.append(f"highpass=f={cfg.media.highpass_hz}")
    if cfg.media.loudnorm:
        af_filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    af = ",".join(af_filters) if af_filters else "anull"

    cmd = [
        ffmpeg, "-y", "-nostdin",
        "-threads", "0",   # 用全部 CPU 核（默认 ffmpeg 只用 1 核）
        "-i", str(src),
        "-map", f"0:a:{audio_track}",
        "-vn",
        "-ac", str(cfg.media.channels),
        "-ar", str(cfg.media.sample_rate),
        "-af", af,
        "-c:a", "pcm_s16le",
        str(dst),
    ]
    return cmd


def extract_audio(video: Path, wav_out: Path, cfg: AppConfig, *, audio_track: int | None = None) -> Path:
    """
    抽音轨到 wav。返回输出路径。
    - 抽不到音轨抛 NoAudioStreamError。
    - 自动加 .wav 后缀（如果用户给的是别的）。
    """
    info = probe(video)
    track = audio_track if audio_track is not None else cfg.media.audio_track
    safe_track = info.audio_track_index(track)

    ffmpeg = which_or_die("ffmpeg")
    wav_out = Path(wav_out)
    if wav_out.suffix.lower() != ".wav":
        wav_out = wav_out.with_suffix(".wav")
    ensure_dir(wav_out.parent)

    cmd = _build_extract_cmd(ffmpeg, video, wav_out, cfg, safe_track)
    logger.info(f"抽音轨: {' '.join(cmd)}")
    proc = run_cmd(cmd, timeout=3600)
    if proc.returncode != 0 or not wav_out.exists():
        raise RuntimeError(
            f"ffmpeg 抽音轨失败 (rc={proc.returncode})\n"
            f"stderr:\n{proc.stderr[-2000:]}"
        )
    return wav_out


# ---------------------------------------------------------------------------
# 可选降噪
# ---------------------------------------------------------------------------
def denoise(wav_in: Path, wav_out: Path, cfg: AppConfig) -> Path:
    """
    用 ffmpeg afftdn 简单降噪（轻量级，不引入额外依赖）。
    噪声大的录课可开 `denoise: true`。
    """
    ffmpeg = which_or_die("ffmpeg")
    wav_out = Path(wav_out)
    if wav_out.suffix.lower() != ".wav":
        wav_out = wav_out.with_suffix(".wav")
    ensure_dir(wav_out.parent)
    cmd = [
        ffmpeg, "-y", "-nostdin",
        "-i", str(wav_in),
        "-af", "afftdn=nf=-25",
        "-c:a", "pcm_s16le",
        str(wav_out),
    ]
    logger.info(f"降噪: {wav_in} -> {wav_out}")
    proc = run_cmd(cmd, timeout=3600)
    if proc.returncode != 0 or not wav_out.exists():
        raise RuntimeError(f"降噪失败: {proc.stderr[-1000:]}")
    return wav_out


# ---------------------------------------------------------------------------
# 时长辅助
# ---------------------------------------------------------------------------
def get_wav_duration_s(wav: Path) -> float:
    """从 wav 头读时长（不重新 ffprobe，靠 soundfile）。"""
    try:
        import soundfile as sf
        info = sf.info(str(wav))
        return float(info.frames) / float(info.samplerate)
    except Exception:
        # fallback: ffprobe
        info = probe(wav)
        return info.duration_s


# ---------------------------------------------------------------------------
# 临时目录辅助（pipeline 调用）
# ---------------------------------------------------------------------------
def make_tmpdir(prefix: str = "asr-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def safe_rmtree(p: Path) -> None:
    if p.exists() and p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
