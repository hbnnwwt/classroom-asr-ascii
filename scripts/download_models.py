"""Explicit model download; inspect revisions offline with ``--list`` first.

The manifest records public revisions and their observed commits. ModelScope's
SDK accepts branches/tags; SenseVoice has no public tag and its master can move.
This is a revision manifest, not a claim of byte-for-byte reproducibility.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
MANIFEST_PATH = ROOT / "models.revisions.json"


def load_manifest(path: Path) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("模型清单 schema_version 必须为 1")
    entries = document.get("models")
    if not isinstance(entries, list) or not entries:
        raise ValueError("模型清单必须包含非空 models 列表")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("模型条目必须为对象")
        for field in ("id", "revision"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f"模型条目缺少非空 {field}")
        if entry["id"] in seen:
            raise ValueError(f"模型 ID 重复: {entry['id']}")
        seen.add(entry["id"])
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--models-dir", type=Path, default=MODELS_DIR)
    parser.add_argument("--model", action="append", help="只下载此模型 ID；可重复")
    parser.add_argument("--revision", action="append", default=[], metavar="ID=REVISION", help="覆盖指定模型的 branch/tag；可重复")
    parser.add_argument("--list", action="store_true", help="离线列出有效下载清单，不导入 ModelScope")
    args = parser.parse_args(argv)
    try:
        entries = load_manifest(args.manifest)
        known = {entry["id"] for entry in entries}
        if args.model and set(args.model) - known:
            raise ValueError("--model 必须为清单中的模型 ID")
        overrides = {}
        for value in args.revision:
            model, separator, revision = value.partition("=")
            if not separator or model not in known or not revision.strip():
                raise ValueError("--revision 必须为清单中的 ID=非空REVISION")
            overrides[model] = revision
        selected = [dict(entry, revision=overrides.get(entry["id"], entry["revision"]))
                    for entry in entries if not args.model or entry["id"] in args.model]
        for entry in selected:
            if entry["id"] in overrides:
                entry.update(revision_kind="explicit", observed_commit=None, source=None)
    except (OSError, ValueError) as exc:
        print(f"[ERROR] 模型清单无效: {exc}", file=sys.stderr)
        return 1

    if args.list:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
        return 0

    os.environ["MODELSCOPE_CACHE"] = str(args.models_dir)
    try:
        from modelscope import snapshot_download
    except ImportError:
        print("[ERROR] 请先用项目 .venv 的 Python 安装 requirements.txt", file=sys.stderr)
        return 1

    args.models_dir.mkdir(parents=True, exist_ok=True)
    for entry in selected:
        model, revision = entry["id"], entry["revision"]
        if entry.get("revision_kind") == "branch" and model not in overrides:
            print(f"[WARNING] {model}@{revision} 是可变分支，不能保证复现已观察提交。", file=sys.stderr)
        print(f"\n>>> 下载 {model}@{revision}")
        try:
            path = snapshot_download(model, revision=revision, cache_dir=str(args.models_dir))
            print(f"    OK -> {path}")
        except Exception as exc:
            print(f"    FAIL: {exc}", file=sys.stderr)
            return 2
    print(f"\n全部下载完成。模型位于: {args.models_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
