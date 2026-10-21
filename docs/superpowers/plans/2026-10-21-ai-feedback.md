# AI 反馈环节实现计划（教学点评 + 逐字稿优化）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 转写任务可选追加 AI stage：统计课堂数据（纯 Python）→ LLM 生成点评报告与优化逐字稿，失败不拖垮转写成果。

**Architecture:** 分四层——配置（`config.yaml` 的 `llm` 节，OpenAI 兼容端点，云/本地一套代码）→ 客户端（`asr/llm.py`，裸 requests）→ 生成（`asr/analyze.py` 确定性统计 + `asr/feedback.py` prompt 构建，两次独立调用两个产物）→ 集成（web 层 `_run_ai_stage` + 上传开关自动跑 + 手动补跑端点）。产物落 `outputs/{task_id}/`，下载走现有 `/download/{id}/{fmt}` 扩展两个 fmt 值。

**Tech Stack:** Python 3.12 / FastAPI / SQLite / pydantic v2 / requests（已有传递依赖，转显式）。前端 vanilla JS。

**Spec:** `docs/superpowers/specs/2026-10-21-ai-feedback-design.md`（计划与 spec 冲突时以 spec 为准）

## Global Constraints

- 测试命令一律 `py -3 -m pytest ...`（Windows，Python 3.12）。
- 每个 Task 结束跑全量 `py -3 -m pytest tests/ -q`，168+ 全绿才许提交（当前基线 168 passed）。
- 注释/docstring 用中文，风格与现有代码一致（讲"为什么"不讲"是什么"）。
- 不引入新依赖；`requests>=2.28` 只是从传递依赖转显式声明（Task 2）。
- 不改 `asr/pipeline.py`；`_run_pipeline` 只许改 meta 写入方式（Task 6），其余行为分毫不动。
- 数据库不加新列——AI 状态全部走 `meta_json`（`ai_feedback` / `ai_files` / `ai_error`）。
- 旧任务（无 ai 字段）、无 `llm` 配置节时行为与现在完全一致（Never break userspace）。
- 每个 commit 末尾加 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。

## Review Focus

实现时最容易咬人的五类输入/故障模式（spec 隐含但没有单条任务测试直接覆盖的，这里逐条钉到所属任务）：

1. **`md` fmt 下载被 AI 产物抢占**——outputs 同时有 `x.md` 和 `x.feedback.md` 时，`/download/{id}/md` 必须返回转写 MD。→ Task 5 Step 1 的抢占测试钉死。
2. **pipeline done 覆盖 meta 冲掉既有键**——多段任务的 `parts`、上传时的 `ai_feedback` 在 done 后必须还在 meta 里。→ Task 6 Step 1 测试同时断言两个键。
3. **AI 失败把任务标成 failed**——转写成果陪葬。合理行为：status 仍 done + `meta.ai_error`。→ Task 7 Step 1 失败路径测试钉死。
4. **手动补跑撞上正在跑的任务**——重复线程双写。合理行为：409。→ Task 8 Step 1 的 409 测试钉死。
5. **前端 done 即停轮询，错过 AI stage**——`ai_feedback` 为真且无 `ai_files`/`ai_error` 时必须继续轮询；failed 状态一律停。→ Task 9 Step 4 手动验证清单 + Task 10 E2E 断言。

---

### Task 1: 配置层——`LLMConfig`

**Files:**
- Modify: `asr/config.py`（`AppConfig` 前加 `LLMConfig`，`AppConfig` 加 `llm` 字段）
- Modify: `config.yaml`（文件末尾加注释示例，不启用）
- Test: `tests/test_config.py`（追加）

**Interfaces:**
- Consumes: 现有 `AppConfig` / `load_config`。
- Produces: `cfg.llm`，类型 `LLMConfig`，字段 `base_url: str = ""`、`api_key: str = ""`、`model: str = ""`、`timeout: int = 300`、`max_tokens: int = 16384`。旧配置无 `llm` 节 → 全默认值。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_config.py` 末尾（沿用该文件的 yaml 写法）：

```python
# ---------------------------------------------------------------------------
# AI 反馈：llm 配置节（缺省全空 = 功能关闭）
# ---------------------------------------------------------------------------
def _write_cfg(tmp_path, extra=""):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "models:\n"
        "  funasr:\n"
        "    asr:  m/asr\n    vad:  m/vad\n    punc: m/punc\n    spk:  m/spk\n"
        "  whisper:\n    size: small\n    compute_type: int8\n" + extra,
        encoding="utf-8",
    )
    return cfg


def test_llm_section_absent_defaults_off(tmp_path):
    """无 llm 节 → 全默认值，功能关闭（llm_enabled 语义的基础）。"""
    cfg = load_config(_write_cfg(tmp_path), strict_models=False)
    assert cfg.llm.base_url == ""
    assert cfg.llm.model == ""
    assert cfg.llm.api_key == ""
    assert cfg.llm.timeout == 300
    assert cfg.llm.max_tokens == 16384


def test_llm_section_parsed(tmp_path):
    """有 llm 节 → 值完整解析。"""
    cfg = load_config(_write_cfg(tmp_path, (
        "llm:\n"
        "  base_url: http://localhost:11434/v1\n"
        "  model: qwen2.5:7b\n"
        "  timeout: 120\n"
    )), strict_models=False)
    assert cfg.llm.base_url == "http://localhost:11434/v1"
    assert cfg.llm.model == "qwen2.5:7b"
    assert cfg.llm.timeout == 120
    assert cfg.llm.api_key == ""   # 本地端点不需要 key
```

- [ ] **Step 2: 跑测试确认失败**

Run: `py -3 -m pytest tests/test_config.py -q -k llm`
Expected: FAIL，`AttributeError: 'AppConfig' object has no attribute 'llm'`

- [ ] **Step 3: 最小实现**

`asr/config.py` 中 `OutputConfig` 类之后、`AppConfig` 之前加：

```python
class LLMConfig(BaseModel):
    """AI 反馈环节（可选）。全空 = 功能整体隐藏，不影响任何现有行为。"""
    base_url: str = ""    # OpenAI 兼容端点；Ollama 用 http://localhost:11434/v1
    api_key: str = ""     # 本地模型留空
    model: str = ""
    timeout: int = 300
    max_tokens: int = 16384
```

`AppConfig` 里 `output` 字段之后加一行：

```python
    llm: LLMConfig = Field(default_factory=LLMConfig)
```

`config.yaml` 末尾追加（纯注释，不启用）：

```yaml

# AI 反馈（教学点评 + 逐字稿优化）。整节缺失 = 功能隐藏。
# 云 API 与本地 Ollama 都走 OpenAI 兼容端点，一套代码：
# llm:
#   base_url: https://open.bigmodel.cn/api/paas/v4   # Ollama: http://localhost:11434/v1
#   api_key: 你的KEY                                   # 本地留空
#   model: glm-4-flash
#   timeout: 300
#   max_tokens: 16384
```

- [ ] **Step 4: 跑测试确认通过**

Run: `py -3 -m pytest tests/test_config.py -q`
Expected: 全 PASS

- [ ] **Step 5: 全量 + 提交**

```bash
py -3 -m pytest tests/ -q
git add asr/config.py config.yaml tests/test_config.py
git commit -m "feat(config): llm 配置节（OpenAI 兼容端点，缺省关闭）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: LLM 客户端——`asr/llm.py`

**Files:**
- Create: `asr/llm.py`
- Modify: `requirements.txt`（Web UI 节加 `requests>=2.28`）
- Test: `tests/test_llm.py`（新文件）

**Interfaces:**
- Consumes: Task 1 的 `AppConfig.llm`。
- Produces:
  - `class LLMError(Exception)` —— 所有 LLM 调用失败（网络/HTTP/结构/空内容）
  - `llm_enabled(cfg) -> bool` —— `base_url` 和 `model` 都非空串
  - `chat(messages: list[dict], cfg, *, temperature: float = 0.3, max_tokens: int | None = None) -> str` —— 返回 `choices[0].message.content`

- [ ] **Step 1: 写失败测试**

新文件 `tests/test_llm.py`：

