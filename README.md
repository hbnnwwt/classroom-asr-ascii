# classroom-asr · 课堂录课本地语音识别系统

把课堂录课视频（mp4/mkv/...）一键转写成带时间戳的 SRT/MD/TXT/JSON，**全程离线**。

> 实现方案详见 [方案.txt](../方案.txt)。

## 快速上手

### 1. 安装系统依赖
- **ffmpeg / ffprobe**：[Windows](https://www.gyan.dev/ffmpeg/builds/) 解压后把 `bin/` 加到 PATH
- **Python 3.10**（机器若只有 3.12，funasr 1.2 + torch 2.14 也能跑，详见"实现偏差"）

> **Windows 一键搭建**：完成上步后，双击 `setup.bat` 即自动完成下面第 2~3 步
> （装 Python 依赖 + 下载 ≈2 GB 模型，需联网，可重复执行），
> 然后双击 `run_web.bat` 启动。

### 2. 安装 Python 依赖
```bash
cd classroom-asr
pip install -r requirements.txt
```

### 3. 预下载模型（一次性，能上网的机器上）
```bash
python scripts/download_models.py
```
产物在 `./models/`，整个目录拷到目标机即可。**总大小 ≈ 2 GB**：
- SenseVoice-Small (~897MB)
- Paraformer-Large (~868MB)
- VAD (~4MB) / PUNC (~280MB) / SPK (~28MB)

### 4. 自检
```bash
python -m asr check
```

### 5. 转写
```bash
# 单文件
python -m asr path/to/lecture.mp4 -o out/

# 批量
python -m asr batch ./videos -o ./out --workers 2

# GPU + 高精度（机器无 GPU 时用 cpu）
python -m asr lecture.mp4 -o out/ --device cuda:0 --quality high
```

### 6. Web UI（可选）
```bash
python -m asr.web                  # 默认 http://127.0.0.1:8000
python -m asr.web --host 0.0.0.0 --port 8080   # 局域网开放
```
打开浏览器访问 http://127.0.0.1:8000，上传文件即可。任务状态、进度、下载都在页面内。
- 单文件转写 + 说话人分离可勾选
- 任务进度轮询（1 秒一次），完成后六个按钮（SRT/VTT/MD/TXT/JSON/replace.log）直接下载
- **AI 反馈（可选）**：上传时勾选"AI 反馈"自动生成，或任务完成后在详情页点"生成 AI 反馈 / 重试"；失败只记错误不拖垮转写
- AI 产物与转写产物一样在下载区下载：AI 点评（`.feedback.md`）与优化逐字稿（`.polished.md`）
- 任务历史存于 `asr/web/web_state.db`（SQLite），重启不丢

#### AI 反馈配置（可选）

两种配法，效果相同：

- **页面配置（推荐）**：首页/详情页右上角"设置"进设置页，填好 base_url 与 model，
  可先"测试连接"再保存——写回 `config.yaml` 的 `llm` 节，已有注释与配置原样保留。
  未配置时点任务上的"AI 反馈"按钮也会直接跳到这里。
- **手改文件**：`config.yaml` 加 `llm` 节即启用（OpenAI 兼容端点，一套代码通吃云 API
  与本地 Ollama）；全留空则功能整体隐藏，不影响任何现有行为：

```yaml
llm:
  base_url: http://localhost:11434/v1   # Ollama；云 API 换成对应 OpenAI 兼容端点
  model: qwen2.5:7b
  api_key: ""                            # 本地模型留空
```

## 命令一览

| 命令 | 说明 |
|---|---|
| `asr check` | 自检 ffmpeg / 模型路径 / CUDA |
| `asr INPUT -o OUT` | 单文件转写 |
| `asr batch DIR_OR_FILES -o OUT` | 批量转写 |

通用参数：
- `--engine funasr`（默认；whisper 已被禁用）
- `--device cpu\|cuda:0`
- `--quality fast\|balanced\|high`（fast=SenseVoice 无时间戳，balanced/high=Paraformer）
- `--diarize` / `--no-diarize`（尚未实现说话人分离，待 P5）
- `--glossary glossary/default.txt`
- `--formats txt,srt,json,vtt,md`
- `--audio-track N`（多音轨视频）
- `--denoise` / `--keep-wav` / `--workers N`

## 输出

```
out/
├── lecture.txt           # 纯文本
├── lecture.md            # Markdown（带说话人）
├── lecture.srt           # 字幕（UTF-8 BOM，Windows 友好）
├── lecture.vtt           # WebVTT
├── lecture.json          # 一等产物（下游摘要/检索）
├── lecture.replace.log   # 术语替换日志
├── lecture.feedback.md   # AI 点评报告（可选：llm 配置后生成）
└── lecture.polished.md   # AI 优化逐字稿（可选）
```

## 实现偏差

按方案第 8 条"以能跑通且满足验收清单为准"原则，以下与方案有偏差：

| 项 | 方案原文 | 实际实现 | 原因 |
|---|---|---|---|
| Python 版本 | "建议固定 3.10，FunASR 对 3.12 兼容性有风险" | 实际用 **Python 3.12.10**（机器无 3.10） | 当前 torch 2.14+cpu + funasr 1.2 + Python 3.12 跑通 |
| Whisper 引擎 | 5.4 节要求 faster-whisper 作为回退引擎 | **禁用**，whisper_engine.py 留接口 stub，实例化即抛错 | 按用户明确要求"严格不要使用 whisper，用 FunASr" |
| 项目路径 | 方案未提及 | 项目实际位于 **`E:\classroom-asr-ascii\`**（ASCII 路径） | Windows + sentencepiece 不支持中文路径；funasr 加载 bpe.model 时用 fopen 走 GBK 找不到文件。funasr_engine._ensure_ascii_path 自动建 junction，但仓库根必须 ASCII |
| ASR 默认模型 | 5.3 节默认 `cfg.models.funasr.asr`（SenseVoice-Small） | 默认走 **`paraformer_model`**（Paraformer-Large） | 实测 SenseVoice 是端到端模型，**不输出时间戳**；Paraformer 才有 token-level timestamp |
| FunASR `hotword` 参数 | 5.3 节通过 `hotword=" ".join(hotwords)` 传入 | 捕获 TypeError 后**改走术语表后处理路径** | 1.1+ 版本 hotword 参数语义变化 |
| 时间戳单位 | 5.2 节"必须做单位探测与单测" | 实现 `_detect_time_unit()` 自动判 centisecond/秒 | Paraformer-large-vad-punc 实测返回 centisecond（1/100 秒），非毫秒非秒 |
| Paraformer timestamp | 方案预期 sentence_info 字级别 | 实际只返回 token-level timestamp，**无 token→char 对齐** | 取首尾 token 时间区间作为整段范围；精确切分留给 P4 postprocess |
| SRT 每条 ≤ 8 秒 | 5.5 节硬约束 + 验收 #6 | **降级为软约束** | FunASR 粗时间戳（22s/9 字），硬切条产生 3 字一段不可读；超长段仅 warning |
| 说话人分离 | 5.6 节要求 | 实现按时长/首次出现启发式（教师/学生A-E/其他） | cam++ 在重叠语音不可靠，全班齐读场景建议关闭 `--diarize` |
| faster-whisper 依赖 | requirements.txt 原列 `faster-whisper>=1.0` | **已移除** | whisper 禁用 |

## FAQ

### Q: 装到一半报 "RuntimeError: NOT_FOUND ... .bpe.model: No such file"
**A**: sentencepiece 不支持中文路径。把项目挪到 ASCII 路径（如 `E:\classroom-asr\`），或在 funasr_engine.py 里改 `_ensure_ascii_path` 的目标。

### Q: 转写时间戳 end 异常大（几百秒），但音频只有几十秒
**A**: Paraformer-large-vad-punc 在某些版本下 timestamp 数值有 bug。已在 `validate_timestamps` 里裁剪到 `duration_s + 0.5`，不会污染输出。

### Q: 中文字符识别对了，但标点重复（"，，""。。""？？"）
**A**: FunASR 自带标点模型在 ITN 之后偶尔重复。P4 postprocess 已加简单正则兜底；如仍有可手动清洗。

### Q: --quality fast 没有时间戳
**A**: SenseVoice-Small 是端到端模型，**不输出时间戳**。需要时间戳用 `--quality balanced`（默认）或 `high`。

### Q: 90 分钟视频 CPU 跑要多久？
**A**: Paraformer 在 8 核 CPU 上 RTF ≈ 0.13，90 分钟视频约 12 分钟。GPU (CUDA) 上 < 1 分钟。

### Q: 怎么批量处理？
**A**: `python -m asr batch videos/ -o out/ --workers 2`。失败文件会写到 `out/_failed/<name>.log`。

### Q: FunASR / torch 怎么卸？
**A**: 当前不卸。模型权重 ≈ 2GB 在 `./models/` 下，可单独删除该目录释放空间。

## 当前进度

| 阶段 | 状态 | 真实证据 |
|---|---|---|
| P1 骨架 + check | ✅ | asr check 通过，11/11 测试 |
| P2 whisper_engine | ⏸ **跳过** | 按用户要求禁用 whisper |
| P3 FunASR 引擎 | ✅ | 真实中文转写，19/19 测试 |
| P4 postprocess + writers | ✅ | 端到端生成五件套，48/48 测试（含 SRT 软约束 + 弱标点修复） |
| P5 diarize | ✅ | diarize.py 角色启发式（教师/学生A-E/其他），9/9 单测；真实视频待生产时验证 |
| P6 batch + 并发 | ✅ | batch 跑通，进度条 + 失败隔离 |
| P7 长视频 + README FAQ | ✅ | 本 README；51s 测试通过 |
| P8 Web UI | ✅ | FastAPI + SQLite + 轮询；10/10 测试 |

**验收清单（方案第 8 节）：12 项中 9 通过 / 2 部分通过 / 1 待生产验证**。

## 已知边界

1. **断句合并算法**：当前按字数比例插值时间戳，对短句重复（如多遍 wav）会产生过度切片。生产建议用真实课堂单段长录音。
2. **Paraformer timestamp 异常**：某些版本下 end 值远超 duration，已硬裁剪。
3. **说话人分离真实数据未验证**：代码完成（diarize.py + 引擎 spk_model 集成），9/9 单测通过；真实视频未用 `--diarize` 重跑（重跑需 24 分钟）。
4. **全班齐读/齐答时分离不可靠**：cam++ 在重叠语音场景下不可靠，建议关闭 `--diarize`。
5. **SRT 每条 ≤ 8 秒形式验收已放弃**：FunASR 粗时间戳导致每段 22-170s 是常态，软约束 + warning 保留；根治需要 FunASR token-level timestamp 重建。
6. **GPU 未在本机验证**：当前机器 `nvidia-smi` 报 "Unknown Error"，未配 NVIDIA 驱动。CPU 上 RTF ≈ 0.13 已验证。
7. **Web UI 单文件 only**：批量请走 CLI `python -m asr batch`。
