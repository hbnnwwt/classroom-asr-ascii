"""最小复现：P0 崩溃 'list' object has no attribute 'strip'

用与 funasr_engine.transcribe 完全相同的 kwargs 调 generate()，
打印原始返回结构，确认 text/sentence_info 的真实类型。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os_cwd = Path(__file__).resolve().parent.parent

from asr.config import load_config

WAV = Path("tmp_repro_5min.wav")


def main() -> None:
    # 1. 切前 5 分钟音频（16k mono，与 pipeline 一致）
    import subprocess

    src = sorted((os_cwd / "asr" / "web" / "uploads").glob("b5a3f6c7a552_*.mp4"))[0]
    if not WAV.exists():
        subprocess.run(
            ["D:/ffmpeg/bin/ffmpeg", "-y", "-i", str(src), "-t", "300",
             "-ac", "1", "-ar", "16000", str(WAV)],
            check=True, capture_output=True,
        )

    # 2. 与 FunASREngine._load_model 相同的模型装配
    import os
    os.environ["MODELSCOPE_OFFLINE"] = "1"
    from funasr import AutoModel

    cfg = load_config(os_cwd / "config.yaml", strict_models=False)
    model = AutoModel(
        model=str(cfg.asr.paraformer_model),
        vad_model=str(cfg.models.funasr.vad),
        punc_model=str(cfg.models.funasr.punc),
        spk_model=None,
        device=cfg.device,
        disable_update=True,
        ncpu=max(1, cfg.num_workers),
    )

    # 3. 与 transcribe 相同的 generate kwargs
    kwargs = dict(
        input=str(WAV),
        batch_size_s=cfg.asr.batch_size_s,
        language="zh",
        use_itn=cfg.asr.use_itn,
        return_spk_res=cfg.diarize.enabled,
        progress_callback=lambda end, total: None,
    )
    results = model.generate(**kwargs)

    # 4. 解剖返回结构
    print("=" * 60)
    print(f"results: type={type(results).__name__} len={len(results)}")
    for i, item in enumerate(results):
        print(f"--- results[{i}] keys: {sorted(item.keys())}")
        for k, v in item.items():
            if k == "sentence_info":
                print(f"  sentence_info: type={type(v).__name__} len={len(v) if v else 0}")
                if v:
                    s0 = v[0]
                    print(f"    [0] keys={sorted(s0.keys())}")
                    for kk, vv in s0.items():
                        preview = str(vv)[:60] if not isinstance(vv, list) else f"list[{len(vv)}] {str(vv[:5])[:60]}"
                        print(f"      {kk}: type={type(vv).__name__} = {preview}")
            elif k == "timestamp":
                print(f"  timestamp: type={type(v).__name__} len={len(v)} first={v[:2]}")
            elif k == "text":
                print(f"  text: type={type(v).__name__} = {str(v)[:100]}")
            else:
                print(f"  {k}: type={type(v).__name__} = {str(v)[:60]}")
    print("=" * 60)


if __name__ == "__main__":
    main()
