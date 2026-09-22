"""batch 并发隔离（P1-2）：每个任务拿独立的 cfg 深拷贝。

pipeline.run 会原位修改 cfg（_quality_override / diarize.enabled /
output.formats，Task 7 复盘确认）。线程池共享一个 cfg 时并发任务互相踩；
这里验证 batch worker 给 pipeline 的是每任务独立副本。
"""
from pathlib import Path

from asr.batch import batch_run
from asr.config import load_config


def test_worker_gets_isolated_cfg_per_task(monkeypatch, tmp_path):
    """并发 worker 各拿独立 cfg 副本；共享 cfg 不被任何任务的写入污染。"""
    import asr.batch as bm

    seen = []

    def fake_run(input_path, cfg, overrides, *args, **kwargs):
        seen.append(cfg)
        # 模拟 pipeline.run 的三处原位写入
        cfg._quality_override = "fast"
        cfg.diarize.enabled = True
        cfg.output.formats = ["txt"]
        return "ok"

    monkeypatch.setattr(bm, "pipeline_run", fake_run)

    base_cfg = load_config("config.yaml", strict_models=False)
    inputs = [Path(f"f{i}.wav") for i in range(3)]
    res = batch_run(
        inputs, base_cfg,
        overrides={"output_dir": str(tmp_path)},
        workers=2,
    )

    assert res.succeeded == 3 and res.failed == 0
    assert len(seen) == 3
    # 每个任务一个独立副本，且都不是共享的 base_cfg
    assert all(c is not base_cfg for c in seen)
    assert len({id(c) for c in seen}) == 3
    # 原始 cfg 未被任何 worker 的写入污染
    assert getattr(base_cfg, "_quality_override", None) is None
    assert base_cfg.diarize.enabled is False
    assert base_cfg.output.formats == ["txt", "md", "srt", "vtt", "json"]
