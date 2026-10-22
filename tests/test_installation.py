"""Packaging and model downloads must work without personal config or model files."""
from __future__ import annotations

from email.parser import Parser
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import types
import zipfile

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    source = tmp_path_factory.mktemp("package-source")
    for name in ("pyproject.toml", "requirements.txt"):
        shutil.copyfile(ROOT / name, source / name)
    shutil.copytree(
        ROOT / "asr", source / "asr",
        ignore=shutil.ignore_patterns("__pycache__", "uploads", "*.db", "*.db-*"),
    )
    result = subprocess.run(
        [sys.executable, "-c", "from setuptools.build_meta import build_wheel; build_wheel('dist')"],
        cwd=source, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return next((source / "dist").glob("*.whl"))


def metadata(wheel):
    with zipfile.ZipFile(wheel) as archive:
        name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        return Parser().parsestr(archive.read(name).decode("utf-8"))


def test_wheel_declares_supported_python_versions(wheel):
    versions = SpecifierSet(metadata(wheel)["Requires-Python"])
    assert all(version in versions for version in ("3.10", "3.11", "3.12"))
    assert all(version not in versions for version in ("3.9", "3.13"))


def test_wheel_dependencies_match_runtime_requirements(wheel):
    expected = {
        str(Requirement(line.split("#", 1)[0].strip()))
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    }
    actual = {str(Requirement(value)) for value in metadata(wheel).get_all("Requires-Dist", [])}
    assert actual == expected
    assert not {Requirement(value).name for value in actual} & {"pytest", "httpx", "pytest-cov"}


def test_wheel_contains_browser_assets(wheel):
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for asset in (ROOT / "asr" / "web" / "static").rglob("*"):
        if asset.is_file():
            assert asset.relative_to(ROOT).as_posix() in names


@pytest.fixture
def downloader(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("download_models_under_test", ROOT / "scripts" / "download_models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    downloads = []
    monkeypatch.setenv("MODELSCOPE_CACHE", "pre-test-cache")
    def snapshot_download(model, **kwargs):
        downloads.append((model, kwargs))
        return str(tmp_path / "cache" / model)
    monkeypatch.setitem(sys.modules, "modelscope", types.SimpleNamespace(snapshot_download=snapshot_download))
    return module, downloads


def test_model_downloads_pass_manifest_revisions(downloader, tmp_path):
    module, downloads = downloader
    result = module.main(["--models-dir", str(tmp_path / "models")])
    assert result == 0
    assert [(model, options["revision"]) for model, options in downloads] == [
        ("iic/SenseVoiceSmall", "master"),
        ("iic/speech_fsmn_vad_zh-cn-16k-common-pytorch", "v2.0.4"),
        ("iic/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727", "v2.0.4"),
        ("iic/speech_campplus_sv_zh-cn_16k-common", "v2.0.2"),
        ("iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch", "v2.0.5"),
    ]
    assert all(options["cache_dir"] == str(tmp_path / "models") for _, options in downloads)


def test_model_download_supports_explicit_revision(downloader, tmp_path):
    module, downloads = downloader
    result = module.main([
        "--models-dir", str(tmp_path / "models"), "--model", "iic/SenseVoiceSmall",
        "--revision", "iic/SenseVoiceSmall=test-release",
    ])
    assert result == 0
    assert len(downloads) == 1
    assert downloads[0][0] == "iic/SenseVoiceSmall"
    assert downloads[0][1]["revision"] == "test-release"


def test_missing_revision_rejects_manifest_before_downloading(downloader, tmp_path):
    module, downloads = downloader
    manifest = tmp_path / "invalid.json"
    manifest.write_text(json.dumps({"schema_version": 1, "models": [{"id": "iic/SenseVoiceSmall"}]}))
    assert module.main(["--manifest", str(manifest), "--models-dir", str(tmp_path / "models")]) != 0
    assert downloads == []
    assert not (tmp_path / "models").exists()


def test_model_manifest_list_is_offline(downloader, capsys, monkeypatch):
    module, downloads = downloader
    monkeypatch.setitem(sys.modules, "modelscope", None)
    assert module.main(["--list"]) == 0
    assert downloads == []
    assert "master" in capsys.readouterr().out


def test_revision_override_does_not_claim_original_commit(downloader, capsys):
    module, downloads = downloader
    assert module.main(["--list", "--model", "iic/SenseVoiceSmall", "--revision", "iic/SenseVoiceSmall=custom-tag"]) == 0
    entry = json.loads(capsys.readouterr().out)[0]
    assert entry["revision"] == "custom-tag"
    assert entry.get("observed_commit") is None
    assert downloads == []
