"""
Whisper 回退引擎 —— **按用户要求禁用**。

本文件仅作为接口桩保留，不实现、不引入 faster-whisper 依赖。
如未来需要启用，可基于 faster-whisper 实现 transcribe()，
并把 requirements.txt 里的相关注释去掉。
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from .base import ASREngine, Segment


class WhisperEngine(ASREngine):
    """禁用占位。实例化即抛错。"""

    name = "whisper"

    def __init__(self, cfg):
        super().__init__(cfg)
        raise RuntimeError(
            "WhisperEngine 已被禁用（按用户要求）。"
            "本项目仅使用 FunASR 引擎；如需启用请编辑 asr/engines/whisper_engine.py"
        )

    def transcribe(self, wav: Path, hotwords: List[str] | None = None) -> List[Segment]:
        raise NotImplementedError("WhisperEngine 已禁用")
