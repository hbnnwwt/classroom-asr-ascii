"""配置文件加载测试（不强制模型路径存在）。"""
from pathlib import Path
import pytest

from asr.config import load_config, ConfigLoadError


def test_load_default_config(strict_models=False):
    """默认 config.yaml 应能加载通过（strict_models=False 时不验模型）。"""
    cfg = load_config("config.yaml", strict_models=False)
    assert cfg.engine == "funasr"
    assert cfg.language == "zh"
    assert cfg.device == "cpu"
    assert cfg.media.sample_rate == 16000
    assert cfg.media.channels == 1
    assert cfg.media.highpass_hz == 60
    # 模型路径应为绝对路径（被解析过）
    assert Path(cfg.models.funasr.asr).is_absolute()
    assert Path(cfg.models.funasr.vad).is_absolute()
    assert Path(cfg.models.funasr.punc).is_absolute()
    assert Path(cfg.models.funasr.spk).is_absolute()


def test_load_with_gpu_override(strict_models=False):
    """config.gpu.yaml 不存在时应报错。"""
    with pytest.raises(ConfigLoadError):
        load_config("config.yaml", overrides_path="config.gpu.yaml", strict_models=False)


def test_strict_models_passes_when_paths_exist():
    """模型路径存在时 strict_models=True 不抛错（正向场景）。"""
    # 模型已下载到 ./models/iic/，应当通过
    cfg = load_config("config.yaml", strict_models=True)
    assert cfg.models.funasr.asr.exists()


def test_strict_models_missing(tmp_path):
    """模型路径缺失时 strict_models=True 抛 ConfigLoadError。"""
    import yaml
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "models:\n"
        "  funasr:\n"
        "    asr:  /nonexistent/path/asr\n"
        "    vad:  /nonexistent/path/vad\n"
        "    punc: /nonexistent/path/punc\n"
        "    spk:  /nonexistent/path/spk\n"
        "  whisper:\n"
        "    size: small\n"
        "    compute_type: int8\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigLoadError):
        load_config(bad, strict_models=True)


# ---------------------------------------------------------------------------
# P2-8：spk 模型按需校验——只有开启 diarize 才强校验 spk
# 用户故事：Web 上传可按次关 diarize，没下 2GB spk 模型的用户必须能跑。
# ---------------------------------------------------------------------------
def _write_cfg(tmp_path, spk: str, diarize_enabled: bool):
    """写一个 asr/vad/punc 目录真实存在、spk 可控的 config。"""
    for name in ("asr", "vad", "punc"):
        (tmp_path / name).mkdir(exist_ok=True)
    diarize_block = "diarize:\n  enabled: true\n" if diarize_enabled else ""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "models:\n"
        "  funasr:\n"
        f"    asr:  {tmp_path / 'asr'}\n"
        f"    vad:  {tmp_path / 'vad'}\n"
        f"    punc: {tmp_path / 'punc'}\n"
        f"    spk:  {spk}\n"
        "  whisper:\n"
        "    size: small\n"
        "    compute_type: int8\n"
        + diarize_block,
        encoding="utf-8",
    )
    return cfg


def test_strict_spk_missing_diarize_off_passes(tmp_path, caplog):
    """P2-8：strict + diarize 关 + spk 缺失 → 加载成功，只 warning 不阻断。"""
    import logging
    cfg_path = _write_cfg(tmp_path, spk=str(tmp_path / "no_spk"), diarize_enabled=False)
    with caplog.at_level(logging.WARNING, logger="asr.config"):
        cfg = load_config(cfg_path, strict_models=True)  # 不应抛错
    assert cfg.diarize.enabled is False
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "spk" in r.getMessage()]
    assert warnings, "spk 缺失且未开 diarize 时应有一条 warning 日志"


def test_strict_spk_missing_diarize_on_raises(tmp_path):
    """P2-8：strict + diarize 开 + spk 缺失 → 仍抛 ConfigLoadError（行为保留）。"""
    cfg_path = _write_cfg(tmp_path, spk=str(tmp_path / "no_spk"), diarize_enabled=True)
    with pytest.raises(ConfigLoadError, match="funasr.spk"):
        load_config(cfg_path, strict_models=True)


