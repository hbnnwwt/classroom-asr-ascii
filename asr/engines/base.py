"""
ASR 引擎抽象基类 + Segment dataclass。

设计原则（方案 5.2 节）：
- 引擎内部统一把模型返回的时间换算成秒(float)。
- 时间单位探测走 asr.utils.normalize_time_unit。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional


@dataclass
class Segment:
    """一条识别结果（带时间戳，可选说话人与置信度）"""
    start: float           # 秒（float）
    end: float             # 秒
    text: str              # 已加标点
    spk: Optional[str] = None    # "spk0"
    conf: Optional[float] = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class TranscriptionOptions:
    language: str = "zh"
    use_itn: bool = True
    hotword_enabled: bool = True
    batch_size_s: int = 300
    speaker_output: bool = False

    @classmethod
    def from_config(cls, cfg) -> "TranscriptionOptions":
        return cls(cfg.language, cfg.asr.use_itn, cfg.asr.hotword_enabled,
                   cfg.asr.batch_size_s, cfg.diarize.enabled)


class ASREngine(ABC):
    """ASR 引擎统一接口"""

    name: str = "base"

    def __init__(self, cfg):
        self.cfg = cfg

    @abstractmethod
    def transcribe(self, wav: Path, hotwords: List[str] | None = None, *,
                   options: TranscriptionOptions | None = None,
                   on_progress: Callable[[float, str], None] | None = None,
                   on_segments: Callable[[List[Segment]], None] | None = None) -> List[Segment]:
        """
        转写一个 wav 文件为带时间戳的段列表。
        - wav: 16kHz 单声道 PCM WAV
        - hotwords: 热词列表（部分引擎不支持，由调用方处理降级）
        """
        raise NotImplementedError

    def close(self) -> None:
        """释放模型资源（子类按需重写）"""
        pass
