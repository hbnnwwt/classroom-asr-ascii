# 项目可靠性与 UI/UX 改进实施计划

日期：2026-10-22（Asia/Shanghai）
状态：T0–T10 已实施并验收；保存在独立工作分支 `codex/project-improvements`。

> 给执行代理：本计划是新的代码实施计划，前一份项目分析计划不等于本计划已经获批。确认后按任务逐项执行、勾选并记录证据。并行任务必须划定文件所有权；共享管线、Web 编排与样式的任务顺序执行。

**目标：**修复已确认的结果错误和资源边界问题，并让非技术教师能顺畅完成上传、等待、阅读全文和下载。

**架构：**保留 Python 单体、FastAPI、SQLite、原生 HTML/CSS/JS 和离线资源。围绕模型、任务、产物三类真实所有权提取小模块；CLI、HTTP 路径、旧任务和既有输出结构继续兼容。

**技术栈：**Python 3.10–3.12、Pydantic v2、FunASR、ffmpeg、FastAPI、SQLite、pytest、原生前端；浏览器验收使用合成任务。

**需求依据：**

- [项目改进报告](../review/project-improvement-2026-10-22.md)
- [UI/UX 补充分析](../review/uiux-improvement-2026-10-22.md)
- [项目设计约束](../.impeccable.md)
- 用户要求：“ok，请你根据改进报告和uiux补充分析，开始改进”。

## 一、核心判断与已选方案

值得做。最危险的关系是“任务记录、后台线程、缓存模型各自改变状态”。先让测试独立，再修确定性错误，随后统一这些对象的所有者，最后接上教师能理解的界面。

采用现有技术栈内的小步改造：有界线程执行器、数据库条件更新、冻结的模型加载配置、原子产物发布、普通分页阅读。当前需求不需要换前端框架或增加分布式调度服务。

数据关系固定为：

| 数据 | 所有者与约束 |
|---|---|
| `AppConfig` | 配置加载器拥有；每次管线使用快照，不修改调用者对象 |
| `EngineLoadSpec` | 冻结、可比较，完整描述实际模型加载依赖 |
| `TranscriptionOptions` | 每次调用携带语言、ITN、热词和批次选项，不留在共享引擎中 |
| SQLite 任务 | repository 负责迁移、原子准入和恢复 |
| 执行者 | execution 管理器负责排队、登记、交接、关闭和删除裁决 |
| 输出文件 | artifacts 负责名称和完整发布；元数据登记真实文件名 |
| 前端 | 展示服务端事实；上传取消、任务受理与推理运行分别表达 |

## 二、全局约束与范围

- 当前基线为 `231baa6`，主工作区在 `main`；已有未跟踪的分析计划和两份报告要保留。
- 获批后使用应用管理的独立 worktree，从当前基线开始，在 `codex/` 工作分支实施；仅复制本次计划和报告，不复制个人配置、模型、真实 SQLite 或媒体数据。
- 默认测试仅使用模板、临时目录、合成 WAV、fake 引擎与 fake LLM；不得读取个人 API 密钥或将真实逐字稿发送云端。
- 默认保持本机部署；局域网增加最低必要的访问控制，启动器不终止其他进程。
- 保留 CLI 的 `--config`、质量枚举、输出格式与模块启动方式。HTTP 任意配置路径会明确收紧，见下方迁移契约。
- 保留浅深主题、系统字体、本地 SVG、复制全文、暂停滚动和减少动画支持。
- 所有 bug 回归先复现失败再修复；低风险纯文案不增加镜像实现的测试。
- 每批完成后跑关联测试；整体结束跑默认全套、浏览器关键流程、独立代码审查并回填本文件。

**本轮不宣称完成的能力：**跨 VAD 组真实说话人归并、强制终止卡死的 native 推理、GPU 性能提升、自动分块重写整堂课、音频回听编辑器。V1 本轮交付实验性说明和真实样本验收协议；只有真实样本与额外实现都成立时才能宣称身份问题已修复。

## 三、接口与用户行为约定

### 3.1 服务端配置、访问控制与密钥