```python
"""LLM 客户端测试：mock requests.post，不发真请求。"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from asr.config import load_config
from asr.llm import LLMError, chat, llm_enabled

CFG = load_config("config.yaml", strict_models=False)
CFG = CFG.model_copy(update={"llm": SimpleNamespace(
    base_url="http://fake:1/v1", api_key="sk-test", model="fake-model",
    timeout=5, max_tokens=512,
)})
# SimpleNamespace 规避 pydantic 校验；chat 只读属性，够用

MSG = [{"role": "user", "content": "hi"}]


def _resp(status=200, body=None):
    r = SimpleNamespace(status_code=status, text='{"raw": 1}')
    r.json = lambda: body if body is not None else {
        "choices": [{"message": {"content": "你好"}}]
    }
    return r


def test_llm_enabled_requires_base_url_and_model():
    assert llm_enabled(CFG) is True
    off = CFG.model_copy(update={"llm": SimpleNamespace(
        base_url="", api_key="", model="", timeout=5, max_tokens=512)})
    assert llm_enabled(off) is False


def test_chat_ok_and_auth_header():
    with patch("asr.llm.requests.post", return_value=_resp()) as p:
        out = chat(MSG, CFG)
    assert out == "你好"
    url, = p.call_args.args
    assert url == "http://fake:1/v1/chat/completions"
    assert p.call_args.kwargs["headers"]["Authorization"] == "Bearer sk-test"
    assert p.call_args.kwargs["json"]["model"] == "fake-model"
    assert p.call_args.kwargs["timeout"] == 5


def test_chat_local_no_auth_header():
    local = CFG.model_copy(update={"llm": SimpleNamespace(
        base_url="http://localhost:11434/v1", api_key="", model="q",
        timeout=5, max_tokens=512)})
    with patch("asr.llm.requests.post", return_value=_resp()) as p:
        chat(MSG, local)
    assert "Authorization" not in p.call_args.kwargs["headers"]


def test_chat_http_error_raises_llmerror():
    bad = _resp(status=500, body={"error": "boom"})
    with patch("asr.llm.requests.post", return_value=bad):
        with pytest.raises(LLMError, match="500"):
            chat(MSG, CFG)


def test_chat_network_error_wrapped():
    with patch("asr.llm.requests.post",
               side_effect=requests.ConnectionError("refused")):
        with pytest.raises(LLMError, match="ConnectionError"):
            chat(MSG, CFG)


def test_chat_bad_structure_raises():
    for body in ({}, {"choices": []}, {"choices": [{"message": {}}]},
                 {"choices": [{"message": {"content": "  "}}]}):
        with patch("asr.llm.requests.post",
                   return_value=_resp(body=body)):
            with pytest.raises(LLMError):
                chat(MSG, CFG)


def test_chat_disabled_config_raises():
    off = CFG.model_copy(update={"llm": SimpleNamespace(
        base_url="", api_key="", model="", timeout=5, max_tokens=512)})
    with pytest.raises(LLMError, match="未配置"):
        chat(MSG, off)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `py -3 -m pytest tests/test_llm.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'asr.llm'`

- [ ] **Step 3: 最小实现**

新文件 `asr/llm.py`：

```python
"""OpenAI 兼容 LLM 客户端——云 API（GLM/DeepSeek…）与本地 Ollama 一套代码。

不引入 SDK：一个 POST 的事，requests 足够。
"""
from __future__ import annotations

import requests

from .config import AppConfig


class LLMError(Exception):
    """LLM 调用失败（未配置/网络/HTTP/响应结构/空内容）。"""


def llm_enabled(cfg: AppConfig) -> bool:
    """llm 节可用 = base_url 与 model 都非空。"""
    return bool(cfg.llm.base_url.strip() and cfg.llm.model.strip())


