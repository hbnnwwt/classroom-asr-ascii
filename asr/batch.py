"""
批量处理（方案 P6）：
- 多文件并发（CPU 用 ThreadPoolExecutor，避免 GIL 影响 IO 密集的 ffmpeg）
- 单文件失败不影响整体
- 进度条（tqdm）
- 失败文件写 out/_failed/<name>.log
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import List

from loguru import logger
from tqdm import tqdm

from .config import AppConfig
from .pipeline import run as pipeline_run


@dataclass
class BatchResult:
    total: int
    succeeded: int
    failed: int
    failures: List[dict]   # [{file, error}]


def batch_run(
    inputs: List[Path],
    cfg: AppConfig,
    *,
    overrides: dict | None = None,
    workers: int = 1,
) -> BatchResult:
    """
    批量处理多个输入文件。
    - workers: 并发数。GPU 时强制 1；CPU 时建议 2~4
    - overrides: 透传给 pipeline.run
    - 单文件失败 → 写 out/_failed/<name>.log，继续处理其他文件
    """
    overrides = overrides or {}
    if cfg.device.startswith("cuda") and workers > 1:
        logger.warning(f"GPU 模式下强制 workers=1（请求 {workers}）")
        workers = 1

    output_dir = Path(overrides.get("output_dir", cfg.output.dir))
    output_dir.mkdir(parents=True, exist_ok=True)

    failures: List[dict] = []
    succeeded = 0

    def _one(p: Path) -> tuple[Path, str | None]:
        try:
            # P1-2：pipeline.run 会原位改 cfg（_quality_override / diarize.enabled /
            # output.formats）。线程池共享一个 cfg 时任务互相踩，每任务深拷贝隔离。
            r = pipeline_run(p, copy.deepcopy(cfg), overrides)
            return p, None
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            logger.error(f"[batch] {p.name} 失败: {err}")
            failed_dir = output_dir / "_failed"
            failed_dir.mkdir(parents=True, exist_ok=True)
            (failed_dir / f"{p.stem}.log").write_text(err, encoding="utf-8")
            return p, err

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {ex.submit(_one, p): p for p in inputs}
        for fut in tqdm(as_completed(futures), total=len(inputs), desc="batch"):
            p, err = fut.result()
            if err:
                failures.append({"file": str(p), "error": err})
            else:
                succeeded += 1

    return BatchResult(
        total=len(inputs),
        succeeded=succeeded,
        failed=len(failures),
        failures=failures,
    )
