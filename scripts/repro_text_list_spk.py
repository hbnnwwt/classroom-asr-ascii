"""最小复现 v2：diarize=True（spk_model 加载 + return_spk_res=True）

验证假设：开启 spk 后 funasr 1.4.16 返回 sentence_info，
且 punc_array 与 timestamp 长度不一致时 text 是 list 而非 str
→ funasr_engine._parse_results 259 行 .strip() 崩溃。
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os_cwd = Path(__file__).resolve().parent.parent

from asr.config import load_config

WAV = Path("tmp_repro_15min.wav")
CUT_S = 900  # 15 分钟


def main() -> None:
    src = sorted((os_cwd / "asr" / "web" / "uploads").glob("b5a3f6c7a552_*.mp4"))[0]
    if not WAV.exists():
        subprocess.run(
            ["D:/ffmpeg/bin/ffmpeg", "-y", "-i", str(src), "-t", str(CUT_S),
             "-ac", "1", "-ar", "16000", str(WAV)],
            check=True, capture_output=True,
        )

    import os
    os.environ["MODELSCOPE_OFFLINE"] = "1"
    from funasr import AutoModel

    cfg = load_config(os_cwd / "config.yaml", strict_models=False)
    model = AutoModel(
        model=str(cfg.asr.paraformer_model),
        vad_model=str(cfg.models.funasr.vad),
        punc_model=str(cfg.models.funasr.punc),
        spk_model=str(cfg.models.funasr.spk),   # ← 与 web 勾选 diarize 一致
        device=cfg.device,
        disable_update=True,
        ncpu=max(1, cfg.num_workers),
    )

    kwargs = dict(
        input=str(WAV),
        batch_size_s=cfg.asr.batch_size_s,
        language="zh",
        use_itn=cfg.asr.use_itn,
        return_spk_res=True,                     # ← diarize 开
        progress_callback=lambda end, total: None,
    )
    results = model.generate(**kwargs)

    print("=" * 60)
    for i, item in enumerate(results):
        print(f"--- results[{i}] keys: {sorted(item.keys())}")
        for k, v in item.items():
            if k == "sentence_info":
                print(f"  sentence_info: type={type(v).__name__} len={len(v) if v else 0}")
                if v:
                    for idx in (0, min(1, len(v) - 1)):
                        s0 = v[idx]
                        types = {kk: type(vv).__name__ for kk, vv in s0.items()}
                        print(f"    [{idx}] field types: {types}")
                        t0 = s0.get("text")
                        print(f"    [{idx}] text repr: {str(t0)[:120]}")
            elif k == "text":
                print(f"  text: type={type(v).__name__} = {str(v)[:80]}")
            else:
                print(f"  {k}: type={type(v).__name__}")
    print("=" * 60)

    # 直接跑项目里的 _parse_results 复现崩溃
    from asr.engines.funasr_engine import FunASREngine
    eng = FunASREngine(cfg)
    try:
        segs = eng._parse_results(results)
        print(f"_parse_results OK: {len(segs)} segments")
        if segs:
            print(f"  first: {segs[0]}")
    except Exception as e:
        print(f"_parse_results CRASHED: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
