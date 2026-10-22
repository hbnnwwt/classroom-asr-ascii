# 验收与测试协议

本项目区分代码回归、浏览器交互和真实模型验收。fake 引擎通过只能证明编排、状态和产物流程；不能证明识别准确率、说话人身份、GPU 速度或长期内存稳定性。

## 轻量回归

在仓库根目录，使用 Python 3.10–3.12。已有完整开发环境时执行 `python -m pip install -r requirements-dev.txt`。若只运行轻量测试，可在独立虚拟环境安装以下子集，不安装 FunASR、ModelScope、PyTorch 或模型权重：

```powershell
py -3.12 -m venv .venv-test
.\.venv-test\Scripts\python.exe -m pip install "pytest>=8.3" "packaging>=24" "setuptools>=68" wheel "pyyaml>=6" "pydantic>=2,<3" "loguru>=0.7" "click>=8.1" "fastapi>=0.110" "uvicorn>=0.27" "python-multipart>=0.0.9" "httpx>=0.27" "requests>=2.28" "diff-match-patch>=20241021" "tqdm>=4.66" "numpy>=1.24,<3" "soundfile>=0.12"
.\.venv-test\Scripts\python.exe -m pytest -q
```

测试从受版本控制的模板生成临时配置，不需要个人 `config.yaml`。严格模型路径正向测试使用临时空目录；模型和 LLM 的调用由 fake 替代，下载器测试只记录参数。默认测试不会下载权重或发送真实逐字稿。

关键回归入口：

| 测试 | 验证内容 |
|---|---|
| `tests/test_integrity.py` | 跨句切点、完整百分数、不完整 Markdown 消费输入与转义 |
| `tests/test_artifacts.py`、`test_pipeline.py`、`test_batch.py` | 带点文件名、原子替换、WAV 清理和输入冲突 |
| `tests/test_engine_runtime.py` | 模型身份、逐次调用选项、共享推理互斥和关闭等待 |
| `tests/test_web_access.py` | 固定配置、真实 peer、认证、密钥三态、静态文件边界 |
| `tests/test_execution.py`、`test_task_repository.py`、`test_upload_sessions.py` | 队列容量、取消竞争、条件准入、忙删除和恢复 |
| `tests/test_llm.py`、`test_web.py` | AI 截断判失败、失败保留旧稿和兼容路由 |
| `tests/test_workflow_e2e.py` | HTTP 上传到真实 pipeline、渐进段落、后处理、产物、fake AI、重启和删除 |
| `tests/test_launchers.py`、`test_installation.py` | 启动脚本替身、同解释器、打包静态资源和 revision 参数 |

合成端到端测试使用 16 kHz WAV、临时 SQLite 和 fake 模型；仅 SRT 与全部五格式各跑一遍，验证最终全文不依赖 JSON 文件。它保留真实管线后处理和文件写入，所以与直接伪造 HTTP 返回的浏览器测试互补。

## CI 范围

