# AI 反馈环节设计（教学点评 + 逐字稿优化）

日期：2026-10-21
状态：待评审

## 1. 背景与目标

课堂转写的产物目前到"可读的逐字稿 + 字幕"为止。本次为其增加 AI 反馈环节：

1. **分析**：指出可改进之处——既评课堂教学质量（给教师的反馈），也评稿件本身（疑似识别错误、可读性问题）。
2. **生成**：产出优化版逐字稿（去口头禅、修标点分段、提炼要点，不改原意）。

已确认的三项决策：

| 决策点 | 结论 |
|---|---|
| 分析对象 | 教学质量 + 稿件质量，两者都要（报告分两部分） |
| LLM 来源 | 可配置：云 API（GLM/DeepSeek 等）与本地（Ollama）都支持，走 OpenAI 兼容端点 |
| 触发方式 | 上传时开关自动跑 + 转写完成后任务页手动补跑，两阶段都要 |

## 2. 架构总览

```
segments(JSON, 含 spk)
   │
   ├─ asr/analyze.py ──→ 统计事实 dict          （纯 Python，零 LLM）
   │                        │
   │                        ▼
   ├─ asr/llm.py ──────→ chat(messages) → str   （OpenAI 兼容 /chat/completions）
   │                        │
   │        ┌───────────────┴───────────────┐
   │        ▼                               ▼
   │   feedback.md                    transcript_polished.md
   │   （点评报告：教学+稿件）           （校订稿 + 要点）
   │
   └─ outputs/{task_id}/ 下与现有产物并列，自动进下载列表
```

核心分工原则：**统计出事实，LLM 管解读**。语速、口头禅频次、提问次数等定量指标是
segments 的确定性计算，不交给 LLM（它数数必幻觉）；这些事实作为证据写进点评 prompt。

一节课 40~45 分钟 ≈ 8000~15000 字 ≈ 2 万 token 以内，**单次整篇喂入，不分块**。

## 3. 配置层

`config.yaml` 新增可选节（缺失即整个功能静默隐藏）：

```yaml
llm:
  base_url: https://open.bigmodel.cn/api/paas/v4   # Ollama: http://localhost:11434/v1
  api_key: ""                                       # 本地模型留空
  model: glm-4-flash
  timeout: 300        # 秒，单次请求
  max_tokens: 16384   # 单次生成上限
```

- 云/本地不分支：只是 `base_url` + `model` 的取值不同。
- `api_key` 为空时不发送 `Authorization` 头（本地端点不需要）。
- `requirements.txt` Web UI 节补 `requests>=2.28`（当前为传递依赖，转为显式声明）。

## 4. LLM 客户端 — `asr/llm.py`（新文件）

```python
class LLMError(Exception): ...

def llm_enabled(cfg) -> bool          # llm 节存在且 base_url、model 均非空
def chat(messages, cfg, *, temperature=0.3, max_tokens=None) -> str
```

- `chat`：`POST {base_url}/chat/completions`，body `{"model", "messages", "temperature",
  "max_tokens"}`；解析 `choices[0].message.content`。
- 非 200 或响应结构异常 → `LLMError`（消息含 HTTP 状态与响应体前 200 字）。
- 请求超时/网络错误 → 包装为 `LLMError`。
- 用裸 `requests`，不引入 openai SDK。

## 5. 统计层 — `asr/analyze.py`（新文件）

```python
def compute_stats(segments: list[dict]) -> dict
```

输入即现有 segments JSON 的 `segments` 数组（`start/end/text/spk`）。输出：

| 字段 | 定义 |
|---|---|
| `duration_sec` | `max(end) - min(start)` |
| `chars` | 全部 text 去空白后的字符总数 |
| `speed_cpm` | 整体语速 = chars / (有效说话秒数 / 60)；有效说话秒数 = Σ(end−start) |
| `filler_top` | 口头禅 TOP5：内置词表子串计数，`[(词, 次数), ...]` 按次数降序 |
| `questions` | 提问次数：按 `[。！？!?]` 切句，句尾为 `？/?` 或句末字为 `吗/呢/吧` 的句子数；有 `spk` 时按说话人分桶，否则为总数 |
| `speakers` | 有 `spk` 时：`[{name, duration_sec, pct, chars, speed_cpm}]` 按时长降序；无 `spk` 时为空列表 |

