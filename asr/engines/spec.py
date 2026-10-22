"""唯一的模型选择规则；加载依赖与每次推理选项分开。"""
from dataclasses import dataclass
from pathlib import Path


def selected_asr_path(cfg, quality: str = "balanced") -> Path:
    return Path(cfg.models.funasr.asr if quality == "fast" or not cfg.asr.paraformer_model
                else cfg.asr.paraformer_model).resolve()


@dataclass(frozen=True)
class EngineLoadSpec:
    asr: Path
    vad: Path
    punc: Path
    spk: Path | None
    device: str
    num_workers: int

    @classmethod
    def from_config(cls, cfg, quality: str = "balanced") -> "EngineLoadSpec":
        return cls(selected_asr_path(cfg, quality), Path(cfg.models.funasr.vad).resolve(),
                   Path(cfg.models.funasr.punc).resolve(),
                   Path(cfg.models.funasr.spk).resolve() if cfg.diarize.enabled else None,
                   cfg.device, max(1, cfg.num_workers))
