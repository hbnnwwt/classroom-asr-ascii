"""
配置加载与校验。
- 用 pydantic v2 定义强类型配置。
- 路径字段在加载时统一解析为绝对路径，并**校验模型目录存在**（缺失即抛错）。
- 支持 config.gpu.yaml 作为覆盖层合并。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


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
    # 断句合并（merge_short_sentences/min/max_sentence_chars）已删除：
    # 跨静音合并制造 29.6s 钉屏段 + 字数插值销毁引擎逐字真时间锚。
    # 旧配置文件残留这些键会被 pydantic 忽略，不影响加载。
    glossary: Path = Path("glossary/default.txt")


class OutputConfig(BaseModel):
    dir: Path = Path("./out")
    formats: list[Literal["txt", "md", "srt", "vtt", "json"]] = Field(
        default_factory=lambda: ["txt", "md", "srt", "vtt", "json"]
    )
    srt_max_chars_per_line: int = 20
    srt_max_duration_s: int = 8
    srt_encoding: str = "utf-8-sig"


class LLMConfig(BaseModel):
    """AI 反馈环节（可选）。全空 = 功能整体隐藏，不影响任何现有行为。"""
    base_url: str = ""    # OpenAI 兼容端点；Ollama 用 http://localhost:11434/v1
    api_key: str = ""     # 本地模型留空
    model: str = ""
    timeout: int = 300
    max_tokens: int = 16384


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
    llm: LLMConfig = Field(default_factory=LLMConfig)

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
            # 同目录有模板（新克隆）→ 报错顺手指路；没有就不多嘴
            if p.with_name("config.example.yaml").exists():
                raise ConfigLoadError(
                    f"配置文件不存在: {p}"
                    f"（首次使用请先复制一份: config.example.yaml -> {p.name}）")
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
    """启动时校验：缺失就报明确错。spk（2GB）仅在开启 diarize 时才用得上，按需校验。"""
    required = [
        ("funasr.asr", cfg.models.funasr.asr),
        ("funasr.vad", cfg.models.funasr.vad),
        ("funasr.punc", cfg.models.funasr.punc),
    ]
    if cfg.diarize.enabled:
        required.append(("funasr.spk", cfg.models.funasr.spk))
    elif not cfg.models.funasr.spk.exists():
        logger.warning(
            "funasr.spk 模型缺失（%s）。当前未开启 diarize，不阻断；"
            "如需说话人分离，请先运行 `python scripts/download_models.py` 下载。",
            cfg.models.funasr.spk,
        )
    missing = [f"  - {name}: {p}" for name, p in required if not p.exists()]
    if missing:
        raise ConfigLoadError(
            "模型路径缺失，请先运行 `python scripts/download_models.py` 下载：\n"
            + "\n".join(missing)
        )


# ---------------------------------------------------------------------------
# 设置页写回：llm 节文本手术
# ---------------------------------------------------------------------------
def _yaml_quote(s: str) -> str:
    """双引号标量（转义 \\ 与 "）。井号/空格等特殊字符无需再操心。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_llm_section(text: str, values: dict) -> str:
    """把 llm 节写进 config.yaml 文本，返回新文本。

    文本手术而非 YAML 重序列化：config.yaml 是用户手写维护的、带成片注释
    文档的文件，round-trip 会把注释全部毁掉。三种形态：
    活节原位替换 / 出厂注释模板激活 / 文末追加。其余内容一字不动。
    """
    nl = "\r\n" if "\r\n" in text else "\n"
    section = nl.join([
        "llm:",
        f"  base_url: {_yaml_quote(values['base_url'])}",
        f"  api_key: {_yaml_quote(values['api_key'])}",
        f"  model: {_yaml_quote(values['model'])}",
        f"  timeout: {int(values['timeout'])}",
        f"  max_tokens: {int(values['max_tokens'])}",
    ])
    lines = text.split("\n")

    start, mode = None, None
    for i, ln in enumerate(lines):
        if re.match(r"^llm:\s*$", ln):
            start, mode = i, "live"
            break
        if re.match(r"^#\s*llm:\s*$", ln):
            start, mode = i, "comment"
            break

    if start is None:  # 都没有 → 文末追加
        if not text.endswith("\n"):
            text += nl
        return text + section + nl

    # 块尾定位：活节吃到下一个顶格键（中间空行一并归块，宁可少一空行
    # 也不留孤立的缩进残行）；注释节吃到连续注释行结束。
    end = start + 1
    if mode == "live":
        while end < len(lines) and not re.match(r"^\S", lines[end]):
            end += 1
    else:
        while end < len(lines) and lines[end].lstrip().startswith("#"):
            end += 1

    lines[start:end] = section.split("\n")
    return "\n".join(lines)
