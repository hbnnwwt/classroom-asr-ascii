"""
模型预下载脚本（在**能上网**的机器上跑一次，之后把整个 models/ 目录拷到目标机）。

执行：
    python scripts/download_models.py

下载列表见方案第 2.3 节。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 把仓库根加进 path，下载完后不依赖项目代码也能跑
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODELS_DIR = ROOT / "models"

MODELS = [
    "iic/SenseVoiceSmall",
    "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "iic/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727",
    "iic/speech_campplus_sv_zh-cn_16k-common",
    "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
]


def main() -> int:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from modelscope import snapshot_download
    except ImportError:
        print("[ERROR] 请先 pip install modelscope", file=sys.stderr)
        return 1

    # 锁缓存到 ./models；HF 同时关掉（有些模型走 hf hub）
    os.environ["MODELSCOPE_CACHE"] = str(MODELS_DIR)
    os.environ.pop("MODELSCOPE_OFFLINE", None)

    for m in MODELS:
        print(f"\n>>> 下载 {m}")
        try:
            path = snapshot_download(m, cache_dir=str(MODELS_DIR))
            print(f"    OK -> {path}")
        except Exception as e:
            print(f"    FAIL: {e}", file=sys.stderr)
            return 2
    print(f"\n全部下载完成。模型位于: {MODELS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
