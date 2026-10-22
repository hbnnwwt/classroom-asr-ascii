"""产物的完整文件名与原子发布。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def artifact_path(base: Path, suffix: str) -> Path:
    """在完整 stem 后追加扩展名，保留 lesson.1 这样的名称。"""
    return Path(str(base) + suffix)


def atomic_write_text(path: Path, text: str) -> None:
    """同目录写完并关闭后替换；失败时保留上次已发布的完整文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=path.parent, prefix="." + path.name + ".",
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
