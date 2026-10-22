# classroom-asr · 课堂录课转写

把课堂视频或音频转成 SRT、TXT、Markdown、VTT、JSON。模型下载完成后，语音识别在本机运行。可选 AI 整理使用配置的 OpenAI 兼容服务；选择云端服务时，逐字稿会发送到该服务。

## Windows 快速开始

需要 **Python 3.10–3.12** 和 `ffmpeg` / `ffprobe`。将 ffmpeg 的 `bin` 目录加入 PATH，然后在项目目录执行：

1. 双击 `setup.bat`。它创建项目 `.venv`、安装依赖，并在缺失时从模板生成 `config.yaml`。
2. 联网时显式下载模型：

   ```powershell
   .\.venv\Scripts\python.exe -X utf8 scripts\download_models.py --list
   .\.venv\Scripts\python.exe -X utf8 scripts\download_models.py
   .\.venv\Scripts\python.exe -m asr check
   ```

3. 双击 `run_web.bat`，打开 **http://127.0.0.1:8765**。

安装器和启动器使用同一个 `.venv` 解释器。未安装 Python launcher 时，可先在命令行设置 `ASR_PYTHON` 为受支持的 `python.exe` 完整路径，再运行 `setup.bat`。端口占用时启动失败并提示换端口，不会终止占用进程：

```powershell
.\run_web.bat --port 8766
```

`setup.bat` 不自动下载模型。Python 3.12 使用 [CPU 候选约束](constraints/cpu-py312.txt)；它只约束部分依赖，已核验公开版本元数据，尚未完成该组合的真实模型验收。GPU 环境需要按硬件、驱动选择匹配的 PyTorch/TorchAudio。

## 手动安装与启动

以下为 PowerShell 示例；Linux/macOS 将解释器路径换为 `.venv/bin/python`：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints/cpu-py312.txt --extra-index-url https://download.pytorch.org/whl/cpu
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\python.exe scripts/download_models.py
.\.venv\Scripts\python.exe -m asr check
.\.venv\Scripts\python.exe -m asr.web
```

**模块启动默认端口为 8000**，与双击脚本的 8765 不同。已有 `config.yaml` 时跳过复制。Python 3.10/3.11 或非 Windows 环境可使用 `-m pip install -r requirements.txt`，不要直接套用 Windows/Python 3.12 CPU 约束。

模型下载清单见 [models.revisions.json](models.revisions.json)。四个模型使用已核验 tag；SenseVoice 使用可变 `master`。`observed_commit` 是核验时的观察记录，下载 SDK 不接受它作为裸 commit 锁；当前清单不保证逐字节复现。可用 `--model ID` 选择单个模型、`--revision ID=TAG` 显式覆盖版本。

## Web 使用

上传后，页面依次展示准备录音、生成文字、整理结果。运行时显示最近 80 段；完成后每页 80 段，可搜索全文、复制全文及下载实际生成的格式。多文件上传按页面顺序组成一个课堂项目；独立文件批处理使用 CLI。

| 选项 | 能力与边界 |
|---|---|
| 字幕转写（推荐）/ `balanced` | 默认使用 Paraformer，提供模型时间戳；仍需核对字幕和术语 |
| 快速文字 / `fast` | 使用 SenseVoice，无精细时间戳；字幕时间信息较粗 |
| `high` | 保留 CLI/历史请求兼容；当前与 balanced 使用同一 Paraformer，不承诺更高准确率 |
| 区分不同说话人（实验性） | 按声音区分发言者；标签和旧版师生角色均需人工核对，跨语音组身份归并尚未验收 |

Web 默认一次执行 1 个任务，最多等待 8 个。队满会明确拒绝受理。上传进度达到 100% 只表示发送完成；取消后，以服务端确认结果为准：若任务已受理，任务继续处理并提供入口。取消上传不等于终止推理。等待中、运行中或后台仍活跃的任务不能删除。

AI 提供教学点评、整理后的逐字稿、课堂讲义，可分别生成和重跑。AI 状态与转写状态独立，生成 AI 时仍可阅读、下载转写结果。截断、过滤或写入失败会保留上次完整 AI 稿；系统不自动续写或重复调用服务。

## 配置与局域网访问

Web 在启动时固定配置路径，默认是启动目录的 `config.yaml`：

```powershell
.\.venv\Scripts\python.exe -m asr.web --config D:\classroom\config.yaml --port 8000
```

也可设置 `ASR_WEB_CONFIG`。旧 HTTP `config` 参数只接受省略、`default`、`config.yaml`，均指向启动时固定的文件；其他路径返回 400。CLI 的 `--config` 仍可选择文件。

未设置密码时仅允许真实本机连接，并要求通过 `localhost`、`127.0.0.1` 或 `[::1]` 等字面 loopback 地址访问；自定义域名即使指向本机也会被拒绝。局域网监听或自定义域名访问必须先设置密码：

```powershell
$env:ASR_WEB_PASSWORD = '请替换为自己的访问密码'
.\.venv\Scripts\python.exe -m asr.web --host 0.0.0.0 --port 8080
```

浏览器 HTTP Basic 用户名为 **`classroom`**，密码为环境变量值；页面、API、静态资源和下载均受保护。HTTP Basic 本身不加密传输，跨不可信网络使用 HTTPS 入口。应用按真实连接地址判断本机，不信任转发头。

AI 可在设置页配置，也可编辑 `config.yaml`：

```yaml
llm:
  base_url: http://localhost:11434/v1
  model: qwen2.5:7b
  api_key: ""
  timeout: 300
  max_tokens: 16384
