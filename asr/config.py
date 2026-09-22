"""
配置加载与校验。
- 用 pydantic v2 定义强类型配置。
- 路径字段在加载时统一解析为绝对路径，并**校验模型目录存在**（缺失即抛错）。
- 支持 config.gpu.yaml 作为覆盖层合并。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# 嵌套模型
# ---------------------------------------------------------------------------
class MediaConfig(BaseModel):
    sample_rate: int = 16000
    channels: int = 1
    loudnorm: bool = False  # 默认关闭：loudnorm 要跑两遍（分析+应用），1 小时课多花 30~60s
    denoise: bool = False
    highpass_hz: int = 60
    keep_wav: bool = False
    audio_track: int = 0


class FunASRModels(BaseModel):
    asr: Path
    vad: Path
    punc: Path
    spk: Path


class WhisperModels(BaseModel):
    size: str = "small"
    compute_type: str = "int8"


class ModelsConfig(BaseModel):
    funasr: FunASRModels
    whisper: WhisperModels


class ASRConfig(BaseModel):
    batch_size_s: int = 300
    vad_max_segment_ms: int = 60000
    use_itn: bool = True
    hotword_enabled: bool = True
    paraformer_model: Path | None = None


class DiarizeConfig(BaseModel):
    enabled: bool = False
    max_speakers: int = 4
    teacher_rule: Literal["majority"] = "majority"


class PostprocessConfig(BaseModel):
    itn: bool = True
    merge_short_sentences: bool = True
    min_sentence_chars: int = 6
    max_sentence_chars: int = 40
    glossary: Path = Path("glossary/default.txt")


class OutputConfig(BaseModel):
    dir: Path = Path("./out")
    formats: list[Literal["txt", "md", "srt", "vtt", "json"]] = Field(
        default_factory=lambda: ["txt", "md", "srt", "vtt", "json"]
    )
    srt_max_chars_per_line: int = 20
    srt_max_duration_s: int = 8
    srt_encoding: str = "utf-8-sig"


class AppConfig(BaseModel):
    """根配置"""
    device: str = "cpu"
    engine: Literal["funasr", "whisper"] = "funasr"
    language: str = "zh"
    num_workers: int = 4

    media: MediaConfig = Field(default_factory=MediaConfig)
    models: ModelsConfig
    asr: ASRConfig = Field(default_factory=ASRConfig)
    diarize: DiarizeConfig = Field(default_factory=DiarizeConfig)
    postprocess: PostprocessConfig = Field(default_factory=PostprocessConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    # ------------------------------------------------------------------
    # 路径解析：以仓库根目录（config.yaml 所在目录）为基准
    # ------------------------------------------------------------------
    @field_validator("models", mode="before")
    @classmethod
    def _resolve_paths(cls, v):
        """把 models 下的相对路径原样保留为字符串，事后由 resolve_paths() 统一解析。"""
        return v


# ---------------------------------------------------------------------------
# 加载入口
# ---------------------------------------------------------------------------
class ConfigLoadError(RuntimeError):
    pass


def _resolve_path(base: Path, p: str | Path) -> Path:
    p = Path(p)
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def load_config(
    config_path: str | Path = "config.yaml",
    overrides_path: str | Path | None = None,
    *,
    strict_models: bool = True,
) -> AppConfig:
    """
    加载 config.yaml，可选再叠加 config.gpu.yaml 覆盖。

    strict_models=True 时启动即校验模型路径存在（缺失抛 ConfigLoadError）。
    """
    base = Path(config_path).parent.resolve()

    def _read(p: Path) -> dict:
        if not p.exists():
            raise ConfigLoadError(f"配置文件不存在: {p}")
        with p.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    data = _read(Path(config_path))
    if overrides_path:
        ov = _read(Path(overrides_path))
        # 浅合并：顶层键覆盖
        data.update(ov)

    # 路径字段解析
    if "models" in data:
        m = data["models"]
        if "funasr" in m:
            for k, v in m["funasr"].items():
                m["funasr"][k] = str(_resolve_path(base, v))
        if "paraformer_model" in data.get("asr", {}):
            data["asr"]["paraformer_model"] = str(
                _resolve_path(base, data["asr"]["paraformer_model"])
            )
    if "postprocess" in data and "glossary" in data["postprocess"]:
        data["postprocess"]["glossary"] = str(
            _resolve_path(base, data["postprocess"]["glossary"])
        )
    if "output" in data and "dir" in data["output"]:
        data["output"]["dir"] = str(_resolve_path(base, data["output"]["dir"]))

    try:
        cfg = AppConfig(**data)
    except Exception as e:
        raise ConfigLoadError(f"配置解析失败: {e}") from e

    if strict_models:
        _check_model_paths(cfg)
    return cfg


def _check_model_paths(cfg: AppConfig) -> None:
    """启动时校验：缺失就报明确错。"""
    missing = []
    for name, p in [
        ("funasr.asr", cfg.models.funasr.asr),
        ("funasr.vad", cfg.models.funasr.vad),
        ("funasr.punc", cfg.models.funasr.punc),
        ("funasr.spk", cfg.models.funasr.spk),
    ]:
        if not p.exists():
            missing.append(f"  - {name}: {p}")
    if missing:
        raise ConfigLoadError(
            "模型路径缺失，请先运行 `python scripts/download_models.py` 下载：\n"
            + "\n".join(missing)
        )