[tests.yml](../.github/workflows/tests.yml) 在 `windows-latest` 上配置 Python 3.10、3.11、3.12，运行完整默认 pytest；Windows 启动脚本和 junction 用例纳入同一矩阵。依赖安装使用上述轻量子集，不安装识别模型栈。Action 版本按核验时的 [checkout](https://github.com/actions/checkout) 和 [setup-python](https://github.com/actions/setup-python) 官方说明选择。

配置文件存在和本地运行成功，均不代表 GitHub 上的矩阵已通过。发布时记录实际 workflow 链接、commit、各 Python 版本结果；未触发时写“未运行远端 CI”。Linux/macOS 和真实模型环境需要单独验收。

## 浏览器交互

使用独立环境中的 Playwright，显式安装浏览器后执行：

```powershell
.\.venv-test\Scripts\python.exe -m pip install playwright
.\.venv-test\Scripts\python.exe -m playwright install chromium
.\.venv-test\Scripts\python.exe tests/browser/verify_ui.py --output review/ui-screenshots
```

已有受支持的 Playwright Chromium 时跳过下载。脚本由 Playwright 路由本地静态资源和合成响应，不启动用户的生产服务，不接触个人数据库或外部 AI。输出 `results.json` 及必要 PNG；它不是网络上传吞吐测试，真实 HTTP 受理由上节集成测试覆盖。

脚本覆盖 200 段的第 1/81/161 段翻页、全文搜索、复制完整文本，AI 阅读层焦点进入/约束/返回、Escape、全屏展开，密钥保留/替换/清空和返回项目不触发 AI，筛选清选择与批删部分失败，以及受控 XHR 的发送进度、取消先赢、受理先赢、网络未确认后恢复。

布局矩阵包括 1440×900、768×1024、390×844、320px 窄屏，700/701px 与 900/901px 断点，浅/深主题和 reduced-motion，检查页面级横向溢出、可达操作与长标题/表格。脚本分别检查 CSS 200% 重排和原生 Chromium 200% 缩放；后者使用临时扩展及独立浏览器配置调用 `tabs.setZoom`，并读取实际缩放值与视口确认，退出后清理临时配置。没有实测对比度时，不报告 WCAG 对比度合格。

2026-10-22 本地合成验收记录：96 个布局组合及上述流程通过，页面异常为 0；原生缩放读取 factor=2、外窗宽 1440、CSS 视口宽 709、devicePixelRatio=2，CSS 200% 也通过。证据在 `review/ui-screenshots/results.json` 和同目录截图。本记录不证明其他浏览器、屏幕阅读器或真实上传网络条件已验收。

## V1 真实多人样本协议

当前不宣称跨 VAD 组说话人身份归并已修复。声纹局部编号可能在不同组重用；旧“教师/学生”标签是启发式，不能作为身份真值。按以下流程建立验收证据后，再决定是否需要额外归并实现：

1. 在录音授权和使用范围明确的测试集上，保留原录音，不把逐字稿发送到未获授权的服务。按真实发言者标注时间区间、重叠区间和听不清部分；至少两人复核身份标注分歧。
2. 固定配置、模型文件校验值、代码 commit、Python/依赖版本、设备和音频预处理。样本长度必须跨过多个实际 VAD 组；保存实际组边界，不能只按任意音频切点推测。
3. 每类样本独立运行关闭/启用 diarize 两种设置；保留逐段文本、时间戳、模型局部 speaker、最终标签和人工对照，不只保存汇总数字。

| 样本 | 必须核对的问题 |
|---|---|
| 同一人在相隔多个 VAD 组再次发言 | 是否被拆成不同身份；稳定性是否依赖首次出现顺序 |
| 不同人在不同组都得到局部 `spk0` | 是否被错误合并；局部编号不能直接当作全课身份 |
| 教师与多位学生轮换、插话和短答 | 标签漂移、短答漏检、角色启发式是否误导 |
| 两人重叠发言、齐读、背景人声 | 重叠区间的漏检、错误归属和不可分离情况 |
| 静音、环境噪声、单一说话人长段 | 是否产生虚假说话人和无文本时间段 |

至少报告每个样本的身份错误清单、跨组同人拆分次数、不同人误合并次数、重叠区间单列结果。若计算 DER，先写清 collar、重叠处理、全局最优标签映射和评分工具版本；不得直接比较不同评分设置的数字。验收阈值由真实课堂用途确定并在运行前冻结。本轮没有填造通过率，也未实现新的跨组声纹归并算法。

## 真实模型、性能与资源协议

真实转写验收需覆盖中文术语、数字/百分数、静音、长句、录音末尾和多段拼接。核对字词、文本守恒、时间范围、字幕可读性及输出文件。`balanced/high` 当前同模型，不能仅凭档位名称宣称更准；`fast` 无精细时间戳，不用其粗字幕衡量精确对齐。

CPU/GPU 各在实际设备测量，记录硬件、驱动、torch/torchaudio、模型版本和全部配置。分开报告冷启动加载、热运行推理、后处理和总耗时，给出音频实际时长；`RTF = 处理秒数 / 音频秒数`，明确分子是否包含加载与转换。相同样本重复运行并报告范围，不将 CPU 推算为 GPU 结果。

长课吞吐和内存测试至少包括：连续多堂真实录音、1 个执行任务加 8 个等待的队列、队满拒绝、AI 与转写交接、人工关闭和重启。按固定间隔记录进程 RSS、CPU、GPU 分配/保留内存（有 GPU 时）、队列长度、磁盘占用及失败率，区分缓存稳定驻留和持续增长。看门狗标记失败后继续观察实际执行者，不能把状态变化记为内存已释放或推理已停止。

测量结果应附原始日志、采样间隔和异常样本。缺少实际运行时，只写协议与“待验收”，不提供未经测量的分钟数、RTF 或最大可承载课堂数。