def chat(
    messages: list[dict],
    cfg: AppConfig,
    *,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> str:
    """调 /chat/completions，返回首条回复文本。任何失败抛 LLMError。"""
    if not llm_enabled(cfg):
        raise LLMError("LLM 未配置（config.yaml 缺 llm 节或 base_url/model 为空）")

    headers = {"Content-Type": "application/json"}
    if cfg.llm.api_key.strip():
        headers["Authorization"] = f"Bearer {cfg.llm.api_key.strip()}"

    body = {
        "model": cfg.llm.model.strip(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or cfg.llm.max_tokens,
    }
    url = cfg.llm.base_url.strip().rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(url, json=body, headers=headers,
                             timeout=cfg.llm.timeout)
    except requests.RequestException as e:  # noqa: BLE001
        raise LLMError(f"LLM 请求失败: {type(e).__name__}: {e}") from e

    if resp.status_code != 200:
        raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LLMError(f"LLM 响应结构异常: {e}") from e
    if not content or not content.strip():
        raise LLMError("LLM 返回空内容")
    return content
```

`requirements.txt` 的 `# Web UI` 节（`python-multipart` 行后）加：

```
requests>=2.28
```

- [ ] **Step 4: 跑测试确认通过**

Run: `py -3 -m pytest tests/test_llm.py -q`
Expected: 全 PASS

- [ ] **Step 5: 全量 + 提交**

```bash
py -3 -m pytest tests/ -q
git add asr/llm.py requirements.txt tests/test_llm.py
git commit -m "feat(llm): OpenAI 兼容客户端（LLMError/llm_enabled/chat）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: 统计层——`asr/analyze.py`

**Files:**
- Create: `asr/analyze.py`
- Test: `tests/test_analyze.py`（新文件）

**Interfaces:**
- Consumes: segments dict（`{"start": float, "end": float, "text": str, "spk": str|None}`）。
- Produces: `compute_stats(segments: list[dict]) -> dict`，键：`duration_sec`（float，两位）、`chars`（int，去空白字符数）、`speed_cpm`（float，字/分钟=chars/(Σ(end-start)/60)）、`filler_top`（`[(词, 次数)]` 降序 TOP5，零次不入）、`questions`（有 spk：`{说话人: 次数}`；无 spk：`{"整体": 次数}`）、`speakers`（有 spk：`[{name, duration_sec, pct, chars, speed_cpm}]` 按时长降序；无 spk：`[]`）。空列表 → 各字段零值/空容器。常量 `FILLER_WORDS: tuple[str, ...]`。

- [ ] **Step 1: 写失败测试**

新文件 `tests/test_analyze.py`：

```python
"""课堂数据统计测试：全部确定性计算，零 LLM。"""
from asr.analyze import FILLER_WORDS, compute_stats


def _seg(start, end, text, spk=None):
    s = {"start": start, "end": end, "text": text}
    if spk:
        s["spk"] = spk
    return s


def test_empty_segments():
    s = compute_stats([])
    assert s["duration_sec"] == 0.0 and s["chars"] == 0
    assert s["filler_top"] == [] and s["speakers"] == []
    assert s["questions"] == {}


def test_basic_no_speaker():
    segs = [
        _seg(0.0, 10.0, "那么我们开始上课。"),      # 10 字符
        _seg(10.0, 20.0, "这个就是今天的内容，好吗？"),
    ]
    s = compute_stats(segs)
    assert s["duration_sec"] == 20.0
    assert s["chars"] == 10 + 14          # 去空白后逐字数
    assert s["speed_cpm"] == round(24 / (20 / 60), 1)
    # 提问：第二段切句 "这个就是今天的内容，好吗？" → 句尾"吗？"计 1
    assert s["questions"] == {"整体": 1}
    # 口头禅：那么1、这个1、就是1（"吗"不在词表）
    words = [w for w, _ in s["filler_top"]]
    assert set(words) == {"那么", "这个", "就是"}


def test_speaker_buckets():
    segs = [
        _seg(0.0, 40.0, "老师讲四十秒的内容。", spk="教师"),
        _seg(40.0, 50.0, "学生回答。", spk="学生1"),
        _seg(50.0, 60.0, "继续讲。", spk="教师"),
    ]
    s = compute_stats(segs)
    assert s["duration_sec"] == 60.0
    assert [sp["name"] for sp in s["speakers"]] == ["教师", "学生1"]
    assert s["speakers"][0]["duration_sec"] == 50.0
    assert s["speakers"][0]["pct"] == round(50 / 60, 3)
    assert set(s["questions"]) == {"教师", "学生1"}


def test_filler_top_capped_and_zero_dropped():
    text = "那么那么那么那么然后然后这个这个其实"
    s = compute_stats([_seg(0, 10, text)])
    assert s["filler_top"][0] == ("那么", 4)
    assert all(c > 0 for _, c in s["filler_top"])
    assert len(s["filler_top"]) <= 5


def test_question_sentence_split():
    segs = [
        _seg(0, 10, "先讲第一点。接下来提问，大家听懂了吗？没懂的举手。"),
        _seg(10, 20, "很好，我们继续。"),
    ]
    s = compute_stats(segs)
    assert s["questions"] == {"整体": 1}    # 只有"大家听懂了吗？"一条


def test_filler_word_table_no_single_char():
    """词表不含单字（"啊/嗯"子串误匹配率太高）。"""
    assert all(len(w) >= 2 for w in FILLER_WORDS)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `py -3 -m pytest tests/test_analyze.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'asr.analyze'`

- [ ] **Step 3: 最小实现**

新文件 `asr/analyze.py`：

```python
"""课堂数据统计：segments 的确定性计算，零 LLM。

语速、口头禅频次、提问次数这些定量事实由 Python 算——LLM 数数必幻觉。
统计值作为证据喂给它，它只管定性解读。
"""
from __future__ import annotations

import re

# 口头禅词表：不含单字（"啊/嗯"子串误匹配率太高）。子串计数会把正常用法
# （如"这个人"）也计入——有意为之：统计是给 LLM 的量级线索，不是结论。
FILLER_WORDS = (
    "那么", "然后", "就是", "这个", "那个", "其实", "对吧",
    "好吧", "等于说", "就是说", "对不对", "我们来看",
)

# 切句不含"？"：问句以"？/?"结尾识别；"？吗"组合靠末字兜底
_SENT_SPLIT = re.compile(r"[。！!?]")
_Q_TAIL_CHARS = ("吗", "呢", "吧")


def _count_chars(texts: list[str]) -> int:
    return sum(len(re.sub(r"\s", "", t)) for t in texts)


def _question_count(texts: list[str]) -> int:
    n = 0
    for t in texts:
        for sent in _SENT_SPLIT.split(t):
            s = sent.strip()
            if s and (s.endswith(("？", "?")) or s.endswith(_Q_TAIL_CHARS)):
                n += 1
    return n


def _speaker_row(name: str, segs: list[dict], total_dur: float) -> dict:
    dur = sum(s["end"] - s["start"] for s in segs)
    chars = _count_chars([s.get("text", "") for s in segs])
    return {
        "name": name,
        "duration_sec": round(dur, 2),
        "pct": round(dur / total_dur, 3) if total_dur else 0.0,
        "chars": chars,
        "speed_cpm": round(chars / (dur / 60), 1) if dur else 0.0,
    }


def compute_stats(segments: list[dict]) -> dict:
    """逐字稿 → 课堂数据事实。输入空/缺键不抛错，出零值。"""
    if not segments:
        return {"duration_sec": 0.0, "chars": 0, "speed_cpm": 0.0,
                "filler_top": [], "questions": {}, "speakers": []}

    texts = [s.get("text", "") for s in segments]
    duration = max(s["end"] for s in segments) - min(s["start"] for s in segments)
    speak_sec = sum(s["end"] - s["start"] for s in segments)
    chars = _count_chars(texts)

    counts = sorted(
        ((w, sum(t.count(w) for t in texts)) for w in FILLER_WORDS),
        key=lambda kv: -kv[1],
    )
    filler_top = [(w, c) for w, c in counts if c > 0][:5]

    speakers: list[dict] = []
    if any(s.get("spk") for s in segments):
        by_spk: dict[str, list[dict]] = {}
        for s in segments:
            by_spk.setdefault(s.get("spk") or "未知", []).append(s)
        total = speak_sec or 1.0
        for name, segs in sorted(
            by_spk.items(),
            key=lambda kv: -sum(s["end"] - s["start"] for s in kv[1]),
        ):
            speakers.append(_speaker_row(name, segs, total))
            questions = {
                name: _question_count([s.get("text", "") for s in segs])
                for name, segs in by_spk.items()
            }
    else:
        questions = {"整体": _question_count(texts)}

    return {
        "duration_sec": round(duration, 2),
        "chars": chars,
        "speed_cpm": round(chars / (speak_sec / 60), 1) if speak_sec else 0.0,
        "filler_top": filler_top,
        "questions": questions,
        "speakers": speakers,
    }
```

注意：实现时把 `questions` 的构建移出 `for` 循环（上面放在循环里是示意，写成循环外一次构建，避免重复计算）。正确写法：

```python
        speakers = [
            _speaker_row(name, segs, total)
            for name, segs in sorted(
                by_spk.items(),
                key=lambda kv: -sum(s["end"] - s["start"] for s in kv[1]),
            )
        ]
        questions = {
            name: _question_count([s.get("text", "") for s in segs])
            for name, segs in by_spk.items()
        }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `py -3 -m pytest tests/test_analyze.py -q`
Expected: 全 PASS

- [ ] **Step 5: 全量 + 提交**

```bash
py -3 -m pytest tests/ -q
git add asr/analyze.py tests/test_analyze.py
git commit -m "feat(analyze): 课堂数据统计（语速/口头禅/提问/说话人占比）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: 生成层——`asr/feedback.py` prompt 构建

**Files:**
- Create: `asr/feedback.py`
- Test: `tests/test_feedback.py`（新文件）

**Interfaces:**
- Consumes: Task 3 的 stats dict。
- Produces:
  - `build_feedback_messages(transcript: str, stats: dict) -> list[dict]`（system + user 两条）
  - `build_polish_messages(transcript: str) -> list[dict]`

- [ ] **Step 1: 写失败测试**

新文件 `tests/test_feedback.py`：

```python
"""prompt 构建测试：不调 LLM，只验结构与硬约束在文。"""
import json

from asr.feedback import build_feedback_messages, build_polish_messages

STATS = {"duration_sec": 1800.0, "chars": 9000, "speed_cpm": 300.0,
         "filler_top": [("就是", 47)], "questions": {"教师": 12},
         "speakers": []}
TEXT = "教师: 那么我们开始上课。"


def test_feedback_messages_structure():
    msgs = build_feedback_messages(TEXT, STATS)
    assert [m["role"] for m in msgs] == ["system", "user"]
    # 统计事实原样进 user（LLM 只许引用给定数字，不许自己算）
    assert json.dumps(STATS, ensure_ascii=False) in msgs[1]["content"]
    assert TEXT in msgs[1]["content"]


def test_feedback_system_has_constraints():
    sysmsg = build_feedback_messages(TEXT, STATS)[0]["content"]
    for kw in ("讲解结构", "节奏", "师生互动", "语言表达",
               "不得编造", "疑似识别错误", "依据"):
        assert kw in sysmsg, f"system prompt 缺少约束关键词: {kw}"


def test_polish_messages_constraints():
    msgs = build_polish_messages(TEXT)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[1]["content"] == TEXT
    for kw in ("不改原意", "不增删信息", "本课要点", "口头禅"):
        assert kw in msgs[0]["content"], f"polish prompt 缺少红线: {kw}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `py -3 -m pytest tests/test_feedback.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'asr.feedback'`

- [ ] **Step 3: 最小实现**

新文件 `asr/feedback.py`：

```python
"""AI 反馈的 prompt 构建：点评报告 + 优化逐字稿，两次独立调用。

点评与优化分开调：合输出会顶模型输出上限（截断风险真实），分开各自
5~8 千 token，稳，且独立重试互不连坐。
"""
from __future__ import annotations

import json

FEEDBACK_SYSTEM = """你是一位资深的教学评课专家兼文字编辑。用户会提供一段课堂录音的逐字稿（可能带"说话人: "前缀）和一份课堂数据统计（JSON）。请输出 Markdown 反馈报告，结构固定：

# AI 课堂反馈

## 一、教学反馈
依次四个小节：### 讲解结构、### 节奏、### 师生互动、### 语言表达。
每条意见必须附依据，格式："依据：〈原文引用（≤50 字）或统计事实〉"。
先写具体优点，再写改进建议，建议合计不超过 6 条。

## 二、稿件反馈
- 疑似识别错误：逐条列出原句与猜测正字（同音字、专有名词等）
- 可读性问题：长句无标点、指代不明等

## 三、课堂数据
把统计 JSON 整理成表格（语速/提问/口头禅/说话人占比）。

硬性约束：
- 不得编造原文不存在的内容
- 统计数字只能引用给定 JSON 里的值，不得自行计算或估算
- 全文用中文，直接输出报告，不要任何前言或解释。"""

POLISH_SYSTEM = """你是一位细心的文字编辑。把下面的课堂逐字稿整理成可读版本，输出 Markdown：
1. 删除口头禅和无意义重复，修正标点，按语义分段
2. 红线：不改原意、不增删信息、不做书面化改写，保留口语的自然度
3. 文末加 "## 本课要点" 一节：3~6 条要点，每条一行
全文用中文，直接输出整理稿，不要任何前言或解释。"""


def build_feedback_messages(transcript: str, stats: dict) -> list[dict]:
    user = ("课堂逐字稿：\n" + transcript
            + "\n\n课堂数据统计（JSON）：\n"
            + json.dumps(stats, ensure_ascii=False, indent=2))
    return [{"role": "system", "content": FEEDBACK_SYSTEM},
            {"role": "user", "content": user}]


def build_polish_messages(transcript: str) -> list[dict]:
    return [{"role": "system", "content": POLISH_SYSTEM},
            {"role": "user", "content": transcript}]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `py -3 -m pytest tests/test_feedback.py -q`
Expected: 全 PASS

- [ ] **Step 5: 全量 + 提交**

```bash
py -3 -m pytest tests/ -q
git add asr/feedback.py tests/test_feedback.py
git commit -m "feat(feedback): 点评/优化稿 prompt 构建（证据引用+忠实度红线）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: 下载路由扩展——`feedback` / `polished` fmt

**Files:**
- Modify: `asr/web/__init__.py:259-284`（`download` 函数）
- Test: `tests/test_web.py`（追加）

**Interfaces:**
- Consumes: 现有 `/download/{task_id}/{fmt}` 路由与 meta。
- Produces: `fmt="feedback"` → 返回 `outputs/{task_id}/*.feedback.md`（`FileResponse`，下载名 `{任务名}.feedback.md`）；`fmt="polished"` 同理。`fmt="md"` 的 glob 排除 `*.feedback.md` / `*.polished.md`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_web.py`（放在 `test_static_file_not_found` 之后；辅助函数放测试紧上方）：

```python
# ---------------------------------------------------------------------------
# AI 反馈产物下载：feedback/polished fmt + md 防抢占
# ---------------------------------------------------------------------------
def _mk_done_task_with_ai_files(isolated_db, task_id="dl1"):
    """造一个 done 任务，outputs 里同时有转写 md 和两个 AI 产物。"""
    import asr.web as wm
    out_dir = wm.OUTPUT_ROOT / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{task_id}_测试课.md").write_text("转写正文", encoding="utf-8")
    (out_dir / f"{task_id}_测试课.feedback.md").write_text("# AI 课堂反馈", encoding="utf-8")
    (out_dir / f"{task_id}_测试课.polished.md").write_text("# 优化稿", encoding="utf-8")
    with closing(wm._db()) as conn:
        conn.execute(
            "INSERT INTO tasks (id, name, filename, input_path, output_dir,"
            " status, progress, created_at, meta_json)"
            " VALUES (?, '测试课', 'l.mp4', 'x', ?, 'done', 1.0, '2026-10-21', ?)",
            (task_id, str(out_dir), json.dumps(
                {"ai_files": {"feedback": f"{task_id}_测试课.feedback.md",
                              "polished": f"{task_id}_测试课.polished.md"}},
                ensure_ascii=False)),
        )
    return out_dir


def test_download_md_not_hijacked_by_ai_files(client, isolated_db):
    """md fmt 必须返回转写 md，不能被 .feedback.md/.polished.md 抢占。"""
    _mk_done_task_with_ai_files(isolated_db)
    r = client.get("/download/dl1/md")
    assert r.status_code == 200
    assert r.text == "转写正文"


def test_download_feedback_and_polished_fmt(client, isolated_db):
    _mk_done_task_with_ai_files(isolated_db)
    r = client.get("/download/dl1/feedback")
    assert r.status_code == 200
    assert "# AI 课堂反馈" in r.text
    assert "feedback.md" in r.headers.get("content-disposition", "")
    r = client.get("/download/dl1/polished")
    assert r.status_code == 200
    assert "# 优化稿" in r.text


def test_download_feedback_missing_404(client, isolated_db):
    """任务没有 AI 产物 → feedback fmt 404（不 500）。"""
    import asr.web as wm
    out_dir = wm.OUTPUT_ROOT / "dl2"
    out_dir.mkdir(parents=True, exist_ok=True)
    with closing(wm._db()) as conn:
        conn.execute(
            "INSERT INTO tasks (id, name, filename, input_path, output_dir,"
            " status, progress, created_at)"
            " VALUES ('dl2', 'n', 'a.mp4', 'x', ?, 'done', 1.0, '2026-10-21')",
            (str(out_dir),),
        )
    assert client.get("/download/dl2/feedback").status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `py -3 -m pytest tests/test_web.py -q -k "download_feedback or download_md_not"`
Expected: FAIL——`feedback` fmt 被 ALLOWED 拒绝（400）；md 抢占测试可能碰巧过（glob 顺序不定），失败以 400 为主。

- [ ] **Step 3: 最小实现**

`asr/web/__init__.py` 的 `download` 函数改为：

```python
@app.get("/download/{task_id}/{fmt}")
def download(task_id: str, fmt: str):
    """下载指定格式输出（SRT/VTT/MD/TXT/JSON/REPLACE_LOG/AI 反馈产物）。"""
    ALLOWED = {"srt", "vtt", "md", "txt", "json", "replace.log", "mp4",
               "feedback", "polished"}
    if fmt not in ALLOWED:
        raise HTTPException(400, f"不支持的格式: {fmt}")

    with closing(_db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    if row["status"] != "done":
        raise HTTPException(409, f"任务未完成: {row['status']}")

    out_dir = Path(row["output_dir"])
    if fmt in ("feedback", "polished"):
        # AI 反馈产物：*.feedback.md / *.polished.md
        files = list(out_dir.glob(f"*.{fmt}.md"))
    else:
        # 排除分段文件（{两位序号}_ 前缀）和 AI 产物——md 的 *.md glob 会
        # 同时命中 *.feedback.md，而 files[0] 不保证顺序，不排除就是抢占
        files = [
            f for f in out_dir.glob(f"*.{fmt}")
            if not re.match(r"^\d{2}_", f.name)
            and not re.search(r"\.(feedback|polished)\.md$", f.name)
        ]
    if not files:
        raise HTTPException(404, f"输出文件不存在: {fmt}")
    target = files[0]
    stem = row["name"] or Path(row["filename"]).stem
    suffix = f".{fmt}.md" if fmt in ("feedback", "polished") else f".{fmt}"
    return FileResponse(str(target), filename=f"{Path(stem).stem}{suffix}")
```

注意：这把现有 `filename=f"{Path(row['filename']).stem}.{fmt}"` 的下载名基准从
`filename` 换成了 `name`（任务名），与 part_srt 的做法一致；若全量测试里有断言旧
下载名的用例失败，按新行为更新断言（这是显示名修正，不是行为破坏）。

- [ ] **Step 4: 跑测试确认通过**

Run: `py -3 -m pytest tests/test_web.py -q -k "download"`
Expected: 全 PASS（含既有下载用例）

- [ ] **Step 5: 全量 + 提交**

```bash
py -3 -m pytest tests/ -q
git add asr/web/__init__.py tests/test_web.py
git commit -m "feat(web): 下载路由支持 feedback/polished fmt，md glob 防抢占

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: `_run_pipeline` done 改 meta 读-改-写

**Files:**
- Modify: `asr/web/__init__.py:762-786`（`_run_pipeline` 的 meta 构建与 done 更新）
- Test: `tests/test_web.py`（追加）

**Interfaces:**
- Consumes: 现有 `_run_pipeline` / `_read_meta`。
- Produces: done 时 `meta_json = {**既有 meta, **pipeline meta}`——`parts` / `merge_mode` / `ai_feedback` 等 done 前写入的键全部保留。后续任务（Task 7/8/9）依赖此语义。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_web.py`：

```python
# ---------------------------------------------------------------------------
# pipeline done 的 meta 读-改-写：done 不许冲掉既有 meta 键
# ---------------------------------------------------------------------------
def test_pipeline_done_preserves_existing_meta(isolated_db, monkeypatch, tmp_path):
    """done 前写进 meta 的键（parts/ai_feedback/merge_mode）在 done 后仍在。"""
    import asr.web as wm
    out_dir = wm.OUTPUT_ROOT / "pm1"
    out_dir.mkdir(parents=True, exist_ok=True)
    with closing(wm._db()) as conn:
        conn.execute(
            "INSERT INTO tasks (id, name, filename, input_path, output_dir,"
            " status, created_at, meta_json)"
            " VALUES ('pm1', '课', 'a.mp4', 'x', ?, 'pending', '2026-10-21', ?)",
            (str(out_dir), json.dumps(
                {"parts": ["a.mp4", "b.mp4"], "ai_feedback": True,
                 "merge_mode": "concat"}, ensure_ascii=False)),
        )
    jp = tmp_path / "out.json"
    jp.write_text(json.dumps({"segments": [
        {"start": 0.0, "end": 1.0, "text": "嗯", "spk": "教师"}]},
        ensure_ascii=False), encoding="utf-8")

    class FakeResult:
        duration_s = 10.0
        elapsed_s = 1.0
        rtf = 0.1
        segments_count = 1
        written = {"json": str(jp)}

    monkeypatch.setattr(wm, "pipeline_run", lambda *a, **kw: FakeResult())
    wm._run_pipeline("pm1", tmp_path / "in.wav", out_dir, "config.yaml",
                     False, "balanced")

    with closing(wm._db()) as conn:
        row = conn.execute("SELECT status, meta_json FROM tasks WHERE id='pm1'").fetchone()
    assert row["status"] == "done"
    meta = json.loads(row["meta_json"])
    assert meta["parts"] == ["a.mp4", "b.mp4"]      # 现网多段任务依赖
    assert meta["ai_feedback"] is True              # AI 开关依赖（Task 7）
    assert meta["merge_mode"] == "concat"           # 旧版任务依赖
    assert "duration_s" in meta                     # pipeline 自己的键照写
```

- [ ] **Step 2: 跑测试确认失败**

Run: `py -3 -m pytest tests/test_web.py -q -k preserves_existing_meta`
Expected: FAIL，`assert meta["parts"] == ...`——done 把 meta 整体覆盖了。

- [ ] **Step 3: 最小实现**

`_run_pipeline` 里 done 更新前（`meta = {...}` 与 `_update_task(...)` 之间）改：

```python
        meta = {
            "duration_s": result.duration_s,
            "elapsed_s": result.elapsed_s,
            "rtf": result.rtf,
            "segments_count": result.segments_count,
            "formats": list(result.written.keys()),
        }
        # 读-改-写：done 前写入的键（parts/merge_mode/ai_feedback…）不许被冲掉
        meta = {**_read_meta(task_id), **meta}
```

（`_read_meta` 已存在于 `asr/web/__init__.py:595`。）

- [ ] **Step 4: 跑测试确认通过**

Run: `py -3 -m pytest tests/test_web.py -q`
Expected: 全 PASS（既有用例不受影响——单段任务 done 前 meta 为空，合并结果与旧值一致）

- [ ] **Step 5: 全量 + 提交**

```bash
py -3 -m pytest tests/ -q
git add asr/web/__init__.py tests/test_web.py
git commit -m "fix(web): pipeline done 的 meta 改读-改-写，保留 parts/ai_feedback 等既有键

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: `_run_ai_stage` + 上传开关 + 自动路径串联

**Files:**
- Modify: `asr/web/__init__.py`（新增 `_run_ai_stage`、`_run_single_task`；改 `create_project`、`_run_multi_task`）
- Test: `tests/test_web.py`（追加）

**Interfaces:**
- Consumes: Task 2 `chat`/`llm_enabled`/`LLMError`、Task 3 `compute_stats`、Task 4 `build_feedback_messages`/`build_polish_messages`、现有 `_read_meta`/`_read_final_segments`/`_update_task`。
- Produces:
  - `_run_ai_stage(task_id: str, config_path: str) -> None` —— 成功：写两个 `.md` + meta `ai_files` + status done/stage done；失败：meta `ai_error` + **status 仍 done**（不拖垮转写）；开始时 meta 补 `ai_feedback: True`
  - `_run_single_task(task_id, input_path: Path, output_dir: Path, config_path: str, diarize: bool, quality: str, ai_feedback: bool) -> None` —— 单任务 wrapper：pipeline → done 检查 → 可选 AI stage
  - `create_project` 新表单字段 `ai_feedback: bool = Form(False)`，落 meta（单/多一致）
  - `_run_multi_task(..., ai_feedback: bool)` 末尾可选 AI stage
  - meta 键约定：`ai_feedback: bool`、`ai_files: {"feedback": str, "polished": str}`、`ai_error: str`（Task 8/9 依赖）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_web.py`：

```python
# ---------------------------------------------------------------------------
# AI stage：统计→点评→优化稿；失败不拖垮转写成果
# ---------------------------------------------------------------------------
def _mk_ai_task(isolated_db, monkeypatch, *, task_id="ai1", with_segs=True,
                chat_side_effect=None):
    """造一个已 done 的任务（transcript 列有 segments），mock 掉 LLM 层。"""
    import asr.web as wm
    out_dir = wm.OUTPUT_ROOT / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    segs = ([{"start": 0.0, "end": 3.0, "text": "那么我们开始上课。", "spk": "教师"}]
            if with_segs else [])
    with closing(wm._db()) as conn:
        conn.execute(
            "INSERT INTO tasks (id, name, filename, input_path, output_dir,"
            " status, progress, stage, created_at, meta_json, transcript)"
            " VALUES (?, '测试课', 'l.mp4', 'x', ?, 'done', 1.0, 'done',"
            " '2026-10-21', ?, ?)",
            (task_id, str(out_dir),
             json.dumps({"ai_feedback": True}, ensure_ascii=False),
             json.dumps(segs, ensure_ascii=False)),
        )
    calls = []

    def fake_chat(messages, cfg, **kw):
        calls.append(messages[0]["content"][:20])
        if chat_side_effect:
            raise chat_side_effect
        return f"# 第{len(calls)}次结果"

    monkeypatch.setattr(wm, "chat", fake_chat)
    monkeypatch.setattr(wm, "llm_enabled", lambda cfg: True)
    return wm, out_dir, calls


def test_run_ai_stage_success(isolated_db, monkeypatch):
    wm, out_dir, calls = _mk_ai_task(isolated_db, monkeypatch)
    wm._run_ai_stage("ai1", "config.yaml")
    assert len(calls) == 2                          # 点评 + 优化稿，两次独立调用
    with closing(wm._db()) as conn:
        row = conn.execute(
            "SELECT status, stage, meta_json FROM tasks WHERE id='ai1'").fetchone()
    assert row["status"] == "done" and row["stage"] == "done"
    meta = json.loads(row["meta_json"])
    fb, po = meta["ai_files"]["feedback"], meta["ai_files"]["polished"]
    assert "ai_error" not in meta
    assert (out_dir / fb).read_text(encoding="utf-8") == "# 第1次结果"
    assert (out_dir / po).read_text(encoding="utf-8") == "# 第2次结果"
    assert fb.endswith(".feedback.md") and po.endswith(".polished.md")


def test_run_ai_stage_failure_keeps_done(isolated_db, monkeypatch):
    """LLM 失败 → status 仍 done + meta.ai_error，不写产物，不拖垮转写。"""
    from asr.llm import LLMError
    wm, out_dir, calls = _mk_ai_task(
        isolated_db, monkeypatch, chat_side_effect=LLMError("LLM HTTP 500: boom"))
    wm._run_ai_stage("ai1", "config.yaml")
    with closing(wm._db()) as conn:
        row = conn.execute(
            "SELECT status, meta_json FROM tasks WHERE id='ai1'").fetchone()
    assert row["status"] == "done"
    assert "boom" in json.loads(row["meta_json"])["ai_error"]
    assert not list(out_dir.glob("*.feedback.md"))
    assert not list(out_dir.glob("*.polished.md"))


def test_run_ai_stage_no_transcript(isolated_db, monkeypatch):
    """无转写结果 → ai_error，不调 LLM。"""
    wm, out_dir, calls = _mk_ai_task(isolated_db, monkeypatch, with_segs=False)
    wm._run_ai_stage("ai1", "config.yaml")
    with closing(wm._db()) as conn:
        row = conn.execute(
            "SELECT status, meta_json FROM tasks WHERE id='ai1'").fetchone()
    assert row["status"] == "done"
    assert "无转写结果" in json.loads(row["meta_json"])["ai_error"]
    assert calls == []


def test_run_ai_stage_marks_flag_and_stats_fed(isolated_db, monkeypatch):
    """prompt 里必须带统计事实（filler/语速）与说话人前缀正文。"""
    import asr.web as wm
    from asr.analyze import compute_stats
    seen = {}

    def fake_chat(messages, cfg, **kw):
        if "评课专家" in messages[0]["content"]:
            seen["feedback_user"] = messages[1]["content"]
        return "# 结果"

    out_dir = wm.OUTPUT_ROOT / "ai2"
    out_dir.mkdir(parents=True, exist_ok=True)
    segs = [{"start": 0.0, "end": 60.0, "text": "那么那么那么我们上课。", "spk": "教师"}]
    with closing(wm._db()) as conn:
        conn.execute(
            "INSERT INTO tasks (id, name, filename, input_path, output_dir,"
            " status, created_at, transcript)"
            " VALUES ('ai2', '课', 'l.mp4', 'x', ?, 'done', '2026-10-21', ?)",
            (str(out_dir), json.dumps(segs, ensure_ascii=False)),
        )
    monkeypatch.setattr(wm, "chat", fake_chat)
    monkeypatch.setattr(wm, "llm_enabled", lambda cfg: True)
    wm._run_ai_stage("ai2", "config.yaml")
    assert "那么" in seen["feedback_user"]            # 正文（带说话人前缀）
    assert "filler_top" in seen["feedback_user"]      # 统计事实进了 prompt
    with closing(wm._db()) as conn:
        meta = json.loads(conn.execute(
            "SELECT meta_json FROM tasks WHERE id='ai2'").fetchone()["meta_json"])
    assert meta["ai_feedback"] is True                # 手动补跑也补旗标


def test_upload_ai_feedback_flag_reaches_meta(client, isolated_db, monkeypatch):
    """上传带 ai_feedback=true → meta 落库，单任务 wrapper 收到 True。"""
    import asr.web as wm
    got = {}

    def fake_single(task_id, inp, out, cfg, dia, q, ai):
        got["ai"] = ai

    _install_fake_probe(monkeypatch, [_mk_info("a.mp3")])
    monkeypatch.setattr(wm, "_run_single_task", fake_single)
    monkeypatch.setattr(wm, "_run_multi_task", lambda *a, **kw: None)
    r = client.post("/api/projects",
                    files={"files": ("a.mp3", b"x", "audio/mpeg")},
                    data={"name": "AI课", "ai_feedback": "true"})
    assert r.status_code == 200
    with closing(wm._db()) as conn:
        row = conn.execute(
            "SELECT meta_json FROM tasks WHERE id=?",
            (r.json()["task_id"],)).fetchone()
    assert json.loads(row["meta_json"])["ai_feedback"] is True
    assert got["ai"] is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `py -3 -m pytest tests/test_web.py -q -k "ai_stage or ai_feedback_flag"`
Expected: FAIL，`AttributeError: module 'asr.web' has no attribute '_run_ai_stage'`（上传用例 500/AttributeError 同理）

- [ ] **Step 3: 最小实现**

`asr/web/__init__.py` 顶部 import 区补（`from ..merge import ...` 之后）：

```python
from ..analyze import compute_stats
from ..feedback import build_feedback_messages, build_polish_messages
from ..llm import LLMError, chat, llm_enabled
```

新增两个函数（放在 `_run_pipeline` 之后）：

```python
# ---------------------------------------------------------------------------
# AI 反馈 stage（点评 + 优化稿）。铁律：AI 失败不拖垮转写成果。
# ---------------------------------------------------------------------------
def _run_ai_stage(task_id: str, config_path: str) -> None:
    """统计课堂数据 → LLM 点评 → LLM 优化稿。异常一律落 meta.ai_error，status 保持 done。"""
    ts = datetime.now().isoformat(timespec="seconds")
    try:
        cfg = load_config(config_path, strict_models=False)
        if not llm_enabled(cfg):
            raise LLMError("LLM 未配置")

        with closing(_db()) as conn:
            row = conn.execute(
                "SELECT output_dir, name, filename FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            return
        stem = row["name"] or Path(row["filename"]).stem

        segs = _read_final_segments(task_id)
        if not segs:
            raise LLMError("无转写结果，无法生成 AI 反馈")

        # 补旗标：手动补跑的老任务也要让前端显示 AI stage 行
        meta = _read_meta(task_id)
        meta["ai_feedback"] = True
        _update_task(task_id, meta_json=json.dumps(meta, ensure_ascii=False),
                     status="running", stage="ai", message="统计课堂数据",
                     stage_started_at=ts)

        # 正文从 segments 重建（单一事实源，不依赖 .txt 排版）
        transcript = "\n".join(
            f"{s.get('spk')}: {s['text']}" if s.get("spk") else s["text"]
            for s in segs
        )
        out_dir = Path(row["output_dir"])
        stats = compute_stats(segs)

        _update_task(task_id, progress=0.1, stage="ai", message="生成点评报告")
        report = chat(build_feedback_messages(transcript, stats), cfg)
        fb_path = out_dir / f"{task_id}_{stem}.feedback.md"
        fb_path.write_text(report, encoding="utf-8")

        _update_task(task_id, progress=0.55, stage="ai", message="生成优化逐字稿")
        polished = chat(build_polish_messages(transcript), cfg)
        po_path = out_dir / f"{task_id}_{stem}.polished.md"
        po_path.write_text(polished, encoding="utf-8")

        meta = _read_meta(task_id)
        meta["ai_files"] = {"feedback": fb_path.name, "polished": po_path.name}
        meta.pop("ai_error", None)
        _update_task(
            task_id, status="done", stage="done", progress=1.0,
            message="AI 反馈完成",
            ended_at=datetime.now().isoformat(timespec="seconds"),
            meta_json=json.dumps(meta, ensure_ascii=False),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[web] 任务 {task_id} AI 反馈失败")
        meta = _read_meta(task_id)
        meta["ai_error"] = f"{type(e).__name__}: {e}"
        _update_task(
            task_id, status="done", stage="ai", message="AI 反馈失败",
            ended_at=datetime.now().isoformat(timespec="seconds"),
            meta_json=json.dumps(meta, ensure_ascii=False),
        )


def _run_single_task(
    task_id: str,
    input_path: Path,
    output_dir: Path,
    config_path: str,
    diarize: bool,
    quality: str,
    ai_feedback: bool,
) -> None:
    """单任务 wrapper：pipeline → （done 且开开关）AI stage。"""
    _run_pipeline(task_id, input_path, output_dir, config_path, diarize, quality)
    with closing(_db()) as conn:
        status = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()["status"]
    if status == "done" and ai_feedback:
        _run_ai_stage(task_id, config_path)
```

`create_project` 改动（三处）：

1. 签名加字段（`quality` 之后）：

```python
    ai_feedback: bool = Form(False),
```

2. 多段分支：`meta = {"parts": names}` → `meta = {"parts": names, "ai_feedback": ai_feedback}`；线程启动改为：

```python
        threading.Thread(
            target=_run_multi_task,
            args=(task_id, saved, out_dir, config, diarize, quality, ai_feedback),
            daemon=True,
        ).start()
```

3. 单段分支：INSERT 语句加 `meta_json` 列，值 `json.dumps({"ai_feedback": ai_feedback}, ensure_ascii=False)`（False 也写，无既有用例断言 create 后 meta 为空——`test_create_single` 只断言 `not meta.get("parts")`）；线程目标换成 wrapper：

```python
        threading.Thread(
            target=_run_single_task,
            args=(task_id, saved[0], out_dir, config, diarize, quality, ai_feedback),
            daemon=True,
        ).start()
```

`_run_multi_task` 改动（两处）：

1. 签名末尾加 `ai_feedback: bool`。
2. `part_files` meta 更新之后（`_update_task(task_id, meta_json=...)` 与 `except` 之间）加：

```python
        # AI 反馈（可选）：拼接字幕写完后串行追加
        if ai_feedback:
            _run_ai_stage(task_id, config_path)
```

（失败兜底沿用外层 try/except：`_run_ai_stage` 自己吞异常写 ai_error，不会进 except 分支。）

- [ ] **Step 4: 跑测试确认通过**

Run: `py -3 -m pytest tests/test_web.py -q`
Expected: 全 PASS。注意既有用例若直接调 `_run_multi_task`（如 `test_run_multi_task_*`），按新签名补 `False` 实参——纯加参，断言不动。

- [ ] **Step 5: 全量 + 提交**

```bash
py -3 -m pytest tests/ -q
git add asr/web/__init__.py tests/test_web.py
git commit -m "feat(web): AI 反馈 stage——统计+点评+优化稿，失败不拖垮转写；上传开关自动串联

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: 手动补跑端点 + LLM 状态端点

**Files:**
- Modify: `asr/web/__init__.py`（`/api/tasks/{task_id}/feedback`、`/api/llm/status`，放 `batch_export` 之后）
- Test: `tests/test_web.py`（追加）

**Interfaces:**
- Consumes: Task 7 `_run_ai_stage`、Task 2 `llm_enabled`。
- Produces:
  - `GET /api/llm/status` → `200 {"enabled": bool, "model": str|null}`
  - `POST /api/tasks/{task_id}/feedback?config=config.yaml` → `202 {"status": "started"}`；404 任务不存在；409 任务 running / 非 done / LLM 未配置。触发时 status→running、stage→ai、清 `meta.ai_error`（Task 9 前端依赖）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_web.py`：

```python
# ---------------------------------------------------------------------------
# 手动补跑 + LLM 状态端点
# ---------------------------------------------------------------------------
def test_llm_status_disabled_by_default(client, isolated_db):
    """config.yaml 无 llm 节（默认仓库配置）→ enabled=false。"""
    r = client.get("/api/llm/status")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "model": None}


def test_llm_status_enabled(client, isolated_db, monkeypatch):
    from types import SimpleNamespace
    import asr.web as wm
    fake = SimpleNamespace(llm=SimpleNamespace(
        base_url="http://x/v1", api_key="", model="fake-m", timeout=5, max_tokens=1))
    monkeypatch.setattr(wm, "load_config", lambda *a, **kw: fake)
    r = client.get("/api/llm/status")
    assert r.json() == {"enabled": True, "model": "fake-m"}


def _mk_done_plain_task(isolated_db, task_id="fb1", status="done"):
    import asr.web as wm
    out_dir = wm.OUTPUT_ROOT / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    segs = json.dumps([{"start": 0.0, "end": 2.0, "text": "上课。"}],
                      ensure_ascii=False)
    with closing(wm._db()) as conn:
        conn.execute(
            "INSERT INTO tasks (id, name, filename, input_path, output_dir,"
            " status, progress, created_at, transcript)"
            " VALUES (?, '课', 'a.mp4', 'x', ?, ?, 1.0, '2026-10-21', ?)",
            (task_id, str(out_dir), status, segs),
        )


def test_feedback_trigger_202_and_clears_error(client, isolated_db, monkeypatch):
    import threading
    import asr.web as wm
    _mk_done_plain_task(isolated_db)
    with closing(wm._db()) as conn:
        conn.execute("UPDATE tasks SET meta_json=? WHERE id='fb1'",
                     (json.dumps({"ai_error": "旧错误"}, ensure_ascii=False),))
    done = threading.Event()
    monkeypatch.setattr(wm, "_run_ai_stage",
                        lambda tid, cfg: done.set())
    r = client.post("/api/tasks/fb1/feedback")
    assert r.status_code == 202 and r.json() == {"status": "started"}
    assert done.wait(5), "AI stage 线程没跑起来"
    with closing(wm._db()) as conn:
        row = conn.execute(
            "SELECT status, stage, meta_json FROM tasks WHERE id='fb1'").fetchone()
    assert row["status"] == "running" and row["stage"] == "ai"
    assert "ai_error" not in json.loads(row["meta_json"])


def test_feedback_trigger_rejections(client, isolated_db):
    _mk_done_plain_task(isolated_db, task_id="fb_running", status="running")
    _mk_done_plain_task(isolated_db, task_id="fb_pend", status="pending")
    assert client.post("/api/tasks/nope/feedback").status_code == 404
    assert client.post("/api/tasks/fb_running/feedback").status_code == 409
    assert client.post("/api/tasks/fb_pend/feedback").status_code == 409
    # done 任务但 LLM 未配置（仓库 config.yaml 无 llm 节）→ 409
    assert client.post("/api/tasks/fb1/feedback").status_code == 409
```

- [ ] **Step 2: 跑测试确认失败**

Run: `py -3 -m pytest tests/test_web.py -q -k "llm_status or feedback_trigger"`
Expected: FAIL，404 Not Found（路由不存在）

- [ ] **Step 3: 最小实现**

`asr/web/__init__.py`（`batch_export` 之后）：

```python
@app.get("/api/llm/status")
def llm_status(config: str = "config.yaml"):
    """LLM 可用性：前端据此显示/隐藏 AI 反馈入口。"""
    try:
        cfg = load_config(config, strict_models=False)
    except ConfigLoadError:
        return JSONResponse({"enabled": False, "model": None})
    return JSONResponse({"enabled": llm_enabled(cfg),
                         "model": cfg.llm.model or None})


@app.post("/api/tasks/{task_id}/feedback")
def trigger_ai_feedback(task_id: str, config: str = "config.yaml"):
    """对 done 任务手动补跑 AI 反馈（含上传没开开关的旧任务）。"""
    with closing(_db()) as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    if row["status"] == "running":
        raise HTTPException(409, "任务处理中，稍后再试")
    if row["status"] != "done":
        raise HTTPException(409, f"任务未完成: {row['status']}")
    if not llm_enabled(load_config(config, strict_models=False)):
        raise HTTPException(409, "LLM 未配置（config.yaml 缺 llm 节）")

    # 清旧错误 + 标 running，起线程；_run_ai_stage 自己管成功/失败终态
    meta = _read_meta(task_id)
    meta.pop("ai_error", None)
    _update_task(task_id, status="running", stage="ai", message="AI 反馈",
                 stage_started_at=datetime.now().isoformat(timespec="seconds"),
                 meta_json=json.dumps(meta, ensure_ascii=False))
    threading.Thread(target=_run_ai_stage, args=(task_id, config),
                     daemon=True).start()
    return JSONResponse({"status": "started"}, status_code=202)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `py -3 -m pytest tests/test_web.py -q`
Expected: 全 PASS

- [ ] **Step 5: 全量 + 提交**

```bash
py -3 -m pytest tests/ -q
git add asr/web/__init__.py tests/test_web.py
git commit -m "feat(web): AI 反馈手动补跑端点 + /api/llm/status

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 9: 前端——上传开关、stage 行、轮询条件、补跑按钮

**Files:**
- Modify: `asr/web/static/index.html`（diarize 复选框后加 AI 开关）
- Modify: `asr/web/static/list.js`（加载时查 LLM 状态；submit 带 `ai_feedback`）
- Modify: `asr/web/static/task.html`（stage 行 + 两个下载按钮 + AI 动作区）
- Modify: `asr/web/static/task.js`（STAGE_ORDER、updateUI、poll 停止条件、重试按钮）
- Modify: `asr/web/static/style.css`（`#ai-actions` 样式）
- Test: 无 JS 单测——验证走 Step 4 手动清单 + Task 10 E2E（Python 侧全量测试须保持绿）

**Interfaces:**
- Consumes: Task 8 的 `GET /api/llm/status`、`POST /api/tasks/{id}/feedback`；meta 键 `ai_feedback`/`ai_files`/`ai_error`；下载 fmt `feedback`/`polished`。
- Produces: 无（叶子节点）。

- [ ] **Step 1: index.html + list.js**

`index.html` 的 diarize `<label class="checkbox">` 之后加：

```html
          <label class="checkbox" id="ai-feedback-block" hidden>
            <input type="checkbox" id="ai-feedback" name="ai_feedback" />
            AI 反馈（点评 + 优化稿）
          </label>
```

`list.js`：文件末尾（提交监听之后）加 LLM 状态探测：

```javascript
// ---------------------------------------------------------------------------
// LLM 可用性：没配 llm 节就整个开关藏起来，不占位不报错
// ---------------------------------------------------------------------------
fetch("/api/llm/status")
  .then((r) => r.json())
  .then((s) => { if (s.enabled) $("ai-feedback-block").hidden = false; })
  .catch(() => {});
```

submit 处理器里 `form.append("diarize", ...)` 之后加一行：

```javascript
  form.append("ai_feedback", $("ai-feedback").checked ? "true" : "false");
```

- [ ] **Step 2: task.html + task.js + style.css**

`task.html` stage 列表 `write` 行与 `done` 行之间插：

```html
            <li data-stage="ai"><span class="mark">·</span><span class="label">AI 反馈</span><span class="msg"></span></li>
```

下载按钮区 `data-fmt="json"` 行后插：

```html
              <a class="btn" data-fmt="feedback" hidden title="AI 点评报告（教学反馈 + 稿件反馈）">AI 点评</a>
              <a class="btn" data-fmt="polished" hidden title="优化版逐字稿（校订 + 要点）">优化稿</a>
```

`#downloads` 区块之后（`</section>` 之前）加动作区：

```html
            <!-- AI 反馈动作：手动生成/重试（done 且 LLM 可用时出现） -->
            <div id="ai-actions" hidden>
              <div id="ai-error" class="error"></div>
              <button id="ai-retry" type="button" class="btn">生成 AI 反馈</button>
            </div>
```

`task.js` 四处改动：

1. `STAGE_ORDER` 改为：

```javascript
const STAGE_ORDER = ["merge", "probe", "extract", "denoise", "asr", "postprocess", "write", "ai", "done"];
```

2. 顶部状态变量区加 `let llmAvailable = false;`；`restoreLastTask();` 调用之前加：

```javascript
fetch("/api/llm/status")
  .then((r) => r.json())
  .then((s) => { llmAvailable = Boolean(s.enabled); })
  .catch(() => {});
```

3. `poll()` 的停止条件改为（AI 是 done 后追加的 stage，不能见 done 就停）：

```javascript
    // AI 反馈可能还在 done 之后跑：开关开了且没有产物/错误 → 继续轮询
    const aiPending = t.status === "done" && t.meta
      && t.meta.ai_feedback && !t.meta.ai_files && !t.meta.ai_error;
    if ((t.status === "done" && !aiPending) || t.status === "failed") {
      stopPolling();
    }
```

4. `updateUI()` 顶部（`const mergeLi = ...` 那块之后）加 AI stage 行显隐——**不放 done 分支**，AI stage 进行中任务处于 running，行得跟着每轮轮询走：

```javascript
  const hasAi = Boolean(t.meta && (t.meta.ai_feedback || t.meta.ai_error || t.meta.ai_files));
  const aiLi = document.querySelector('#stages li[data-stage="ai"]');
  if (aiLi) aiLi.style.display = hasAi ? "" : "none";
```

5. `updateUI()` 的 done 分支里 `renderPartDownloads(t.meta)` 之后加 `renderAiUi(t);`，并在 `renderPartDownloads` 函数之后新增（只管按钮区，stage 行显隐在第 4 条）：

```javascript
/**
 * AI 反馈按钮区（仅 done 分支调用）：两个下载按钮显隐 + 手动生成/重试。
 * done + LLM 可用 +（无产物 或 有错误）→ 显示按钮；有产物无错 → 只留下载。
 */
function renderAiUi(t) {
  const meta = t.meta || {};
  const fbBtn = document.querySelector('#downloads .btn[data-fmt="feedback"]');
  const poBtn = document.querySelector('#downloads .btn[data-fmt="polished"]');
  const hasFiles = Boolean(meta.ai_files);
  if (fbBtn) fbBtn.hidden = !hasFiles;
  if (poBtn) poBtn.hidden = !hasFiles;

  const box = $("ai-actions");
  if (t.status !== "done" || !llmAvailable || hasFiles) { box.hidden = true; return; }
  box.hidden = false;
  const errBox = $("ai-error");
  if (meta.ai_error) {
    errBox.hidden = false;
    errBox.textContent = `AI 反馈失败：${meta.ai_error}`;
    $("ai-retry").textContent = "重试 AI 反馈";
  } else {
    errBox.hidden = true;
    $("ai-retry").textContent = "生成 AI 反馈";
  }
}

$("ai-retry").addEventListener("click", async () => {
  $("ai-retry").disabled = true;
  try {
    const resp = await fetch(`/api/tasks/${currentTaskId}/feedback`, { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `触发失败: ${resp.status}`);
    }
    startPolling();   // 恢复轮询，看 AI stage 进度
  } catch (err) {
    alert(err.message);
  } finally {
    $("ai-retry").disabled = false;
  }
});
```

`style.css` 末尾加：

```css
/* AI 反馈动作区（手动生成/重试） */
#ai-actions {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
}
```

- [ ] **Step 3: Python 全量保持绿**

Run: `py -3 -m pytest tests/ -q`
Expected: 全 PASS（前端改动不触 Python 测试；`test_static_files` 断言的关键词 `loadProjects`/`poll`/`theme-toggle` 均未被删）

- [ ] **Step 4: 手动验证清单（起服务器过一遍）**

启动 `py -3 -m asr.web`（注意先 `netstat -ano | grep :8901` 清残留进程），浏览器验证：

1. 无 llm 配置：上传页**没有** AI 开关；任务页无 AI stage 行、无按钮。
2. 临时启用假 LLM 配置（见 Task 10 的 fake_llm.py）后再启动：上传页出现开关；勾选上传一个短音频，任务页出现"AI 反馈" stage 行，转写完成后进度条进入 AI 阶段，完成后出现"AI 点评"/"优化稿"两个下载按钮。
3. 对一个**没开开关**的已完成任务：出现"生成 AI 反馈"按钮，点击后 stage 行出现、开始轮询。
4. 停掉 fake LLM 再点重试：出现错误条 + "重试 AI 反馈"按钮，任务状态仍是"完成"（不是失败）。

- [ ] **Step 5: 提交**

```bash
git add asr/web/static/index.html asr/web/static/list.js asr/web/static/task.html asr/web/static/task.js asr/web/static/style.css
git commit -m "feat(web): AI 反馈前端——上传开关、stage 行、轮询条件、手动生成/重试

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 10: E2E——假 LLM 服务器跑通全流程

**Files:**
- Create: `%TEMP%/ai_e2e/fake_llm.py`（假 OpenAI 端点，不进仓库）
- Create: `%TEMP%/ai_e2e/run_srv.py`（服务器 wrapper，stdout 落文件——沿用 merge E2E 的管道教训）
- Create: `%TEMP%/ai_e2e/automation.py`（Playwright 脚本，不进仓库）
- Test: 一次性 E2E（不进 pytest）

**Interfaces:**
- Consumes: Task 1-9 全部。
- Produces: E2E 通过的证据（控制台输出 `AI E2E ALL PASS`）；真 LLM 手动 runbook 备注。

- [ ] **Step 1: 假 LLM 服务器**

`fake_llm.py`（区分点评/优化稿两类调用，返回可断言的固定文本）：

```python
"""假 OpenAI 兼容端点：按 system prompt 区分两类调用，返回固定内容。"""
from fastapi import FastAPI, Request

app = FastAPI()
CALLS = []


@app.post("/v1/chat/completions")
async def completions(req: Request):
    body = await req.json()
    system = body["messages"][0]["content"]
    CALLS.append(system[:30])
    if "评课专家" in system:
        content = "# AI 课堂反馈\n\n## 一、教学反馈\n### 讲解结构\n好。依据：〈我们开始上课〉\n## 三、课堂数据\n| 指标 | 值 |\n|---|---|\n| 语速 | 300 |\n"
    else:
        content = "# 优化稿\n\n我们开始上课。\n\n## 本课要点\n- 要点一\n"
    return {"choices": [{"message": {"content": content}}]}
```

启动：`py -3 -m uvicorn fake_llm:app --port 8902`（工作目录 `%TEMP%/ai_e2e`）。

- [ ] **Step 2: 带 llm 配置的测试 config + 服务器 wrapper**

测试 config（`%TEMP%/ai_e2e/config.ai.yaml`）：复制仓库 `config.yaml` 全文，末尾追加：

```yaml
llm:
  base_url: http://127.0.0.1:8902/v1
  model: fake
```

`run_srv.py`（stdout 落文件——FunASR/tqdm 写无人读的 PIPE 在 Windows 上会炸线程，这是上个 E2E 踩过的坑）：

```python
import sys
log = open(r"<TEMP>\ai_e2e\server.log", "w", encoding="utf-8", errors="replace")
sys.stdout = log
sys.stderr = log
sys.path.insert(0, r"E:\classroom-asr-ascii")
from asr.web import main
main()   # --port 8901
```

- [ ] **Step 3: automation 脚本（断言清单）**

`automation.py` 骨架（沿用 merge E2E 的 wait_done/download_text 模式）：

```python
# 断言清单（全过输出 AI E2E ALL PASS）：
# 1. 上传页：#ai-feedback-block 可见，勾选后提交（page.evaluate 直接 fetch
#    /api/projects，表单带 config=config.ai.yaml 路径 + ai_feedback=true + files=[t3.mp3]）
# 2. 任务页：stage 列表出现"AI 反馈"行（fake LLM 秒回，不要求命中 active 瞬间）
# 3. done 后：#downloads 出现 [data-fmt=feedback] 与 [data-fmt=polished]，
#    download_text 内容分别含 "AI 课堂反馈" 与 "本课要点"
# 4. 单文件回归：再建一个不开 AI 的任务 → 无 AI stage 行、无 AI 按钮、无 AI 下载
# 5. 手动补跑：对任务 4 的任务 POST /api/tasks/{id}/feedback → 轮询到 done →
#    AI 下载出现
# 6. 浏览器 console 无 error
# 7. DELETE 清理两个测试任务
```

执行顺序：先起 fake_llm（8902），再起 run_srv（8901，启动前 `netstat -ano | grep :8901` 清残留），最后 `py -3 automation.py`。

- [ ] **Step 4: 跑通 + 真 LLM 备注**

Expected: 输出 `AI E2E ALL PASS`。若有断言失败，先查 `server.log`（Errno 22 = PIPE 问题，确认走的是 run_srv.py；10048 = 端口被旧进程占）。

真 LLM 手动 runbook（可选，不进自动化）：`config.yaml` 启用 llm 节（GLM/DeepSeek/Ollama 任一），重启服务器，勾选 AI 反馈上传一段真实课程音频，人工阅读点评报告与优化稿的质量——prompt 调优在这一步做，改 `asr/feedback.py` 的措辞后重跑即可。

- [ ] **Step 5: 无仓库提交（E2E 脚本在 TEMP，不进 git）**

若 Task 9 有残留未提交文件，此处清点提交；否则本任务无 commit。
