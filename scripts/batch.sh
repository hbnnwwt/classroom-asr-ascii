#!/usr/bin/env bash
# 批量示例：把 ./videos/ 下所有视频转写到 ./out/
# Windows 用户在 Git Bash 或 WSL 里跑。

set -euo pipefail
cd "$(dirname "$0")/.."

# 默认 CPU 配置；GPU 用户换成 -c config.gpu.yaml
python -m asr batch ./videos -o ./out -c config.yaml --workers 2