口头禅词表（内置常量，不进配置，保守起见不含单字）：
`那么、然后、就是、这个、那个、其实、对吧、好吧、等于说、就是说、对不对、我们来看`

子串计数会把正常用法也计入（如"这个人"），**这是有意的**：统计值是给 LLM 的证据线索而非结论，量级对了就有用，精确切分是过度设计。

## 6. 生成层 — `asr/feedback.py`（新文件）

```python
def build_feedback_messages(transcript: str, stats: dict) -> list[dict]  # system + user
def build_polish_messages(transcript: str) -> list[dict]
```

**两次独立调用，两个产物**。不合并为一次：两个产物合计输出可超 1.5 万 token，顶着多数模型的输出上限，截断风险真实；分开各自 5~8 千，稳，且独立重试互不连坐。

### feedback prompt 要点（逐字稿在实现时定稿，以下为约束）

system 角色设定：资深教学评课专家兼文字编辑。输入 = 带说话人标签的逐字稿 + 统计事实
（JSON 原样嵌入）。输出 Markdown，结构固定：

```markdown
# AI 课堂反馈

## 一、教学反馈
### 讲解结构 / 节奏 / 师生互动 / 语言表达    ← 四个小节
每条意见必须带依据，二选一：
- 依据：〈原文引用 ≤50 字〉
- 依据：〈统计事实，如"口头禅'就是'出现 47 次"〉
先具体优点，后改进建议，建议合计 ≤6 条。

## 二、稿件反馈
- 疑似识别错误：列出原句与猜测正字（同音字、专有名词等）
- 可读性问题：长句无标点、指代不明等

## 三、课堂数据
统计事实以表格呈现（语速/提问/口头禅/说话人占比）。
```

硬约束写进 prompt：不得编造原文不存在的内容；统计数字只能引用给定事实，不得自行计算。

### polish prompt 要点

- 校订稿：删除口头禅与无意义重复、修标点、按语义分段；**不改原意、不增删信息、
  不做书面化改写**（保留口语自然度）——这是教师场景的忠实度红线。
- 文末追加 `## 本课要点`：3~6 条 bullet。

### 产物文件

写入 `outputs/{task_id}/`，与现有产物并列：

- `{task_id}_{name}.feedback.md`
- `{task_id}_{name}.polished.md`

沿用现有命名前缀规则，现有下载列表 glob 自动带出，**零新下载端点**。task meta 登记
`ai_files: {"feedback": "<文件名>", "polished": "<文件名>"}`。

## 7. Pipeline 集成与失败语义

**铁律：AI 失败不拖垮转写成果。** 转写是主产品，AI 是增值环节。

- 上传表单新增字段 `ai_feedback`（`"true"/"false"`，缺省 false）→ 存入 task meta。
- 带此开关的任务：转写 stage 照旧，完成后进入新 stage `ai`：
  - 进度：统计完成 `0.1` → 点评写盘 `0.55` → 优化稿写盘 `1.0`
  - stage 显示名："AI 反馈"
- `_run_ai_stage(task_id)`（web 层函数，两条触发路径共用）：
  1. 从 `outputs/{task_id}/*.json` 读 segments（**从文件重建，不依赖内存**——手动补跑
     是新线程/新调用，没有上一次 pipeline 的内存态）；喂给 LLM 的正文也由 segments
     重建：有 `spk` 时每行 `"{spk}: {text}"`，无则裸 `text`。单一事实源，不依赖 .txt
     产物的排版
  2. `compute_stats` → `build_feedback_messages` → `chat` → 写盘
  3. `build_polish_messages` → `chat` → 写盘
  4. 成功：meta 写 `ai_files`，status 保持/回到 `done`
  5. 任何异常（`LLMError` 及其他）：捕获，meta 写 `ai_error: str(e)`，
     **status 置 `done`**（转写产物完好），stage 停在 `ai`，不抛出
- 自动路径：`_run_pipeline` 成功后（status==done）且 meta.ai_feedback → 调
  `_run_ai_stage`（同线程顺序执行）。
- 手动路径：`POST /api/tasks/{task_id}/feedback`（**对任何 done 任务开放，含上传时
  没开开关的旧任务**——手动补跑是通用出口，不只服务失败重试）
  - 前置校验：任务存在（404）；`status == "running"`（409，防并发重入）；
    `llm_enabled` 为 false（409 "LLM 未配置"）
  - 触发：status→`running`、stage→`ai`、清空 `meta.ai_error`，起后台线程跑
    `_run_ai_stage`；已跑过的覆盖旧产物重跑
  - 返回 `202 {"status": "started"}`

