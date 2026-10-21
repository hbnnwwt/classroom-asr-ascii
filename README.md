# classroom-asr · 课堂录课本地语音识别系统

把课堂录课视频（mp4/mkv/...）一键转写成带时间戳的 SRT/MD/TXT/JSON，**全程离线**；可选接入 LLM（本地 Ollama 或云 API）生成 AI 点评与优化逐字稿。

## 快速开始

### Windows 一键（推荐）

1. 安装 [ffmpeg](https://www.gyan.dev/ffmpeg/builds/)，解压后把 `bin/` 加进 PATH
2. 双击 `setup.bat` —— 装 Python 依赖 + 下载 ≈2 GB 模型（需联网，可重复执行；首次运行自动从模板生成本地 `config.yaml`）
3. 双击 `run_web.bat`，浏览器打开 http://127.0.0.1:8000

### 手动安装

- Python 3.10+（3.12 + funasr 1.2 + torch 2.14 实测可跑）
- `pip install -r requirements.txt`
- `python scripts/download_models.py` —— 产物在 `./models/`，整个目录拷到目标机即可
- 自检：`python -m asr check`

## Web 界面

上传 → 进度 → 下载全在页面内：

- 单文件转写，说话人分离可勾选；任务进度 1 秒轮询
- 完成后直接下载 SRT / VTT / MD / TXT / JSON / replace.log；多段任务另有每段独立字幕（时间轴归零，配各段原视频）
- **AI 反馈（可选）**：上传时勾选，或完成后点"生成 AI 反馈"；已有产物时变"重新生成"（确认后才覆盖并重新调用 LLM）；失败只记错误不拖垮转写
- **在线阅读**：AI 点评 / 优化稿在弹层内渲染 Markdown（服务端先转义后渲染），下载看完再定；优化稿带"修改痕迹"红字页签——删的词红底划线、新增绿色、标点修正只标变化字符
- 任务历史存 SQLite（`asr/web/web_state.db`），重启不丢
- 批量处理请走命令行

```bash
python -m asr.web                  # 默认 http://127.0.0.1:8000
python -m asr.web --host 0.0.0.0 --port 8080   # 局域网开放
```

## 命令行

| 命令 | 说明 |
|---|---|
| `python -m asr INPUT -o OUT` | 单文件转写 |
| `python -m asr batch DIR -o OUT --workers 2` | 批量转写（失败写到 `out/_failed/<name>.log`） |
| `python -m asr check` | 自检 ffmpeg / 模型路径 / CUDA |

通用参数：`--device cpu\|cuda:0` · `--quality fast\|balanced\|high`（fast=SenseVoice 无时间戳，balanced/high=Paraformer） · `--diarize`（说话人分离，启发式角色标注） · `--glossary glossary/default.txt` · `--formats txt,srt,json,vtt,md` · `--audio-track N`（多音轨） · `--denoise` · `--keep-wav` · `--workers N`

## AI 反馈配置（可选）

两种配法，效果相同：

- **页面配置（推荐）**：首页/详情页右上角"设置"进设置页，"常用服务商"下拉一键填充 base_url 与 model（Ollama 本地 / DeepSeek / 硅基流动 / ModelScope / MiniMax / 智谱 / OpenAI，api_key 自填），可先"测试连接"再保存
- **手改文件**：`config.yaml` 加 `llm` 节即启用（OpenAI 兼容端点，云 API 与本地 Ollama 一套代码通吃）；全留空则功能整体隐藏，不影响任何现有行为

```yaml
llm:
  base_url: http://localhost:11434/v1   # Ollama；云 API 换成对应 OpenAI 兼容端点
  model: qwen2.5:7b
  api_key: ""                            # 本地模型留空
```

> `config.yaml` 含 api_key，**不进仓库**（已 gitignore）；仓库只带模板 `config.example.yaml`——新克隆先复制一份（`setup.bat` 会自动做）。

## 输出

```
out/
├── lecture.txt / .md / .srt / .vtt / .json   # 转写五件套（.json 是一等产物，供下游摘要/检索）
├── lecture.replace.log                       # 术语替换日志
├── lecture.feedback.md                       # AI 点评报告（可选）
└── lecture.polished.md                       # AI 优化逐字稿（可选）
```

## FAQ

- **报 "RuntimeError: NOT_FOUND ... .bpe.model: No such file"** —— sentencepiece 不支持中文路径，把项目挪到 ASCII 路径（如 `E:\classroom-asr\`）。
- **转写时间戳 end 异常大（几百秒）** —— Paraformer 某些版本 timestamp 有 bug，已在 `validate_timestamps` 裁剪，不污染输出。
- **中文字符对但标点重复（"，，""？？"）** —— FunASR 标点模型偶发，postprocess 有正则兜底。
- **`--quality fast` 没有时间戳** —— SenseVoice 是端到端模型不输出时间戳，要时间戳用 balanced（默认）或 high。
- **90 分钟视频 CPU 要多久** —— 8 核 CPU RTF ≈ 0.13，约 12 分钟；GPU < 1 分钟。
- **全班齐读时说话人分离乱** —— cam++ 重叠语音不可靠，建议关 `--diarize`。
- **SRT 一条字幕二三十秒** —— FunASR 粗时间戳的常态，已降级为软约束（详见 [docs/DEVNOTES.md](docs/DEVNOTES.md)）。