- Web 通过 `--config` 或 `ASR_WEB_CONFIG` 固定配置绝对路径，默认解析当前目录 `config.yaml`；测试注入临时 runtime。
- 原 HTTP `config` 参数只接受省略、`default`、`config.yaml`，都映射至固定配置；其他路径为 400，错误码 `config_not_allowed`。CLI 自选配置能力不变。
- `GET /api/llm/settings` 保留公开字段，`api_key` 固定为 `null`，增加 `api_key_set: bool`、固定掩码 `api_key_masked: str`。
- 保存与测试连接共用密钥语义：省略或 `null` 保留，非空字符串替换，空串明确清空。前端不再回填真实密钥；连接测试不落盘。
- 无访问密码时仅真实 loopback peer 可用；配置 `ASR_WEB_PASSWORD` 后，页面、API、静态文件和下载统一使用 HTTP Basic，用户名固定 `classroom`。非本机 bind 且无密码拒绝启动；直接 uvicorn 入口仍有 peer 检查。
- 不根据 `Host` 或未经验证的转发头判定本机，不给 `testclient` 写生产特例。测试使用明确的 peer scope。缺少/错误认证为 401，带 Basic challenge；无密码远端请求为 403。
- 对浏览器发起的状态变更请求检查同源 Origin；认证与来源校验不要求正常本地脚本伪造浏览器请求头。实现依据使用 [FastAPI Basic 官方文档](https://fastapi.tiangolo.com/advanced/security/http-basic-auth/) 和 [FastAPI 静态文件文档](https://fastapi.tiangolo.com/tutorial/static-files/)。

### 3.2 上传、取消与受理

| 接口 | 返回及含义 |
|---|---|
| 新增 `POST /api/uploads` | 201：`{upload_id,status:"open",task_id:null}` |
| 新增 `GET /api/uploads/{id}` | 200：返回 `open/cancelled/accepted` 和对应 task_id；未知为 404 |
| 原 `/api/projects`、`/upload` | 可选 `X-Upload-Id`；原成功 200 和原字段保持；旧客户端不传凭证仍可创建 |
| 新增 `POST /api/uploads/{id}/cancel` | 取消先赢：200、`status:"cancelled"`，保证不创建任务；重复取消仍 200 |
| 受理后再取消 | 409、`code:"upload_already_accepted"`、准确 task_id，任务继续处理 |

凭证使用轻量 SQLite 表；“open → accepted＋插入任务”与“open → cancelled”在同一数据库中原子竞争。上传接收、probe、排队失败都清理本次已写文件，包括尚未加入 saved 列表的当前文件。队列没有容量时，不留下任务或 accepted 凭证的半更新。

前端 XHR 仅负责真实发送进度；100% 后显示“正在确认接收…”。取消时中止发送并请求服务端裁决；服务端确认后才显示“已取消上传”。若已受理，显示“任务已接收，继续处理”并提供入口；网络不明时显示“取消结果暂未确认”，用凭证查询恢复。

### 3.3 任务、AI 与产物

- 保留 `pending/running/done/failed`。默认 1 个业务 worker、最多 8 个等待任务；队满为 503、`code:"queue_full"` 并带 `Retry-After`。
- 自动/手动 AI 都走统一 `done → pending(stage=ai) → running` 条件准入。成功仍为 202 `{status:"started"}`；重复/忙为 409。队满不得改变原状态、已发布稿或错误记录。
- `/status` 和项目列表/详情增加 `execution_active`、`can_delete`、`transcription_status`、`ai_status`、`available_formats`，旧字段保留。
- pending、running 或执行者仍活跃的任务，删除一律 409 `task_busy`。终态且执行者已退出才可删；本轮不把“删除”实现成强杀推理。
- 批量删除保留 `{deleted:[id],failed:[id]}`，增加逐项 `failure_details`。UI 列出不可删除任务，服务端仍处理并发竞态。
- 新基础产物登记在 `meta.output_files`；保留 `formats/part_files/ai_files`。有登记时按登记读取；仅没有登记的旧任务使用受限旧查找。任何目标都必须留在该任务输出目录内。
- AI 重跑保留最后完整 `ai_files`；本次尝试写 `meta.ai_attempts[fmt]`，状态 `queued/running/succeeded/failed`。完整发布成功后才替换登记，失败显示“本次生成失败，仍可查看上次版本”。
- `chat() -> str` 保持；`length/content_filter` 或非字符串/空正文抛 `LLMError`。兼容未返回 `finish_reason` 的服务，但不把缺失字段描述成已验证完整；记录可用的结束原因与长度，不自动增加调用。
- `ended_at` 继续表示转写结束时间。仅 AI 中断的恢复不抹掉已完成转写，也不自动再次调用 LLM。

### 3.4 前端具体方案

- 普通质量入口显示 balanced“字幕转写（推荐）”、fast“快速文字（无精细时间戳）”；`high` 继续接受历史值，当前不暗示更准确。
- 新任务记录请求的 `meta.quality`；历史任务没有记录时不猜测。fast 的字幕下载标明时间信息较粗，不把质量档名称当作精度实测结果。
- 说话人选项写“区分不同说话人（实验性）”，说明“按声音区分发言者，结果需核对”，不承诺识别真实师生角色。
- 搜索或筛选变化立即清空选择；无搜索结果显示“没有符合条件的项目”和“清除搜索与筛选”。删除确认列名称及文件会一并删除的范围。
- 主任务呈现“准备录音 / 生成文字 / 整理结果”，工程步骤放可展开详情。AI 独立显示；转写完成后下载始终可见。
- 运行中仅显示最近 80 段；完成态默认每页 80 段，支持全文段落搜索和前后页。复制仍使用完整数据。
- 首层下载为“下载字幕（SRT）”“下载文字稿（TXT）”；“更多格式”展示实际可用格式。
- AI 短稿保留弹层，补焦点进入/约束/返回与背景不可交互；长稿用同一阅读层的全屏开关扩展，不新增独立阅读产品。保留已有 Escape 和关闭按钮。
- 设置显示“服务商 / 模型名称 / 访问密钥”，“服务地址 / 超时 / 输出上限”置于原生高级设置。保存后提供“返回《项目名》”，导航不自动触发 AI。
- 云端 AI 开关附近说明逐字稿将发送至所配置服务；不得把可选云端 AI 说成全程离线。
- 窄屏保留项目名；长文正文以 16px 为起点验证。真实录音总长取 `meta.duration_s`，不用最后一个句子的结束时间冒充总时长。

## 四、实施批次与文件责任

| 批次 | 任务 | 可交付结果 |
|---|---|---|
| A | T0、T1、T2；T9 中启动器强杀修复优先 | 默认测试安全可跑，丢字/错误数字/循环/覆盖被修复 |
| B | T3、T4、T5、T6 | 文件与配置受限，模型与任务有所有者，AI 完整发布 |
| C | T7、T8 | 教师可理解上传与取消，安全管理项目，完整阅读与下载 |
| D | T9 其余、T10 | 安装与版本契约一致，CI/浏览器验收和最终审查完成 |

核心 Python 由 Python 专家负责；Web 与新接口由 FastAPI 专家依据已定契约负责；前端由 frontend-developer 负责，CSS 改动统一交 T8 所有者；安装由部署专家负责，文档由 documentation-specialist 负责，最终由 code-reviewer 独立复核。多人同时工作不得撤销彼此改动。

## 五、逐项实施与验收

执行命令中的解释器先探测项目虚拟环境；若尚未建立，可使用已确认存在的 `C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe`。不得调用 Microsoft Store 的空壳 `python.exe`。以下简写 `python` 指已验证的该解释器。

### T0：建立不依赖个人环境的测试基线（R10 前半）

文件：新增 `tests/conftest.py`，修改 `tests/test_config.py`、`test_batch.py`、`test_funasr_engine.py`、`test_postprocess.py`、`test_web.py`。

接口：`template_config` 夹具返回从出厂模板加载的 `AppConfig`；`web_runtime` 夹具提供临时配置、SQLite、上传/输出目录和 fake 执行者，后续替代直接引用实际配置。

- [x] 清点并替换默认测试对个人 `config.yaml` 的读取；保留真实的缺失配置报错测试。
- [x] 严格模型路径正向用例用临时空目录，负向用例继续断言缺失路径报错；不关闭校验来消灭失败。
- [x] 原有 14 个模块在无个人配置、无模型工作区：279 passed，0 failed，0 errors（16.01s）；新增回归随各任务验收。

### T1：修复文字、数值与 Markdown 正确性（R2、R4、R5）

文件：`asr/engines/funasr_engine.py`、`asr/postprocess.py`、`asr/mdrender.py`；对应三份既有测试。

接口：既有 `_split_by_char_ts()`、`apply_itn()`、`render_markdown()` 签名保持。

- [x] 先加入并观察失败的跨句切点回归，使用以下确切输入与断言：

```python
text = "甲，乙。" + "一二三四五六七八九十" * 2 + "。"
ts = [[i * 500, i * 500 + 400] for i in range(22)]
clean = validate_timestamps(_split_by_char_ts(text, ts, 15), 15)
assert "".join(s.text for s in clean) == text
assert all(s.start <= s.end for s in clean)
assert all(a.end <= b.start for a, b in zip(clean, clean[1:]))
```

- [x] 强标点提交后重置全部段内切点；保持秒制结果与既有文本结构。
- [x] 百分数仅转换完整支持的数词：`百分之九十九 -> 99%`；`百分之一百二十`、`百分之三点五`、`百分之负三` 本轮保持原文。年份 `二〇二三年 -> 2023年` 继续成立。
- [x] 为 `| orphan` 和不完整表格加入有执行上限保护的失败复现；所有解析分支消费输入，HTML 转义继续成立。
- [x] 正确性回归先 10 failed / 6 passed；上述模块与新增回归合并验收 142 passed（见执行账本）。

### T2：统一产物命名与路径登记（R3、R12 产物部分）

文件：新增 `asr/artifacts.py`、`tests/test_pipeline.py`、`tests/test_artifacts.py`；修改 `asr/srt.py`、`pipeline.py`、`batch.py`、Web 产物定位及相关测试。

接口：`artifact_path(base: Path, suffix: str) -> Path` 追加扩展名；`Result` 增加有默认值的最终 `segments` 字段，既有构造兼容，Web 不依赖用户是否选择 JSON 文件才能保存全文。

- [x] 先加入命名与覆盖回归：

```python
assert artifact_path(Path("lesson.1"), ".srt").name == "lesson.1.srt"
assert artifact_path(Path("lesson.2"), ".replace.log").name == "lesson.2.replace.log"
```

- [x] 五件套、替换日志、保留 WAV 统一完整 stem；不同输入 WAV 均保留，工作临时目录始终清理。
- [x] batch 在启动任何识别前检查同 stem/同目标路径冲突，明确报错并列输入，不自动猜测重命名。
- [x] 新任务写 `meta.output_files`；下载优先登记并校验所属目录，保留无登记旧任务和旧分段/MP4 产物兼容。
- [x] 验证仅请求 SRT 时最终 transcript 仍可供前端阅读；重命名项目不改变文件定位。
- [x] 运行 `python -m pytest -q tests/test_artifacts.py tests/test_srt.py tests/test_pipeline.py tests/test_batch.py tests/test_web.py`，验收为 0 failed。

### T3：限定 Web 文件、配置与访问范围（R1）

文件：新增 `asr/web/runtime.py`、`asr/web/access.py`、`tests/test_web_access.py`；修改 `asr/web/__init__.py`、启动入口和设置相关测试。

接口：runtime 暴露固定 `config_path`；Web 请求按第 3 节契约校验；现有 URL 保留。

- [x] 先用合成临时文件写边界回归：目录外静态文件不可返回；任意 HTTP 配置路径被拒绝且目标不变。
- [x] 静态目录采用受限服务或规范路径所属检查，继续保留 no-cache 行为。
- [x] 密钥 GET 不含明文；保存/测试按 null、非空、空串三态处理；所有错误不得回显密钥。
- [x] 加入 loopback/远端/认证回归：伪造 Host、转发头无效；下载与页面同样受保护。
- [x] 运行 `python -m pytest -q tests/test_web_access.py tests/test_web.py tests/test_config.py`，验收为 0 failed。

### T4：冻结模型加载身份，显式传任务选项（R8、R12 配置部分）

文件：新增 `asr/engines/runtime.py`、`tests/test_engine_runtime.py`；修改 `asr/engines/base.py`、`funasr_engine.py`、禁用引擎接口桩、`pipeline.py` 及相应测试。

接口：
- `EngineLoadSpec` 为 frozen dataclass，包含实际 ASR/VAD/PUNC/SPK 规范路径、device、加载使用的 CPU 参数；未启用 SPK 时为 None。
- `TranscriptionOptions` 为每次调用的冻结选项，包含 language、use_itn、hotword_enabled、batch_size_s、说话人输出选项。
- 基类 `transcribe()` 正式声明选项与进度/段落回调，旧参数调用方式保留适配。

- [x] 先写语言变化、模型路径变化、并发创建与关闭回归；假模型记录实际收到的参数。
- [x] 模型选择逻辑集中一处，pipeline 不再用私有 `_quality_override` 修改配置；`run()` 不改变调用者 AppConfig。
- [x] 正式回调类型为 `Callable[[float, str], None]` 与 `Callable[[list[Segment]], None]`；`transcribe(wav, hotwords=None, *, options=None, on_progress=None, on_segments=None)` 让旧位置参数继续可用，任务选项显式传入。
- [x] 缓存构建与实例完整转写/释放具有明确互斥；两次并发创建只得到一个实例，同实例最大活跃推理数为 1。
- [x] 非 ASCII 模型目录别名按规范路径摘要区分，避免不同真实模型复用固定旧 junction；不递归删除旧模型链接。
- [x] 先 7 failed；上述四个模块最终 55 passed（2.25s），包括真实参数转发、并发双调用和关闭等待。

### T5：统一任务执行与上传取消裁决（R7、R12 仓库/执行部分）

文件：新增 `asr/web/repository.py`、`asr/web/execution.py`、`tests/test_task_repository.py`、`tests/test_execution.py`、`tests/test_upload_sessions.py`；修改 Web 编排与测试。

接口：repository 提供条件准入和上传凭证事务；execution 管理器默认 1 worker、8 个等待位置，统一接收普通、多段、自动 AI、手动 AI 任务。

- [x] 先写并发准入/取消竞争测试：两次 AI 请求恰好一个 202；取消与受理恰好一个获胜，响应丢失可查到准确 task_id。
- [x] 用有界容量预留与事务协调确保队满 503 无残留；拒绝重复凭证生成第二个任务。
- [x] 业务执行不再各自创建无主 daemon 线程；任务携带本次执行身份，超时后的晚到回调不得复活终态。
- [x] 重启时转写遗留任务标记中断；仅 AI 中断保住转写和旧稿，记录失败，恢复不自动调用 LLM。
- [x] pending/running/执行者仍活跃的删除为 409；批删逐项说明。转写到自动 AI 的所有权交接也在管理器内完成。
- [x] 关闭先停止准入，再协调在途任务，执行者未退出不并发释放模型。看门狗文案明确“标记失败”，不声称底层已停止。
- [x] 验证多段第二文件失败、probe 期间取消、当前半截文件尚未登记等清理场景。
- [x] 运行 `python -m pytest -q tests/test_task_repository.py tests/test_execution.py tests/test_upload_sessions.py tests/test_web.py`，验收为 0 failed。

### T6：AI 完整性校验与原子发布（R9）

文件：`asr/llm.py`、`asr/artifacts.py`、Web AI 执行代码；`tests/test_llm.py`、`tests/test_artifacts.py`、Web 测试。

接口：新增 `atomic_write_text(path: Path, text: str) -> None`，同目录临时文件写完并关闭后原子替换，失败清理临时文件；发布与登记按第 3.3 节规则执行。

- [x] mock 非空正文配 `finish_reason="length"`，先断言会抛 `LLMError`；覆盖过滤、异常类型和空正文。
- [x] 写入中断/替换失败时，断言旧完整文件逐字节不变，未发布半成品不可下载。
- [x] 新尝试单独记账；成功才替换 `ai_files`，单件失败不拖累其他件或转写成果。
- [x] 记录输入字符数、输出字符数和可用结束原因；不引入自动续写、重复付费或未经验证的分块合并。
- [x] 运行 `python -m pytest -q tests/test_llm.py tests/test_artifacts.py tests/test_web.py`，验收为 0 failed。

### T7：上传、列表与质量选择交互（UI 第一批）

文件：`asr/web/static/index.html`、`list.js`；浏览器测试入口由 T10 固化。CSS 需求交 T8 所有者合并。

- [x] 接入上传凭证、XHR 真实进度与取消确认，完整实现第 3.2 节全部状态文案。
- [x] 搜索/筛选立即清空选择；首次空态和搜索空态分开；批删前展示名称和不可删除项。
- [x] 质量与说话人文案使用第 3.4 节确切含义；仅显示实际可用的下载和删除动作。
- [x] 浏览器验证慢上传、取消先赢、受理先赢、网络未确认、筛选后选择、批删部分失败；预期不显示虚假的取消成功、不删除隐藏选择。

### T8：任务阅读、设置与可访问性（UI 第二批）

文件：`task.html`、`task.js`、`settings.html`、`settings.js`、`style.css`、必要的本地小型脚本模块；保留现有测试锚点。

- [x] 将主任务阶段、AI 阶段和下载可用性分开呈现；保留工程详情与真实进度。
- [x] 实现运行态最近 80 段、完成态每页 80 段与全文段落搜索；翻页和搜索不截断复制全文数据。
- [x] 首层 SRT/TXT 与更多格式分组；长 AI 稿可选全屏，失败重生明确上次版本。
- [x] 设置中文/高级字段折叠/来源返回；密钥未编辑发送 null，显式清空发送空串；保存和导航不自动生成 AI。
- [x] 阅读层打开时进入焦点、Tab 保持在层内、Escape 关闭并返回原按钮，背景不可交互；动态消息提供适当状态提示。
- [x] 窄屏保留项目名，长文 16px 起点，浅深主题与减少动画不回退。
- [x] 真实浏览器验证 1440×900、768×1024、390×844、桌面 200% 缩放；使用至少 200 段和长表格/长标题合成稿，验证能读到第 1/81/161 段及搜索命中、键盘完整流程。保存必要截图。
- [x] 补充 320px 窄屏、700/701px 与 900/901px 断点边界、浅/深主题和 reduced-motion，核对布局无页面级溢出且核心操作可达；未经浏览器测量不报告对比度合格。

### T9：安全启动、统一解释器与版本契约（R6、R11）

文件：`setup.bat`、`run_web.bat`、`pyproject.toml`、`requirements.txt`、`.gitignore`、`scripts/download_models.py`；新增 `requirements-dev.txt`、`constraints/cpu-py312.txt`、模型 revision 清单、`tests/test_launchers.py`、`tests/test_installation.py`。

- [x] **批次 A 优先完成：**删除所有按端口无条件 taskkill 的逻辑，端口占用给出明确提示；用命令替身检查，不运行真实终止命令。
- [x] 安装创建项目 `.venv`，启动使用同一个解释器，缺少时给安装指引；模型下载脚本保留显式执行，不在回归测试自动下载。
- [x] `requires-python` 统一为 `>=3.10,<3.13`，3.12 为本机已验证的单测环境；真实模型支持范围与轻量测试结果分别说明。
- [x] 运行依赖由 `requirements.txt` 作为单一来源供 pyproject 读取，测试依赖拆出；显式打包本地静态资源。
- [x] CPU 约束记录经过核验的版本，不把整个全局环境一股脑冻结。模型 revision 从可核验公开元数据取得，下载显式传入 revision；不可核验者明确记录边界，不编造哈希或宣称已复现。
- [x] 验证脚本参数转发、`.venv` 选择、模型下载 revision 参数、端口占用；运行 `python -m pytest -q tests/test_launchers.py tests/test_installation.py`，验收为 0 failed。

### T10：端到端、文档与独立复核（R10 后半、R11、V1）

文件：新增受版本控制的 `tests/test_workflow_e2e.py`、`tests/browser/verify_ui.py`、`.github/workflows/tests.yml`、`docs/validation.md`、`docs/web-contract.md`；修改 README、DEVNOTES 和必要设计记录。配置模板保留现有兼容默认值。

- [x] 合成 WAV＋临时 SQLite＋fake 引擎/LLM 串起上传、队列、渐进转写、全部输出、AI 重生失败保旧稿、重启和删除；不依赖 TEMP 中丢失的旧 E2E 脚本。
- [x] CI 默认只跑轻量单测/集成；Python 3.10、3.11、3.12 矩阵配置明确，未触发的远端 CI 不能报告为已通过。
- [x] 文档区分双击 8765 和模块默认 8000；说明访问密码、固定 Web 配置、密钥三态、取消边界、质量能力和已知限制。
- [x] V1 建立真实多人样本协议：跨组同一人、不同人局部同号、轮换与重叠发言；GPU、吞吐、长期内存也仅记录真实测量结果。
- [x] 使用 `python -m pytest -q` 跑默认全套，运行 T7/T8 浏览器流程，检查 JS 语法、工作区差异与旧任务迁移。
- [x] 由独立 code-reviewer 复核，安全与可访问性问题交相应专家确认；修复发现后重跑关联检查。
- [x] 更新本计划勾选项和下方 Review，报告实际修改、测试结果、兼容迁移和仍待真实数据验收的项目。

## 六、五个必须覆盖的交互边界

1. 上传取消与接受竞争、响应丢失：T5/T7 覆盖，不能出现“界面已取消但任务悄悄继续”。
2. 带点名称、同 stem 输入、保留 WAV：T2 覆盖，不能覆盖其他输入成果。
3. AI 截断或写入失败：T6/T8 覆盖，旧完整稿保持可读，本次失败可见。
4. 超时但执行者仍活跃、自动 AI 交接、删除与重启：T5 覆盖，不能删除活跃者使用的数据或复活旧状态。
5. 配置变化、并发共享模型、Windows 中文路径别名：T4 覆盖，不能复用错误模型或语言。

## 七、执行与兼容迁移

- T0 先于其他测试；T1 与启动器安全修复可独立并行。T2/T4 都修改 pipeline，顺序进行。
- T3 → T5 → T6 的 Web/状态变更顺序集成；接口稳定后 T7 和 T8 可按独立文件并行，样式由一个所有者统一修改。
- 设置旧客户端不再获取真实 key，空白密码保存改为 null；HTTP 任意配置路径明确拒绝。其余旧 URL 和成功响应字段保留。
- 所有旧 SQLite 迁移只加列/表，不重建丢数据；已有已发布产物不批量重命名；新命名依靠登记定位，旧命名兼容读取。
- 批次在工作分支保留可回滚的本地提交；推送、合并、部署不属于本计划验收。

## 八、开工前确认与 Review

- [x] 两份分析报告、项目设计约束与相关代码已核对。
- [x] 技术负责人、API 架构师、UI/UX 代理已给出明确方案与兼容边界。
- [x] 已自查报告覆盖、任务依赖和验收边界：R1–R12 均有任务映射，V1 保留为真实样本验收，未把未知能力写成已实现。
- [x] 用户确认本实施计划（2026-10-22：开始实施）。
- [x] 建立工作区、记录新的执行账本与基线（codex/project-improvements，BASE 231baa6）。
- [x] 完成 T0–T10 并逐项验收。
- [x] 独立代码审查完成，Review 记录完整。

### Review（2026-10-22）

已完成全部 T0–T10，详细映射与证据见 [实施验收记录](../review/implementation-2026-10-22.md)。主要变更为结果正确性与产物完整命名、冻结模型身份与逐次选项、统一任务所有权与上传取消裁决、配置/认证边界、AI 完整发布及失败保旧稿，以及原生前端的上传、分页阅读、状态、设置和移动端交互。

最终本机 Python 3.12.10 默认全套 **367 passed，2 warnings（33.24s）**；禁用重模型导入后仍 **367 passed（34.43s）**。浏览器 96 组布局、200 段阅读/搜索/复制、受控 XHR 取消竞争、弹层焦点和设置三态通过；原生 200% 缩放实测 factor=2。四个 JS 语法检查通过。独立 code-reviewer 运行 144 项相关用例、安全专项运行 63 项相关用例；无剩余阻断项。可访问性补查确认 320px 三页无横溢、正文 16px、弹层反向 Tab/背景拦截；AI/转写错误增加 alert，重复相同内容不重新播报。

审查发现的 DNS 重绑定、替换日志登记别名和最终空稿残留均先复现再修复。HTTP 任意配置路径现拒绝；GET 不再回显密钥；无密码时同时要求真实回环连接与本机 Host。旧 Result 构造、历史文件、HTTP 成功字段和 CLI 入口保留兼容。

真实模型、GPU、跨组多人身份、长期内存、长课吞吐与远端 CI 未运行；CPU 候选约束和可变 master 不等于完全可复现环境。真实数据验收按 [协议](../docs/validation.md) 执行。两条宿主依赖警告保留。P3 后续项：任务页标题层级、收起上传面板后的焦点返回；未宣称屏幕阅读器或 WCAG 全面合规。

变更仅在应用管理的独立工作区和 `codex/project-improvements` 本地提交；未复制主目录个人配置、模型、真实数据库或媒体。推送、合并、部署未执行。回滚以工作分支提交为单位进行，保留基线 `231baa6`，无需改动用户已有运行数据。
