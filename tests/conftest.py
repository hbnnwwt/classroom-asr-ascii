"""测试只使用出厂模板、临时目录和假依赖。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from asr.config import load_config


@pytest.fixture
def template_config_path(tmp_path):
    """复制受版本控制的模板，所有相对资源路径落到临时目录。"""
    template = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    return config_path


@pytest.fixture
def template_config(template_config_path):
    """每个测试独立的出厂 AppConfig，不读取个人配置或真实模型。"""
    return load_config(template_config_path, strict_models=False)


@pytest.fixture
def web_runtime(tmp_path, template_config_path):
    """Web 测试的临时资源；业务 runtime 接口接入后复用这些路径。"""
    (tmp_path / "config.example.yaml").write_text(
        template_config_path.read_text(encoding="utf-8"), encoding="utf-8")
    upload_dir = tmp_path / "uploads"
    output_root = tmp_path / "outputs"
    upload_dir.mkdir()
    output_root.mkdir()
    return SimpleNamespace(
        root=tmp_path,
        config_path=template_config_path,
        db_path=tmp_path / "state.db",
        upload_dir=upload_dir,
        output_root=output_root,
    )