```

`config.yaml` 可能含密钥，已被 Git 忽略。设置接口不返回明文密钥：省略或 `null` 表示保留，非空字符串替换，空字符串清空。连接测试使用相同语义但不保存。迁移旧客户端时，不要把未编辑的密码框作为空字符串提交。

## 命令行

```powershell
.\.venv\Scripts\python.exe -m asr run lecture.mp4 -o out --quality balanced --formats txt,srt,json
.\.venv\Scripts\python.exe -m asr batch recordings -o out --workers 2
.\.venv\Scripts\python.exe -m asr check
```

常用参数：`--config`、`--device cpu|cuda:0`、`--quality fast|balanced|high`、`--diarize`、`--glossary glossary/default.txt`、`--audio-track N`、`--denoise`、`--keep-wav`。完整选项见 `python -m asr run --help`。

批处理开始前会拒绝同输出 stem 的输入，例如不同目录的 `lesson.mp4` 与 `lesson.wav`，以免覆盖。带点文件名会完整保留：`lesson.1.mp4` 生成 `lesson.1.srt`。`--keep-wav` 的副本放在输出目录 `_wav/`，临时工作文件仍会清理。

Web 的 SQLite、上传和输出默认位于 `asr/web/`。升级前备份这些数据及个人配置；新数据库迁移保留旧记录，已有产物不批量改名，项目重命名不改输出文件名。

## 开发与验收

默认测试使用模板配置、临时 SQLite、合成 WAV 和 fake 引擎/LLM，不读取个人配置、不下载模型、不调用真实云服务：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

仅做轻量开发时，可按 [验收文档](docs/validation.md) 安装不含 FunASR/PyTorch 的依赖子集。GitHub Actions 配置 Python 3.10/3.11/3.12 轻量矩阵；本地通过不代表远端矩阵已通过。

- [验收命令、浏览器检查与真实样本协议](docs/validation.md)
- [Web 接口与客户端迁移约定](docs/web-contract.md)
- [架构、历史记录和已知边界](docs/DEVNOTES.md)

当前未给出真实多人身份归并、GPU 性能、长课吞吐或长期内存保证。这些能力需要按真实数据协议单独测量。