## 8. API 变更汇总

| 端点 | 变更 |
|---|---|
| `GET /api/llm/status` | **新增**：`{"enabled": bool, "model": str|null}`。enabled = `llm_enabled(cfg)` |
| `POST /api/projects` | 表单可选字段 `ai_feedback`，缺省 false |
| `POST /api/tasks/{id}/feedback` | **新增**：手动补跑，202/404/409 如上 |
| 下载 | 无新端点，`.feedback.md` / `.polished.md` 走现有 glob |

## 9. 前端

### 上传页（index.html + list.js）

- 开关区块 `#ai-feedback-block`：checkbox + 标签 "AI 反馈（点评 + 优化稿）"。
- 页面加载时 `GET /api/llm/status`：`enabled=false` → 整块隐藏（不 disabled 占位）。
- submit 追加 `form.append("ai_feedback", checked ? "true" : "false")`。

### 任务页（task.html + task.js）

- stage 列表加 "AI 反馈" 行：`meta.ai_feedback` 存在或 `meta.ai_error`/`ai_files`
  非空时显示（与"拼接音轨"行同机制）。
- 下载区：两个 `.md` 文件随现有列表自动出现，无新逻辑。
- "AI 反馈"按钮（done 状态 + LLM 可用时显示，两种文案一个出口）：
  - 无 `ai_files` → "生成 AI 反馈"（覆盖上传没开开关的旧任务）
  - 有 `ai_error` → 错误条 + "重试 AI 反馈"
  - 已有 `ai_files` 且无错 → 不显示（重跑走 API，不占 UI）
  点击 → `POST /api/tasks/{id}/feedback` → 恢复轮询。
- LLM 不可用（`/api/llm/status` enabled=false）→ 按钮不渲染。
- 兼容旧任务：无 `ai_feedback` 字段且 LLM 未配置 → 一切照旧。

## 10. 兼容性（Never break userspace）

- `config.yaml` 无 `llm` 节 → 功能整体不可见，现有行为分毫不动。
- 旧任务（无 `ai_feedback` meta）→ 任务页渲染与现在完全一致。
- 手动补跑端点只动 `status/stage/meta`，不碰转写产物文件。
- 上传 API 旧客户端不带 `ai_feedback` 字段 → 缺省 false，行为不变。

## 11. 测试策略（TDD）

| 模块 | 测试 |
|---|---|
| `asr/analyze.py` | 新 `tests/test_analyze.py`：各指标计算、空 segments、无 spk、有 spk 分桶、口头禅计数、提问切句 |
| `asr/llm.py` | 新 `tests/test_llm.py`：mock `requests.post`——成功解析、非 200、超时、结构异常、空 api_key 不带 Authorization 头；`llm_enabled` 判定 |
| `asr/feedback.py` | prompt 构建包含统计事实与忠实度约束（并入 test_llm.py 或独立小文件） |
| web 集成 | `tests/test_web.py` 增：`/api/llm/status` 有/无配置；上传 `ai_feedback` 落 meta；`_run_ai_stage` 成功（mock chat）→ 产物 + `ai_files`；mock 抛 `LLMError` → status 仍 `done` + `ai_error`；补跑端点 202/404/409；下载列表含 `.md` |
| E2E | LLM 配置就绪后真跑一轮（不进常规测试，手动 runbook） |

## 12. 非目标（明确不做）

- L3 原文↔优化版逐段对照稿（对齐算法复杂，价值/成本比低）
- 各维度数字评分（LLM 打分不可复现）
- SSE 流式输出（任务页本来就在轮询，进度条够用）
- 多模型路由 / 并发双调用
- 保留时间戳的优化版 SRT（校订后行数与原文不对齐，强行对齐必做假数据）
- LLM 调用自动重试退避（失败有手动补跑出口，足够）

## 13. 实现顺序

1. `asr/llm.py` + 测试（最底层，无依赖）
2. `asr/analyze.py` + 测试
3. `asr/feedback.py` prompt 构建 + 测试
4. web 集成：`_run_ai_stage` + 上传字段 + `/api/llm/status` + 补跑端点 + 测试
5. 前端：上传开关 + 任务页 stage/错误条/重试按钮
6. 真机 E2E 一轮（LLM 配置后）
