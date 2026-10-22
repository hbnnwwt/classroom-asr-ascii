"""Exercise Windows launchers with isolated executables and harmless commands."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows batch launchers")


class IsolatedEnvironment(dict):
    def __repr__(self):
        return "<isolated subprocess environment>"


@pytest.fixture
def launcher_workspace(tmp_path):
    tmp_path = tmp_path / "class room"
    tmp_path.mkdir()
    for name in ("run_web.bat", "setup.bat"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    commands = tmp_path / "commands"
    commands.mkdir()
    # These replace process inspection/termination; a regression must never kill a process.
    (commands / "netstat.cmd").write_text(
        "@echo off\necho TCP 127.0.0.1:8765 0.0.0.0:0 LISTENING 424242\n"
    )
    (commands / "taskkill.cmd").write_text(
        '@echo off\necho attempted>"%ASR_TEST_KILL_LOG%"\nexit /b 0\n'
    )
    (commands / "py.cmd").write_text("@echo off\nexit /b 0\n")
    env = IsolatedEnvironment(os.environ.copy())
    env["PATH"] = str(commands) + os.pathsep + env["PATH"]
    env["ASR_TEST_KILL_LOG"] = str(tmp_path / "terminated.txt")
    env["PYTHONHOME"] = sys.base_prefix
    env["ASR_TEST_LAUNCH_LOG"] = str(tmp_path / "launch.json")
    env["ASR_TEST_INSTALL_LOG"] = str(tmp_path / "install.jsonl")
    env["ASR_PYTHON"] = sys.executable
    return tmp_path, env


def run_batch(workspace, name="run_web.bat", *args):
    directory, env = workspace
    return subprocess.run(
        ["cmd.exe", "/d", "/c", name, *args], cwd=directory, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
    )


def install_fake_web(workspace, *, occupied=False):
    directory, _ = workspace
    scripts = directory / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(sys.executable, scripts / "python.exe")
    package = directory / "asr"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "web.py").write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['ASR_TEST_LAUNCH_LOG']).write_text("
        "json.dumps({'executable': sys.executable, 'args': sys.argv[1:]}))\n"
        + ("print('Address already in use'); sys.exit(1)\n" if occupied else "")
    )


def test_launcher_never_terminates_a_port_owner(launcher_workspace):
    run_batch(launcher_workspace)
    directory, _ = launcher_workspace
    assert not (directory / "terminated.txt").exists()


def test_launcher_requires_project_environment(launcher_workspace):
    result = run_batch(launcher_workspace)
    assert result.returncode != 0
    assert b"setup.bat" in result.stdout


def test_launcher_uses_local_python_and_forwards_arguments(launcher_workspace):
    install_fake_web(launcher_workspace)
    result = run_batch(launcher_workspace, "run_web.bat", "--port", "8999", "--config", "class room.yaml")
    assert result.returncode == 0, result.stdout + result.stderr
    directory, _ = launcher_workspace
    data = json.loads((directory / "launch.json").read_text())
    assert Path(data["executable"]) == directory / ".venv" / "Scripts" / "python.exe"
    assert data["args"] == ["--host", "127.0.0.1", "--port", "8765", "--port", "8999", "--config", "class room.yaml"]


def test_occupied_port_reports_failure_without_termination(launcher_workspace):
    install_fake_web(launcher_workspace, occupied=True)
    result = run_batch(launcher_workspace)
    assert result.returncode != 0
    assert b"port" in result.stdout.lower()
    directory, _ = launcher_workspace
    assert not (directory / "terminated.txt").exists()


def install_fake_installer(workspace):
    directory, _ = workspace
    (directory / "config.example.yaml").write_text("engine: funasr\n")
    (directory / "requirements.txt").write_text("example-runtime>=1.0\n")
    (directory / "venv.py").write_text(
        "import json, os, pathlib, shutil, sys\n"
        "scripts = pathlib.Path(sys.argv[-1]) / 'Scripts'\n"
        "scripts.mkdir(parents=True)\n"
        "shutil.copyfile(sys.executable, scripts / 'python.exe')\n"
        "with open(os.environ['ASR_TEST_INSTALL_LOG'], 'a') as log:\n"
        "    log.write(json.dumps({'module': 'venv', 'executable': sys.executable, 'args': sys.argv[1:]}) + '\\n')\n"
    )
    (directory / "pip.py").write_text(
        "import json, os, sys\n"
        "with open(os.environ['ASR_TEST_INSTALL_LOG'], 'a') as log:\n"
        "    log.write(json.dumps({'module': 'pip', 'executable': sys.executable, 'args': sys.argv[1:]}) + '\\n')\n"
        "sys.exit(int(os.environ.get('ASR_TEST_PIP_FAILURE', '0')))\n"
    )
    scripts = directory / "scripts"
    scripts.mkdir()
    (scripts / "download_models.py").write_text(
        "from pathlib import Path\nPath('model-download-attempted').touch()\n"
    )


def test_setup_creates_local_environment_and_keeps_download_explicit(launcher_workspace):
    install_fake_installer(launcher_workspace)
    result = run_batch(launcher_workspace, "setup.bat")
    assert result.returncode == 0, result.stdout + result.stderr
    directory, _ = launcher_workspace
    events = [json.loads(line) for line in (directory / "install.jsonl").read_text().splitlines()]
    assert events[0]["module"] == "venv"
    assert Path(events[0]["executable"]) == Path(sys.executable)
    pip_events = [event for event in events if event["module"] == "pip"]
    assert pip_events
    assert all(Path(event["executable"]) == directory / ".venv" / "Scripts" / "python.exe" for event in pip_events)
    assert pip_events[0]["args"][:3] == ["install", "-r", "requirements.txt"]
    assert (directory / "config.yaml").read_text() == "engine: funasr\n"
    assert not (directory / "model-download-attempted").exists()


def test_setup_reuses_environment_and_preserves_existing_config(launcher_workspace):
    install_fake_web(launcher_workspace)
    install_fake_installer(launcher_workspace)
    directory, env = launcher_workspace
    env["ASR_PYTHON"] = str(directory / "missing-python.exe")
    (directory / "config.yaml").write_text("existing-user-settings\n")
    result = run_batch(launcher_workspace, "setup.bat")
    assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line) for line in (directory / "install.jsonl").read_text().splitlines()]
    assert all(event["module"] == "pip" for event in events)
    assert (directory / "config.yaml").read_text() == "existing-user-settings\n"


def test_setup_returns_dependency_install_failure(launcher_workspace):
    install_fake_installer(launcher_workspace)
    directory, env = launcher_workspace
    env["ASR_TEST_PIP_FAILURE"] = "7"
    result = run_batch(launcher_workspace, "setup.bat")
    assert result.returncode != 0
    assert not (directory / "model-download-attempted").exists()
