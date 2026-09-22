"""
CLI 入口（P1 最小版 + 后续阶段逐步扩展）。

子命令：
- asr check                       自检：ffmpeg/ffprobe、模型路径、10 秒样本推理
- asr INPUT [-o OUT] [options]    单文件转写（P2 起实现）
- asr batch DIR_OR_LIST --out OUT 批量转写（P6 起实现）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import click
from loguru import logger

from .config import load_config, ConfigLoadError
from .media import probe, NoAudioStreamError
from .utils import setup_logging, which_or_die, run_cmd


# ---------------------------------------------------------------------------
# 共用：解析配置 + 应用 CLI 覆盖
# ---------------------------------------------------------------------------
def _load_cfg(config: str, gpu_config: bool) -> tuple:
    overrides = "config.gpu.yaml" if gpu_config else None
    try:
        return load_config(config, overrides, strict_models=False)
    except ConfigLoadError as e:
        click.echo(f"[CONFIG ERROR] {e}", err=True)
        sys.exit(2)


# ---------------------------------------------------------------------------
# check：自检
# ---------------------------------------------------------------------------
@click.group()
@click.option("--verbose", "-v", is_flag=True, help="详细日志")
def main(verbose: bool):
    """课堂录课本地语音识别 CLI"""
    setup_logging(verbose=verbose)


@main.command()
@click.option("--config", "-c", default="config.yaml", help="配置文件路径")
@click.option("--with-gpu/--no-gpu", default=False, help="同时检查 config.gpu.yaml")
@click.option("--strict/--no-strict", default=False, help="严格模式：模型缺失即返回非零")
def check(config: str, with_gpu: bool, strict: bool):
    """自检：ffmpeg/ffprobe、模型路径、CUDA 可用性。"""
    click.echo("=" * 60)
    click.echo("classroom-asr 自检")
    click.echo("=" * 60)

    # 1. ffmpeg/ffprobe
    try:
        ff = which_or_die("ffmpeg")
        fp = which_or_die("ffprobe")
        v1 = run_cmd([ff, "-version"]).stdout.split("\n")[0]
        v2 = run_cmd([fp, "-version"]).stdout.split("\n")[0]
        click.echo(f"[OK] ffmpeg:  {v1}")
        click.echo(f"[OK] ffprobe: {v2}")
    except RuntimeError as e:
        click.echo(f"[FAIL] {e}")
        sys.exit(1)

    # 2. 配置加载 + 模型路径
    try:
        cfg = _load_cfg(config, with_gpu)
        click.echo(f"[OK] 配置加载: {config}")
        # 模型路径校验
        from .config import _check_model_paths
        try:
            _check_model_paths(cfg)
            click.echo("[OK] 所有模型路径已就位")
        except ConfigLoadError as e:
            click.echo(f"[WARN] 模型路径缺失:\n{e}")
            if strict:
                sys.exit(3)
    except ConfigLoadError as e:
        click.echo(f"[FAIL] {e}")
        sys.exit(2)

    # 3. torch + CUDA
    try:
        import torch
        cuda_avail = torch.cuda.is_available()
        click.echo(f"[OK] torch={torch.__version__}, cuda_available={cuda_avail}")
        if cuda_avail:
            click.echo(f"     GPU: {torch.cuda.get_device_name(0)}")
    except ImportError:
        click.echo("[WARN] torch 未安装（FunASR/Whisper 都需要）")

    # 4. Python 路径
    click.echo(f"[OK] Python: {sys.version.split()[0]}")

    click.echo("=" * 60)
    click.echo("自检完成")


# ---------------------------------------------------------------------------
# run：单文件转写（P2 接入）
# ---------------------------------------------------------------------------
@main.command()
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output-dir", type=click.Path(path_type=Path), default=None)
@click.option("--engine", type=click.Choice(["funasr", "whisper"]), default=None)
@click.option("--device", default=None, help="cpu | cuda:0")
@click.option("--quality", type=click.Choice(["fast", "balanced", "high"]), default=None)
@click.option("--diarize/--no-diarize", default=None)
@click.option("--language", default=None)
@click.option("--glossary", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--formats", default=None, help="逗号分隔，如 txt,srt,json")
@click.option("--audio-track", type=int, default=None)
@click.option("--denoise/--no-denoise", default=None)
@click.option("--keep-wav/--no-keep-wav", default=None)
@click.option("--workers", type=int, default=None)
@click.option("--config", "-c", default="config.yaml")
def run(
    input_path: Path,
    output_dir: Optional[Path],
    engine: Optional[str],
    device: Optional[str],
    quality: Optional[str],
    diarize: Optional[bool],
    language: Optional[str],
    glossary: Optional[Path],
    formats: Optional[str],
    audio_track: Optional[int],
    denoise: Optional[bool],
    keep_wav: Optional[bool],
    workers: Optional[int],
    config: str,
):
    """单文件转写（视频或音频）"""
    cfg = _load_cfg(config, False)

    overrides = {
        "output_dir": str(output_dir) if output_dir else None,
        "engine": engine,
        "device": device,
        "quality": quality,
        "diarize": diarize,
        "language": language,
        "glossary": str(glossary) if glossary else None,
        "formats": formats,
        "audio_track": audio_track,
        "denoise": denoise,
        "keep_wav": keep_wav,
    }
    overrides = {k: v for k, v in overrides.items() if v is not None}

    from .pipeline import run as pipeline_run
    result = pipeline_run(input_path, cfg, overrides)
    click.echo(f"\n完成: {len(result.written)} 个输出文件")
    for fmt, p in result.written.items():
        click.echo(f"  [{fmt}] {p}")
    click.echo(f"用时: {result.elapsed_s}s, RTF={result.rtf}, 段数={result.segments_count}")


# ---------------------------------------------------------------------------
# batch：批量转写（P6 接入）
# ---------------------------------------------------------------------------
@main.command()
@click.argument("inputs", nargs=-1, required=True, type=click.Path(path_type=Path))
@click.option("-o", "--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--workers", type=int, default=None)
@click.option("--engine", type=click.Choice(["funasr", "whisper"]), default=None)
@click.option("--quality", type=click.Choice(["fast", "balanced", "high"]), default=None)
@click.option("--diarize/--no-diarize", default=None)
@click.option("--formats", default=None)
@click.option("--glossary", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--config", "-c", default="config.yaml")
def batch(inputs, output_dir, workers, engine, quality, diarize, formats, glossary, config):
    """批量转写一个或多个目录/文件"""
    from .utils import iter_inputs
    from .batch import batch_run

    cfg = _load_cfg(config, False)
    files = iter_inputs(inputs)
    if not files:
        click.echo("[batch] 没有找到可处理的文件")
        return

    overrides = {
        "output_dir": str(output_dir),
        "engine": engine,
        "quality": quality,
        "diarize": diarize,
        "formats": formats,
        "glossary": str(glossary) if glossary else None,
    }
    overrides = {k: v for k, v in overrides.items() if v is not None}

    w = workers if workers is not None else cfg.num_workers
    result = batch_run(files, cfg, overrides=overrides, workers=w)

    click.echo(f"\n批量完成: 成功 {result.succeeded}/{result.total}, 失败 {result.failed}")
    for f in result.failures:
        click.echo(f"  FAIL {f['file']}: {f['error']}")


if __name__ == "__main__":
    main()
