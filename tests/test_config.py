"""配置文件加载测试（不强制模型路径存在）。"""
from pathlib import Path
import pytest

from asr.config import load_config, ConfigLoadError


def test_load_default_config(strict_models=False):
    """出厂模板 config.example.yaml 应能加载通过（strict_models=False 时不验模型）。"""
    cfg = load_config("config.example.yaml", strict_models=False)
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


def test_missing_config_error_hints_example(tmp_path):
    """config.yaml 不存在但同目录有模板 → 报错信息指路 copy（新克隆引导）。"""
    (tmp_path / "config.example.yaml").write_text("device: cpu\n", encoding="utf-8")
    with pytest.raises(ConfigLoadError, match="config.example.yaml"):
        load_config(tmp_path / "config.yaml", strict_models=False)


def test_missing_config_no_example_no_hint(tmp_path):
    """同目录没有模板 → 报错就是纯"不存在"，不多嘴（回归保护）。"""
    with pytest.raises(ConfigLoadError) as ei:
        load_config(tmp_path / "config.yaml", strict_models=False)
    assert "config.example.yaml" not in str(ei.value)


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
    cfg = load_config("config.example.yaml", strict_models=False)
    assert (cfg.device, cfg.engine, cfg.language) == ("cpu", "funasr", "zh")  # 出厂模板基线
    _apply_overrides(cfg, {"device": "cuda:0", "engine": "whisper", "language": "en"})
    assert cfg.device == "cuda:0"
    assert cfg.engine == "whisper"
    assert cfg.language == "en"


def test_overrides_none_and_unknown_keys_ignored():
    """P0-7 契约：None（CLI 未传）不覆盖 cfg；未知键静默忽略。"""
    from asr.pipeline import _apply_overrides
    cfg = load_config("config.example.yaml", strict_models=False)
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


# ---------------------------------------------------------------------------
# 设置页写回：write_llm_section 文本手术——保住注释与其余配置，
# 绝不走 YAML 重序列化（会把整个文件的注释文档毁掉）。
# ---------------------------------------------------------------------------
import yaml

from asr.config import write_llm_section

_VALS = {"base_url": "https://open.bigmodel.cn/api/paas/v4",
         "api_key": "sk-abc", "model": "glm-4-flash",
         "timeout": 120, "max_tokens": 8192}


def _roundtrip_llm(text: str) -> dict:
    """手术后的文本必须仍能被 yaml 正常解析，取出 llm 节。"""
    data = yaml.safe_load(text)
    assert isinstance(data, dict), f"手术结果不是合法 YAML: {text!r}"
    return data.get("llm") or {}


def test_write_llm_appends_when_absent():
    """无 llm 节 → 文末追加；原内容一字不动。"""
    before = "device: cpu\n\nmedia:\n  sample_rate: 16000\n"
    after = write_llm_section(before, _VALS)
    assert after.startswith(before)
    assert _roundtrip_llm(after) == _VALS


def test_write_llm_replaces_commented_template():
    """出厂注释模板 → 活节替换掉 # llm: 起的注释块，上方说明注释保留。"""
    before = (
        "# 常规注释，不许动\n"
        "\n"
        "# AI 反馈（教学点评 + 逐字稿优化）。整节缺失 = 功能隐藏。\n"
        "# 云 API 与本地 Ollama 都走 OpenAI 兼容端点，一套代码：\n"
        "# llm:\n"
        "#   base_url: https://example.com/v1\n"
        "#   api_key: 你的KEY\n"
        "#   model: glm-4-flash\n"
    )
    after = write_llm_section(before, _VALS)
    # 说明注释保留、注释值行消失、活节出现
    assert "# AI 反馈（教学点评 + 逐字稿优化）。整节缺失 = 功能隐藏。" in after
    assert "#   base_url:" not in after
    assert "\nllm:\n" in after
    assert "# 常规注释，不许动\n" in after
    assert _roundtrip_llm(after) == _VALS


def test_write_llm_replaces_live_section_in_middle():
    """活 llm 节夹在其他节中间 → 只换 llm 块，后续节原样。"""
    before = (
        "device: cpu\n"
        "llm:\n"
        "  base_url: http://old/v1\n"
        "  model: old-model\n"
        "  timeout: 60\n"
        "output:\n"
        "  dir: ./out\n"
    )
    after = write_llm_section(before, _VALS)
    assert "old-model" not in after
    assert "http://old/v1" not in after
    assert "output:\n  dir: ./out\n" in after
    assert _roundtrip_llm(after) == _VALS


def test_write_llm_preserves_unrelated_lines_byte_for_byte():
    """llm 块以外的每一行逐字节不变（含行内注释与空行）。"""
    before = (
        "# 顶部说明\n"
        "device: cpu        # 注释\n"
        "\n"
        "llm:\n"
        "  base_url: http://old/v1\n"
        "  model: old\n"
        "\n"
        "output:\n"
        "  formats: [txt, md]\n"
    )
    after = write_llm_section(before, _VALS)
    kept = [ln for ln in after.splitlines() if ln not in
            ("llm:", "  base_url: http://old/v1", "  model: old")]
    # 每一非 llm 原行都原样存在（顺序不变由 startswith 分块验证足够）
    for line in ("# 顶部说明", "device: cpu        # 注释", "",
                 "output:", "  formats: [txt, md]"):
        assert line in after.splitlines(), f"丢失原行: {line!r}"


def test_write_llm_empty_values_disable():
    """base_url/model 清空保存 = 功能关闭（写入空串，加载后 enabled=False）。"""
    from asr.llm import llm_enabled
    vals = {"base_url": "", "api_key": "", "model": "",
            "timeout": 300, "max_tokens": 16384}
    after = write_llm_section("device: cpu\n", vals)
    data = yaml.safe_load(after)
    assert data["llm"]["base_url"] == ""
    assert data["llm"]["model"] == ""


def test_write_llm_quotes_special_chars():
    """api_key 含引号/井号/反斜杠 → 双引号转义，roundtrip 不丢字符。"""
    nasty = 'sk-"x#y\\z'
    vals = {**_VALS, "api_key": nasty}
    after = write_llm_section("device: cpu\n", vals)
    assert _roundtrip_llm(after)["api_key"] == nasty


def test_write_llm_crlf_endings_preserved():
    """CRLF 文件：新节用 CRLF，未动行保持 CRLF。"""
    before = "device: cpu\r\nmedia:\r\n  sample_rate: 16000\r\n"
    after = write_llm_section(before, _VALS)
    assert "device: cpu\r\n" in after
    assert "\r\nllm:\r\n" in after
    assert _roundtrip_llm(after) == _VALS
