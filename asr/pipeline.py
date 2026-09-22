"""
主流程编排（方案 5.8 节）。

输入：视频/音频文件路径 + 配置 + CLI overrides
输出：写出 SRT/VTT/MD/TXT/JSON 五件套 + replace.log
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

from . import srt as writers
from .config import AppConfig
from .engines.base import ASREngine
from .engines.funasr_engine import FunASREngine
from .media import (
    probe, extract_audio, denoise, make_tmpdir, safe_rmtree, get_wav_duration_s,
    NoAudioStreamError,
)
from .postprocess import apply as apply_postprocess
from .utils import ensure_dir


@dataclass
class Result:
    input_path: Path
    output_dir: Path
    duration_s: float
    elapsed_s: float
    rtf: float
    written: dict[str, Path]
    replace_log: list[str]
    segments_count: int


# P1-1 引擎缓存：key = (engine, quality, asr 模型, device, diarize)
_ENGINE_CACHE: dict[tuple, ASREngine] = {}


def build_engine(cfg: AppConfig, use_cache: bool = True) -> ASREngine:
    """
    按 cfg.engine 创建引擎实例。当前仅支持 funasr。

    P1-1：进程内按 (engine, quality, asr 模型, device, diarize) 缓存引擎，
    批量/连续上传不再每个文件重载 2GB 模型。FunASREngine 惰性加载
    （构造函数不碰模型），缓存未加载实例无内存代价；缓存实例的 close()
    统一由 close_cached_engines() 负责（web lifespan 退出时调用），run()
    不 close。多线程并发 build 同 key 的良性竞争（后写覆盖）可接受，不加锁。
    """
    key = (
        cfg.engine,
        getattr(cfg, "_quality_override", "balanced"),
        str(cfg.asr.paraformer_model or cfg.models.funasr.asr),
        cfg.device,
        cfg.diarize.enabled,
    )
    if use_cache and key in _ENGINE_CACHE:
        return _ENGINE_CACHE[key]
    if cfg.engine != "funasr":
        raise RuntimeError(f"未知引擎: {cfg.engine}（当前仅支持 funasr）")
    eng = FunASREngine(cfg)
    if use_cache:
        _ENGINE_CACHE[key] = eng
    return eng


def close_cached_engines() -> None:
    """释放并清空引擎缓存（进程退出前调用；web lifespan 已挂）。"""
    for eng in _ENGINE_CACHE.values():
        eng.close()
    _ENGINE_CACHE.clear()


def _apply_overrides(cfg: AppConfig, overrides: dict) -> None:
    """
    把 CLI 覆盖项中直接影响 AppConfig 顶层的三个键落到 cfg：device / engine / language。

    - None（CLI 未传）跳过，不用 None 覆盖 cfg；
    - 未知键忽略（既有契约，静默）；
    - 必须在 build_engine 之前调用，engine 覆盖才能生效。
    """
    for key in ("device", "engine", "language"):
        value = overrides.get(key)
        if value is not None:
            setattr(cfg, key, value)


def run(
    input_path: Path,
    cfg: AppConfig,
    overrides: dict | None = None,
    on_progress: Optional["Callable[[str, float, str], None]"] = None,
) -> Result:
    """
    端到端跑一个输入文件。
    overrides 可覆盖 CLI 临时项：device, engine, language, quality, diarize, glossary, formats, audio_track, denoise, keep_wav
    on_progress(stage, ratio, msg) 可选回调：用于 Web 进度条。
      stage: "probe" | "extract" | "denoise" | "asr" | "postprocess" | "write" | "done"
      ratio: 0.0 - 1.0
      msg:   人类可读的状态描述
    """
    overrides = overrides or {}
    _apply_overrides(cfg, overrides)  # device/engine/language 落 cfg；须在 build_engine 前
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"输入文件不存在: {input_path}")

    t_total = time.time()

    def _progress(stage: str, ratio: float, msg: str) -> None:
        if on_progress:
            try:
                on_progress(stage, ratio, msg)
            except Exception:
                pass  # 回调失败不影响 pipeline

    # 1. 探测
    logger.info(f"[pipeline] probe: {input_path}")
    _progress("probe", 0.05, "探测音视频")
    info = probe(input_path)

    # 2. 音频准备（视频抽轨 / 非 wav 音频转码 / wav 直拷）
    tmpdir = make_tmpdir(prefix="asr-")
    wav: Optional[Path] = None
    try:
        suffix = input_path.suffix.lower()
        if suffix in {".mp4", ".mkv", ".mov", ".flv", ".ts", ".avi", ".m4v"}:
            audio_track = overrides.get("audio_track", cfg.media.audio_track)
            _progress("extract", 0.10, "提取音轨")
            wav = extract_audio(input_path, tmpdir / "audio.wav", cfg, audio_track=audio_track)
        elif suffix == ".wav":
            # wav：复制到临时目录；采样率/通道不符时再转码
            wav = tmpdir / "audio.wav"
            import shutil
            shutil.copy2(input_path, wav)
            # 若采样率/通道不对，转码
            try:
                import soundfile as sf
                meta = sf.info(str(wav))
                if meta.samplerate != cfg.media.sample_rate or meta.channels != cfg.media.channels:
                    wav = extract_audio(input_path, wav, cfg, audio_track=0)
            except Exception:
                pass
        else:
            # mp3/m4a 等音频：ffmpeg 直接转码统一 16k mono（旧 copy2+sf.info 猜测路径不可靠）
            _progress("extract", 0.10, "转码音频")
            wav = extract_audio(input_path, tmpdir / "audio.wav", cfg, audio_track=0)

        # 3. 可选降噪
        if overrides.get("denoise", cfg.media.denoise):
            _progress("denoise", 0.15, "降噪")
            wav = denoise(wav, tmpdir / "audio_denoised.wav", cfg)

        # 4. 时长
        duration_s = get_wav_duration_s(wav)
        logger.info(f"[pipeline] 音频时长: {duration_s:.1f}s")

        # 5. 引擎加载 + 转写
        _progress("asr", 0.20, "加载模型并识别")
        cfg._quality_override = overrides.get("quality", "balanced")  # type: ignore[attr-defined]
        cfg.diarize.enabled = overrides.get("diarize", cfg.diarize.enabled)  # type: ignore[attr-defined]
        engine = build_engine(cfg)
        hotwords: list[str] = []
        glossary_path = overrides.get("glossary", None)
        if glossary_path:
            from .postprocess import load_glossary
            hotwords = [p for p, _ in load_glossary(Path(glossary_path))]

        t_asr = time.time()
        # ASR 进度映射：样本级 ratio (0~1) → 总进度 0.20 ~ 0.90（留给 postprocess/write）
        def _asr_progress(ratio: float, msg: str) -> None:
            total_ratio = 0.20 + 0.70 * ratio
            _progress("asr", total_ratio, msg)

        segs = engine.transcribe(wav, hotwords=hotwords, on_progress=_asr_progress)
        elapsed_asr = time.time() - t_asr
        logger.info(f"[pipeline] ASR 完成: {len(segs)} 段, {elapsed_asr:.1f}s")

        # ASR 完成 → 0.90（ASR 是大头）
        _progress("postprocess", 0.92, "后处理中")

        # 6. 后处理
        glossary = Path(overrides.get("glossary", cfg.postprocess.glossary)) if overrides.get("glossary") or cfg.postprocess.glossary else None
        if overrides.get("glossary"):
            cfg.postprocess.glossary = glossary
        segs, replace_log = apply_postprocess(segs, cfg, duration_s=duration_s)

        # 6.5 说话人分离（角色启发式）：仅在 --diarize 时执行
        role_map: dict[str, str] = {}
        if cfg.diarize.enabled and any(s.spk for s in segs):
            from .diarize import diarize as diarize_apply
            segs, role_map = diarize_apply(segs, max_speakers=cfg.diarize.max_speakers)

        # 7. 输出
        _progress("write", 0.96, "写输出文件")
        output_dir = Path(overrides.get("output_dir", cfg.output.dir))
        ensure_dir(output_dir)
        stem = input_path.stem
        base = output_dir / stem

        formats = overrides.get("formats")
        if formats:
            cfg.output.formats = [f.strip() for f in formats.split(",")]

        meta = {
            "file": str(input_path),
            "duration": round(duration_s, 2),
            "engine": cfg.engine,
            "model": str(cfg.asr.paraformer_model if cfg._quality_override != "fast" else cfg.models.funasr.asr),  # type: ignore[attr-defined]
            "language": cfg.language,
            "elapsed_s": round(elapsed_asr, 2),
            "rtf": round(elapsed_asr / max(duration_s, 1.0), 4),
        }

        written = writers.write_all(segs, base, cfg.output, meta=meta, replace_log=replace_log)

        # 8. replace.log（始终写出，便于人工核查）
        if replace_log:
            log_path = base.with_suffix(".replace.log")
            log_path.write_text("\n".join(replace_log) + "\n", encoding="utf-8")
            written["replace_log"] = log_path

        # 9. keep_wav 时保留中间 wav
        keep_wav = overrides.get("keep_wav", cfg.media.keep_wav)
        if keep_wav and wav and wav.exists():
            wav_dst = output_dir / "_wav" / wav.name
            ensure_dir(wav_dst.parent)
            import shutil
            shutil.copy2(wav, wav_dst)

    finally:
        if not (overrides.get("keep_wav", cfg.media.keep_wav)):
            safe_rmtree(tmpdir)

    elapsed_total = time.time() - t_total
    rtf = elapsed_total / max(info.duration_s, 1.0)
    _progress("done", 1.0, "完成")

    return Result(
        input_path=input_path,
        output_dir=Path(overrides.get("output_dir", cfg.output.dir)),
        duration_s=info.duration_s,
        elapsed_s=round(elapsed_total, 2),
        rtf=round(rtf, 4),
        written=written,
        replace_log=replace_log,
        segments_count=len(segs),
    )
