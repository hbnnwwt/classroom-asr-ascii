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