def test_strict_spk_present_diarize_on_passes(tmp_path):
    """P2-8：strict + diarize 开 + spk 存在 → 正常通过。"""
    spk_dir = tmp_path / "spk"
    spk_dir.mkdir()
    cfg_path = _write_cfg(tmp_path, spk=str(spk_dir), diarize_enabled=True)
    cfg = load_config(cfg_path, strict_models=True)
    assert cfg.diarize.enabled is True


def test_no_strict_all_missing_still_passes(tmp_path):
    """P2-8（回归）：非 strict 模式完全不校验，路径全缺也不抛。"""
    cfg_path = _write_cfg(tmp_path, spk=str(tmp_path / "no_spk"), diarize_enabled=True)
    cfg = load_config(cfg_path, strict_models=False)
    assert cfg is not None


# ---------------------------------------------------------------------------
# P0-7：CLI --device/--engine/--language overrides 落到 cfg
# CLI（asr/cli.py run）组装的 overrides 键即 "device"/"engine"/"language"，
# 值为字符串；未传的键在 CLI 侧已被 None 过滤，web 侧可能传 None。
# ---------------------------------------------------------------------------
def test_overrides_device_engine_language_applied():
    """P0-7：overrides 里 device/engine/language 必须落到 cfg。"""
    from asr.pipeline import _apply_overrides
    cfg = load_config("config.yaml", strict_models=False)
    assert (cfg.device, cfg.engine, cfg.language) == ("cpu", "funasr", "zh")  # config.yaml 基线
    _apply_overrides(cfg, {"device": "cuda:0", "engine": "whisper", "language": "en"})
    assert cfg.device == "cuda:0"
    assert cfg.engine == "whisper"
    assert cfg.language == "en"


def test_overrides_none_and_unknown_keys_ignored():
    """P0-7 契约：None（CLI 未传）不覆盖 cfg；未知键静默忽略。"""
    from asr.pipeline import _apply_overrides
    cfg = load_config("config.yaml", strict_models=False)
    _apply_overrides(cfg, {"device": None, "language": None, "nonsense": "x", "quality": "fast"})
    assert cfg.device == "cpu"
    assert cfg.language == "zh"


# ---------------------------------------------------------------------------
# AI 反馈：llm 配置节（缺省全空 = 功能关闭）
# ---------------------------------------------------------------------------
def _write_cfg_llm(tmp_path, extra=""):
    # 与上面 P2-8 的 _write_cfg 同名会互相覆盖（签名不同），故独立命名。
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "models:\n"
        "  funasr:\n"
        "    asr:  m/asr\n    vad:  m/vad\n    punc:  m/punc\n    spk:  m/spk\n"
        "  whisper:\n    size: small\n    compute_type: int8\n" + extra,
        encoding="utf-8",
    )
    return cfg


def test_llm_section_absent_defaults_off(tmp_path):
    """无 llm 节 → 全默认值，功能关闭（llm_enabled 语义的基础）。"""
    cfg = load_config(_write_cfg_llm(tmp_path), strict_models=False)
    assert cfg.llm.base_url == ""
    assert cfg.llm.model == ""
    assert cfg.llm.api_key == ""
    assert cfg.llm.timeout == 300
    assert cfg.llm.max_tokens == 16384


def test_llm_section_parsed(tmp_path):
    """有 llm 节 → 值完整解析。"""
    cfg = load_config(_write_cfg_llm(tmp_path, (
        "llm:\n"
        "  base_url: http://localhost:11434/v1\n"
        "  model: qwen2.5:7b\n"
        "  timeout: 120\n"
    )), strict_models=False)
    assert cfg.llm.base_url == "http://localhost:11434/v1"
    assert cfg.llm.model == "qwen2.5:7b"
    assert cfg.llm.timeout == 120
    assert cfg.llm.api_key == ""   # 本地端点不需要 key
