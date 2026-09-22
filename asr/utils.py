"""
通用工具：日志、子进程、分片、文件遍历、时间单位探测与归一化。

设计原则：
- 时间统一为秒(float)。模型可能返回毫秒/秒/采样点，这里提供探测与归一化。
- 子进程统一走 subprocess.run([...], shell=False)，编码显式 utf-8。
- 所有路径用 pathlib.Path。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Sequence

from loguru import logger

# ---------------------------------------------------------------------------
# 日志：loguru 单实例
# ---------------------------------------------------------------------------
_LOG_CONFIGURED = False


def setup_logging(verbose: bool = False) -> None:
    """全局日志初始化（单次）。"""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <level>{message}</level>",
        enqueue=True,
    )
    _LOG_CONFIGURED = True


# ---------------------------------------------------------------------------
# 子进程：禁用 shell=True，强制 utf-8 输出
# ---------------------------------------------------------------------------
def run_cmd(
    cmd: Sequence[str],
    *,
    timeout: int | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """
    执行命令并返回 CompletedProcess。**禁止** shell=True。
    - 在 Windows 上若命令以 ffmpeg/ffprobe 开头，自动加 creationflags 防止弹窗。
    - 编码 utf-8，errors=replace，避免 GBK 解码炸裂。
    """
    # Windows 上隐藏子进程控制台窗口
    kwargs: dict = {
        "shell": False,
        "encoding": "utf-8",
        "errors": "replace",
        "capture_output": True,
        "text": True,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(list(cmd), **kwargs)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed (rc={proc.returncode}): {' '.join(cmd)}\nstderr:\n{proc.stderr}"
        )
    return proc


def which_or_die(program: str) -> str:
    """查找可执行文件，找不到直接抛错（带明确指引）。"""
    found = shutil.which(program)
    if not found:
        raise RuntimeError(
            f"未找到可执行文件 {program!r}。请安装并加入 PATH。\n"
            f"Windows: https://www.gyan.dev/ffmpeg/builds/\n"
            f"macOS: brew install ffmpeg\n"
            f"Linux: apt install ffmpeg"
        )
    return found


# ---------------------------------------------------------------------------
# 文件遍历 / 临时目录
# ---------------------------------------------------------------------------
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".flv", ".ts", ".avi", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def iter_inputs(paths: Iterable[Path]) -> List[Path]:
    """
    接受单文件或目录路径，返回所有支持的视频/音频文件（按文件名排序）。
    - 目录时只扫描第一层（非递归）。
    """
    out: List[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_file():
            if p.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS:
                out.append(p)
            else:
                logger.warning(f"跳过不支持的文件格式: {p}")
        elif p.is_dir():
            for child in sorted(p.iterdir()):
                if child.is_file() and child.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS:
                    out.append(child)
        else:
            logger.warning(f"路径不存在: {p}")
    return out


# ---------------------------------------------------------------------------
# 时间单位探测与归一化（关键：不同 FunASR 版本返回秒/毫秒不一致）
# ---------------------------------------------------------------------------
def normalize_time_unit(value: float | int, duration_s: float | None = None) -> float:
    """
    把任意模型返回的时间值归一化为**秒（float）**。

    判定规则（按文档第 5.2 节）：
    - 若 duration_s 已知且 value > duration_s * 1.2，视为毫秒 → /1000。
    - 若 value > 1e6（远超任何视频时长），几乎可以肯定是毫秒 → /1000。
    - 其它情况按"已是秒"处理。
    """
    v = float(value)
    if v < 0:
        return 0.0
    # 绝对大值兜底：超过一百万秒（约 11.5 天）只能是毫秒
    if v > 1_000_000:
        return v / 1000.0
    # 相对判定：已知时长则按比例
    if duration_s is not None and duration_s > 0 and v > duration_s * 1.2:
        return v / 1000.0
    return v


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def ensure_dir(p: Path) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p
