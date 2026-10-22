"""Server-owned paths. HTTP input never chooses a filesystem configuration."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WebRuntime:
    config_path: Path
    db_path: Path
    upload_dir: Path
    output_root: Path
    static_dir: Path = field(default_factory=lambda: Path(__file__).parent / "static")
    password: str = ""
    workers: int = 1
    max_waiting: int = 8
    settings_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self):
        for name in ("config_path", "db_path", "upload_dir", "output_root", "static_dir"):
            setattr(self, name, Path(getattr(self, name)).resolve())

    def resolve_config(self, value=None) -> Path:
        if value not in (None, "", "default", "config.yaml"):
            raise ValueError("config_not_allowed")
        return self.config_path

    @classmethod
    def from_environment(cls):
        package = Path(__file__).resolve().parent
        return cls(config_path=Path(os.environ.get("ASR_WEB_CONFIG", "config.yaml")),
                   db_path=package / "web_state.db", upload_dir=package / "uploads",
                   output_root=package / "outputs", password=os.environ.get("ASR_WEB_PASSWORD", ""))
