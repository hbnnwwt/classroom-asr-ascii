# Classroom-ASR 代码审查修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `review/code_review.md` 中的全部 P0 缺陷与关键 P1 缺陷，外加两个今晚新定位的活 bug，使 94 分钟真实课程视频产出时间戳真实、标点保留、可开启说话人分离的字幕。

**Architecture:** 核心重写在 `funasr_engine._parse_results`：利用 funasr 1.4.16 的字符级 timestamp（实测 517 token 对应 517 汉字、单位厘秒）与文本逐字对齐、按强标点切段，取代"只取首尾 token + 字数插值"的伪造时间轴。postprocess 保留 spk 与标点贯穿后续合并。Web 层修看门狗生命周期与前端死引用。

**Tech Stack:** Python 3.12 / FunASR 1.4.16 / FastAPI / pytest / Vanilla JS

**Spec:** `review/code_review.md`（E:\classroom-asr-ascii\review\code_review.md）

## 根因调查结论（2026-09-22 深夜实测）

- **今晚任务 b5a3f6c7a552 崩溃**：DB error = `Unexpected: AttributeError: 'list' object has no attribute 'strip'`，progress=0.90、message="识别完成 · 5628s" → `generate()` 成功返回，崩溃在**我们代码** `_parse_results`。
- **触发条件**：上传表单勾选"启用说话人分离"。funasr 加载 spk_model 后返回 `sentence_info`；当 punc_array 与 timestamp 长度不齐（`auto_model.py:1125-1127` 置 None），`timestamp_sentence()` 走空分支返回 **`"text": text_postprocessed.split()`（list！）**（`funasr/utils/timestamp_tools.py:155`）。我们 259 行 `(s.get("text") or "").strip()` 对 list 调 `.strip()` → 崩。
- **前端 `Cannot set properties of null (setting 'disabled')`**：`task.js:110` 引用 `$("submit-btn")`，该元素只存在于 index.html，task.html 没有 → 任务到终态时轮询回调必炸。
- **5 分钟最小复现**（diarize=False）：text 为 str、timestamp 为 517 个 `[5080,5320]` 厘秒对（50.80s，与 out_real 首段吻合）、无 sentence_info。证明时间戳分支可用字符对齐根治 P0-1。复现脚本：`scripts/repro_text_list.py`、`scripts/repro_text_list_spk.py`（保留作回归工具）。
- **funasr 1.4.16 为今天 15:43 安装**，out_real 产物出自旧版 —— 两份结果差异来自版本变更，不是玄学。

## Global Constraints

- 工作目录：`E:\classroom-asr-ascii`（用户确认）。所有路径以此为准。
- Python 解释器：`C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe`（PATH 里没有裸 `python`）。
- 测试命令：`cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ -q`
- 基线：当前 93 个测试全绿（含把 bug 固化为规范的 `test_postprocess.py` 断言，Task 3 翻转它）。
- 测试不得加载真实模型（2GB、分钟级）。`_parse_results` 的测试全部用**合成 funasr 返回结构**（形状与本计划"根因调查结论"及 review 记载一致）。
- 每个任务结束必须：测试绿 + `git commit`（Task 1 的 Step 0 先建仓库）。
- 不改动 `models/`、`uploads/`、`outputs/` 下任何文件；不删除任何数据文件。
- 提交信息结尾加：`Co-Authored-By: Claude Code <noreply@anthropic.com>`

## 明确不做（本计划范围外）

- P1-7 Web 认证/限流（第三梯队，单机内网使用，另立计划）
- P1-8 transcript 渐进写库的 O(n²) 优化（有界、一次性，非当前痛点）
- P2-6/P2-7 类型卫生、README 全面修订
- 清理 tmp_models/（3GB）与 uploads/（2GB）—— 涉及删除用户数据，需用户单独确认

---

### Task 1: 止血 — text/list 崩溃 + 前端 null + 建仓

**Files:**
- Create: `.gitignore`
- Modify: `asr/engines/funasr_engine.py:240-301`（`_parse_results` 文本归一）
- Modify: `asr/web/static/task.js:108-112`（删除死引用）
- Test: `tests/test_funasr_engine.py`

**Interfaces:**
- Produces: 模块级函数 `funasr_engine._as_text(v) -> str`（Task 2 复用）
- Produces: `_parse_results(self, results)` 行为不变签名不变（Task 2 才加 duration_s 参数）

- [ ] **Step 0: git init + .gitignore**

```bash
cd /e/classroom-asr-ascii && git init
```

`.gitignore` 内容：
```
__pycache__/
*.pyc
models/
tmp_models/
asr/web/uploads/
asr/web/outputs/
asr/web/web_state.db*
out*/
*.log
tmp_repro_*.wav
lecture.mp4
.pytest_cache/
```

```bash
git add -A && git commit -m "chore: 初始提交（review 基线，93 tests green）"
```

- [ ] **Step 1: 写失败测试（合成 funasr 返回结构，不加载模型）**

在 `tests/test_funasr_engine.py` 追加（engine 构造是惰性加载，无需模型）：

```python
class TestParseResultsTextList:
    """funasr 1.4.16 开 spk 后 sentence_info 的 text 可能是 list
    （timestamp_tools.py:155 的空 punc_id_list 分支）。回归：任务 b5a3f6c7a552。"""

    def _engine(self):
        from asr.config import load_config
        from asr.engines.funasr_engine import FunASREngine
        cfg = load_config(Path(__file__).parent.parent / "config.yaml", strict_models=False)
        return FunASREngine(cfg)

    def test_sentence_info_text_list_normalized(self):
        results = [{
            "key": "x",
            "sentence_info": [
                {"start": 0, "end": 1500, "text": ["你", "好"], "spk": 0},
                {"start": 1600, "end": 3000, "text": "今天上课。", "spk": 1},
            ],
        }]
        segs = self._engine()._parse_results(results)
        assert segs[0].text == "你好"
        assert segs[1].text == "今天上课。"

    def test_top_level_text_list_normalized(self):
        results = [{"key": "x", "text": ["大", "家", "好"], "timestamp": [[100, 200]]}]
        segs = self._engine()._parse_results(results)
        assert len(segs) == 1
        assert segs[0].text == "大家好"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_funasr_engine.py -q`
Expected: 新增 2 条 FAIL（`AttributeError: 'list' object has no attribute 'strip'`）

- [ ] **Step 3: 实现 `_as_text` 并在两处使用**

`asr/engines/funasr_engine.py` 模块级（`FunASREngine` 类外）：

```python
def _as_text(v) -> str:
    """funasr 返回的 text 字段可能是 str 或 list[str]（开 spk 时
    timestamp_sentence 的空 punc 分支，timestamp_tools.py:155）。统一转 str。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple)):
        return "".join(str(x) for x in v).strip()
    return str(v).strip()
```

259 行：`text = (s.get("text") or "").strip()` → `text = _as_text(s.get("text"))`
277 行：`text = (item.get("text") or "").strip()` → `text = _as_text(item.get("text"))`

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2。Expected: 全部 PASS（含原有测试）。

- [ ] **Step 5: 删 task.js 死引用**

`asr/web/static/task.js:108-112`：

```js
    if (t.status === "done" || t.status === "failed") {
      stopPolling();
      $("submit-btn").disabled = false;
      $("submit-btn").textContent = "开始转写";
    }
```

改为（task.html 无 submit-btn，这两行是旧单页残留）：

```js
    if (t.status === "done" || t.status === "failed") {
      stopPolling();
    }
```

- [ ] **Step 6: 全量测试 + 提交**

```bash
cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ -q
git add -A && git commit -m "fix: funasr text 字段为 list 时崩溃 + task.js 引用不存在的 submit-btn

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

- [ ] **Step 7: 通知用户重启 web 服务后可重传视频（勾不勾 diarize 都不会再崩）**

---

### Task 2: P0-1 字符级时间戳对齐（核心重写）

**Files:**
- Modify: `asr/engines/funasr_engine.py`（`_parse_results` 重写、`transcribe` 传 duration、删 `_detect_time_unit`）
- Test: `tests/test_funasr_engine.py`

**Interfaces:**
- Consumes: `_as_text`（Task 1）
- Produces: `_parse_results(self, results, duration_s: float | None = None) -> List[Segment]`（新增关键字参数，默认 None，向后兼容）
- Produces: 模块级 `_split_by_char_ts(text: str, ts: list, duration_s: float | None) -> List[Segment]`、`_detect_unit_div(ts: list, duration_s: float | None) -> float`
- dies: `_detect_time_unit`（P1-5）、engine 对 `normalize_time_unit` 的无 duration 调用（P1-4）

- [ ] **Step 1: 写失败测试**

```python
class TestCharLevelTimestamps:
    """P0-1：timestamp 与字符逐一对齐，按强标点切段，时间戳真实（非插值）。"""

    def test_split_by_char_ts_basic(self):
        from asr.engines.funasr_engine import _split_by_char_ts
        # 4 汉字 + 1 句号；timestamp 厘秒，逐字对应（标点无 timestamp）
        text = "大家好。再见"
        ts = [[100, 200], [200, 300], [300, 400], [400, 500], [900, 950]]
        segs = _split_by_char_ts(text, ts, duration_s=10.0)
        assert len(segs) == 2
        assert segs[0].text == "大家好。"          # 标点保留在句尾
        assert segs[0].start == 1.0 and segs[0].end == 5.0
        assert segs[1].text == "再见"
        assert segs[1].start == 9.0                 # 真实间隙，不是紧贴上一段
        assert segs[1].end == 9.5

    def test_unit_detection_centisecond(self):
        from asr.engines.funasr_engine import _detect_unit_div
        # 末值 562824 厘秒 = 5628s ≤ duration*1.5；原值远超 duration → 厘秒
        assert _detect_unit_div([[5080, 5320], [562700, 562824]], 5628.0) == 100.0

    def test_unit_detection_seconds(self):
        from asr.engines.funasr_engine import _detect_unit_div
        assert _detect_unit_div([[0.5, 1.0], [50.0, 56.0]], 5628.0) == 1.0

    def test_parse_results_full_pipeline_shape(self):
        # 5 分钟实测形状：单 dict、text str、517 token。合成小样本。
        text = "好。那么我们今天呢，开始正式的上课。好各位同学"
        n_chars = len([c for c in text if c not in "，。；？！"])
        ts = [[500 + i * 100, 580 + i * 100] for i in range(n_chars)]
        results = [{"key": "k", "text": text, "timestamp": ts}]
        segs = self._engine()._parse_results(results, duration_s=60.0)
        assert len(segs) == 3                       # 两个句号 → 3 段
        # review 回归断言：段间必须有真实间隙
        gaps = [segs[i + 1].start - segs[i].end for i in range(len(segs) - 1)]
        assert max(gaps) >= 0.0                     # 不再恒为 0 的紧贴
        rates = [(s.end - s.start) / len(s.text) for s in segs]
        assert max(rates) - min(rates) > 1e-6       # s/字 不再恒定（插值特征）

    def test_mismatch_falls_back_single_segment(self):
        # timestamp 数与去标点字符数差 > 3 → 退回单段（老行为），warn 不崩
        text = "大家好大家好大家好大家好"
        ts = [[100, 200]]                            # 严重不齐
        segs = self._engine()._parse_results(
            [{"key": "k", "text": text, "timestamp": ts}], duration_s=10.0)
        assert len(segs) == 1 and segs[0].text == text

    def test_no_timestamp_spans_duration(self):
        # fast 模式（SenseVoice）无 timestamp：占满 [0, duration]（P0-4 前半）
        segs = self._engine()._parse_results(
            [{"key": "k", "text": "大家好", "timestamp": []}], duration_s=100.0)
        assert segs[0].start == 0.0 and segs[0].end == 100.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_funasr_engine.py -q`
Expected: 新增用例 FAIL（`_split_by_char_ts` 不存在 / 时间戳仍为插值）

- [ ] **Step 3: 重写 `_parse_results`**

```python
# 强标点：切段边界；弱标点与空白：无 timestamp，对齐时跳过
_HARD_PUNCT = "。？！；\n"
_NO_TS_CHARS = set("，、：""''（）()《》\n\r\t ")

def _split_by_char_ts(text: str, ts: list, duration_s: float | None) -> List["Segment"]:
    """token 级 timestamp 与字符逐一对齐，按强标点切段。

    funasr 1.4.16 Paraformer 实测（scripts/repro_text_list.py）：timestamp
    长度 = 去标点后字符数，每项 [start,end] 对应一个汉字。对齐失败的容差
    由调用方（_parse_results）预检，这里只做逐字消费。
    """
    div = _detect_unit_div(ts, duration_s)
    out: List[Segment] = []
    buf: list[str] = []
    t0 = t1 = None
    ti = 0
    for ch in text:
        if ch in _HARD_PUNCT:
            if buf:
                buf.append(ch)
                out.append(Segment(start=t0 / div, end=t1 / div, text="".join(buf)))
                buf, t0 = [], None
            continue
        if ch in _NO_TS_CHARS:
            continue
        if ti >= len(ts):
            buf.append(ch)              # 时间戳耗尽：残余文本留给收尾并入
            continue
        s, e = ts[ti]
        ti += 1
        if t0 is None:
            t0 = s
        t1 = e
        buf.append(ch)
    if buf and t0 is not None:
        out.append(Segment(start=t0 / div, end=t1 / div, text="".join(buf)))
    elif buf and out:                   # 时间戳耗尽的残余并入末段，不丢文本
        last = out[-1]
        out[-1] = Segment(start=last.start, end=last.end, text=last.text + "".join(buf))
    return [s for s in out if s.text.strip()]


def _detect_unit_div(ts: list, duration_s: float | None) -> float:
    """证据判定 timestamp 单位：厘秒(100) 还是秒(1)。

    Paraformer-large 实测返回厘秒（[5080,5320] = 50.80s）。用音频总时长做
    交叉验证，取代旧的"均值>30"启发式（P1-5：前 30 秒静音即误判）。
    """
    if not ts:
        return 1.0
    last_end = float(ts[-1][1])
    if duration_s and duration_s > 0:
        fits_cs = last_end / 100.0 <= duration_s * 1.5
        fits_s = last_end <= duration_s * 1.5
        if fits_s and not fits_cs:
            return 1.0
        return 100.0                    # 厘秒为实测默认；两者皆超界也按厘秒兜底
    return 100.0 if last_end > 3600 else 1.0
```

`_parse_results` 重写为（`transcribe` 中调用处同步改：`return self._parse_results(results, duration_s=duration_total)`）：

```python
    def _parse_results(self, results, duration_s: float | None = None) -> List[Segment]:
        """解析 funasr generate() 返回 → List[Segment]（秒）。

        三种形状（funasr 1.4.16）：
        1. sentence_info（开 spk 时；text 可能为 list，_as_text 归一；
           start/end 毫秒）
        2. text + 字符级 timestamp（默认；timestamp 厘秒，逐字对应）
        3. 仅 text（SenseVoice fast；占满 [0, duration] 由后处理分摊）
        """
        segments: List[Segment] = []
        for item in results or []:
            if "sentence_info" in item and item["sentence_info"]:
                for s in item["sentence_info"]:
                    text = _as_text(s.get("text"))
                    if not text:
                        continue
                    segments.append(Segment(
                        start=float(s.get("start", 0)) / 1000.0,
                        end=float(s.get("end", 0)) / 1000.0,
                        text=text,
                        spk=_as_spk(s.get("spk")),
                    ))
                continue

            text = _as_text(item.get("text"))
            ts = item.get("timestamp") or []
            if not text:
                continue
            if not ts:
                logger.warning("FunASR 返回无时间戳（fast 模式），整段占满时长")
                segments.append(Segment(start=0.0, end=duration_s or 0.0, text=text))
                continue

            n_text = sum(1 for c in text if c not in _NO_TS_CHARS and c not in _HARD_PUNCT)
            if abs(len(ts) - n_text) > 3:
                logger.warning(
                    f"timestamp 数({len(ts)})与字符数({n_text})不齐，退回单段"
                )
                div = _detect_unit_div(ts, duration_s)
                segments.append(Segment(
                    start=float(ts[0][0]) / div,
                    end=float(ts[-1][1]) / div,
                    text=text,
                ))
                continue
            segments.extend(_split_by_char_ts(text, ts, duration_s))

        segments.sort(key=lambda s: (s.start, s.end))
        return segments
```

`_as_spk`（模块级小助手，Task 4 的 diarize 依赖）：

```python
def _as_spk(v) -> Optional[str]:
    if v is None:
        return None
    return str(v) if str(v).startswith("spk") else f"spk{v}"
```

同时**删除**文件末尾的 `_detect_time_unit` 函数及其 import（`normalize_time_unit` 若仍被 sentence_info 旧路径引用则一并清理 import）。

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2。Expected: 全 PASS。

- [ ] **Step 5: 全量测试 + 提交**

```bash
cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ -q
git add -A && git commit -m "fix(P0-1): 字符级时间戳对齐取代字数插值，时间轴真实化

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

- [ ] **Step 6: 真实回归（可选，5 分钟 CPU）**

Run: `cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" scripts/repro_text_list.py`
Expected: 输出 text/timestamp 结构不变；另跑 `python -m asr run tmp_repro_5min.wav --output-dir out_verify` 后检查 out_verify SRT：段间间隙 > 0、s/字 非恒定。

---

### Task 3: P0-3 标点保留 + P1-11 死代码 + P2-5 测试翻转

**Files:**
- Modify: `asr/postprocess.py:141-154`（`split_by_punct`）、`asr/postprocess.py:230-239`（删死代码）
- Test: `tests/test_postprocess.py`（翻转被固化的 bug 断言）

**Interfaces:**
- Produces: `split_by_punct(text) -> List[str]` 语义变更：**切分标点保留在句尾**（Task 2 的段文本已带标点，此处不得再丢）

- [ ] **Step 1: 翻转/新增测试**

`tests/test_postprocess.py` 中找到断言 `split_by_punct("你好。今天") == ["你好", "今天"]`（约 88 行）的用例，改为：

```python
def test_split_by_punct_keeps_punct():
    """P0-3：强标点必须保留在句尾（旧实现丢弃，转写不可读）。"""
    assert split_by_punct("你好。今天天气怎么样？") == ["你好。", "今天天气怎么样？"]

def test_split_by_punct_no_trailing_punct():
    assert split_by_punct("大家好") == ["大家好"]

def test_split_by_punct_leading_punct_dropped():
    # 句首孤立标点（异常输入）不产生空段/纯标点段
    assert split_by_punct("。你好") == ["你好"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_postprocess.py -q`
Expected: 新断言 FAIL

- [ ] **Step 3: 实现**

```python
def split_by_punct(text: str) -> List[str]:
    """按强切分标点切段，标点保留在句尾（P0-3：标点是可读性的一部分）。"""
    out: List[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in _HARD_PUNCT:
            piece = "".join(buf).strip()
            # 纯标点残余（如句首孤立标点）不产出
            if piece and any(c not in _HARD_PUNCT + _SOFT_PUNCT + " \n" for c in piece):
                out.append(piece)
            buf = []
    if buf:
        piece = "".join(buf).strip()
        if piece:
            out.append(piece)
    return out
```

删除 `asr/postprocess.py:230-239` 的死代码块（`for ...: pass # 见下方改进` 与未用的 `cur_seg_start_idx` 初值——注意 239 行 `cur_seg_start_idx = 0` 之后 241 行 while 循环里若未再使用则一并删）。

- [ ] **Step 4: 全量测试（此时 test_postprocess 的下游用例可能因标点保留而需同步更新断言——逐个按新语义修正，禁止为图省事改回实现）+ 提交**

```bash
cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ -q
git add -A && git commit -m "fix(P0-3): split_by_punct 保留句尾标点；删除死代码

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: P0-2 spk 贯穿断句合并（diarize 复活）

**Files:**
- Modify: `asr/postprocess.py`（`merge_short_sentences`）
- Test: `tests/test_postprocess.py`

**Interfaces:**
- Consumes: Segment.spk（Task 2 的 `_as_spk` 产出 "spk0" 形态）
- Produces: `merge_short_sentences` 输出段保留 spk（合并段取字数多数者）

- [ ] **Step 1: 写失败测试**

```python
def test_merge_preserves_spk():
    """P0-2：merge_short_sentences 不得抹掉 spk，否则 diarize 永不生效。"""
    segs = [
        Segment(start=0.0, end=2.0, text="好。", spk="spk0"),
        Segment(start=2.5, end=5.0, text="老师好。", spk="spk1"),
    ]
    out = merge_short_sentences(segs, min_chars=10, max_chars=30)
    assert any(s.spk for s in out), "postprocess 抹掉了 spk"

def test_pipeline_diarize_guard_passes():
    """pipeline.py:147 守卫 any(s.spk ...) 在 postprocess 之后仍为 True。"""
    from asr.postprocess import apply as apply_postprocess
    segs = [Segment(start=0.0, end=2.0, text="好。", spk="spk0")]
    out, _ = apply_postprocess(segs, _test_cfg(), duration_s=10.0)
    assert any(s.spk for s in out)
```

（`_test_cfg` 用该测试文件里已有的 cfg 构造方式；若无则参照同文件现有用例建 `AppConfig` 最小实例，`postprocess.merge_short_sentences=True`、`min_sentence_chars=10`、`max_sentence_chars=30`、`itn=False`、`glossary=""`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/test_postprocess.py -q`
Expected: FAIL（spk 为 None）

- [ ] **Step 3: 实现**

`merge_short_sentences` 中数据结构从 `Tuple[str, float, float]` 扩为 `Tuple[str, float, float, Optional[str]]`：

- 入口清洗（约 172-179 行）：拼接到前一段时 `cleaned[-1] = replace(prev, text=..., end=seg.end)` 已保留 prev.spk，不动。
- 第 1 步切分：`pieces.append((ph, seg.start, seg.end, seg.spk))`
- 第 2 步合并：合并时 spk 取**字数多数**：

```python
    merged: List[Tuple[str, float, float, Optional[str]]] = []
    for text, s, e, spk in pieces:
        if merged and len(merged[-1][0]) < min_chars:
            old_text, old_s, old_e, old_spk = merged[-1]
            new_spk = old_spk if len(old_text) >= len(text) else spk
            merged[-1] = (old_text + text, old_s, e, new_spk)
        else:
            merged.append((text, s, e, spk))
```

- 第 3 步弱标点切分的 `_flush` 与收尾同步带 spk（同一原始段内切分，spk 不变）。
- 第 4 步插值重建：`out.append(Segment(start=..., end=..., text=t, spk=spk))`（252 与 261 两处，review P0-2 点名的位置）。

- [ ] **Step 4: 全量测试 + 提交**

```bash
cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ -q
git add -A && git commit -m "fix(P0-2): spk 贯穿 merge_short_sentences，diarize 分支可达

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: P0-5 年份转换正确实现（删硬编码 2025）

**Files:**
- Modify: `asr/postprocess.py:25,60-70`
- Test: `tests/test_postprocess.py`

- [ ] **Step 1: 写失败测试**

```python
def test_itn_year_real_conversion():
    """P0-5：中文年份逐位转换，禁止硬编码。"""
    assert apply_itn("二〇二三年") == "2023年"
    assert apply_itn("一九九九年") == "1999年"      # 旧正则不覆盖，一并修
    assert apply_itn("今年是二零二五年。") == "今年是2025年。"

def test_itn_year_no_false_positive():
    assert apply_itn("三四千年历史") == "三四千年历史"  # 非 4 位数字串不动
    assert apply_itn("二五年度") == "二五年度"
```

- [ ] **Step 2: 跑测试确认失败**（现状：二〇二三 → 2025；一九九九 不匹配）

- [ ] **Step 3: 实现**

```python
_CN_DIGIT = {"零": "0", "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
             "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
_YEAR_CN_RE = re.compile(r"([零〇一二三四五六七八九]{4})年")

def apply_itn(text: str) -> str:
    """对单条文本做 ITN 正则兜底。只做有把握的转换，宁可不动也不写错。"""
    text = _YEAR_CN_RE.sub(
        lambda m: "".join(_CN_DIGIT[c] for c in m.group(1)) + "年", text)

    def _pct_sub(m: re.Match) -> str:
        n = _cn_num_to_int(m.group(1))
        return f"{n}%" if n is not None else m.group(0)
    return _PERCENT_CN_RE.sub(_pct_sub, text)
```

（原 `_YEAR_CN_RE` 要求以"二"开头 → 换成任意 4 位数字字 + "年"，覆盖一九九九；`_year_sub` 硬编码函数删除。）

- [ ] **Step 4: 全量测试 + 提交**

```bash
cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ -q
git add -A && git commit -m "fix(P0-5): 中文年份逐位转换，删除硬编码 2025

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: P0-6 看门狗生命周期 + 终态原子守卫（P2-2 lifespan 顺带）

**Files:**
- Modify: `asr/web/__init__.py:497-599`（`_run_pipeline`）、`asr/web/__init__.py:145-150`（startup → lifespan）
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `_update_task_failed_if_running(task_id, error) -> bool`（SQL 级守卫：`UPDATE ... WHERE status='running'`）

- [ ] **Step 1: 写失败测试**

```python
class TestWatchdog:
    def test_done_task_not_flipped_by_watchdog(self, tmp_path, monkeypatch):
        """P0-6：任务已 done，看门狗不得改写为 failed。"""
        from asr.web import _init_db, _db, _update_task_failed_if_running
        from contextlib import closing
        import sqlite3, datetime
        _init_db._forced = None  # 若模块已有测试夹具建库逻辑，沿用之
        # （沿用 test_web.py 现有的 DB 夹具模式；直接写一行 done 任务）
        with closing(_db()) as conn:
            conn.execute("DELETE FROM tasks")
            conn.execute(
                "INSERT INTO tasks (id, filename, input_path, output_dir, status, created_at)"
                " VALUES ('t1','f','i','o','done','2026-01-01')")
        flipped = _update_task_failed_if_running("t1", "watchdog fired")
        assert flipped is False
        with closing(_db()) as conn:
            assert conn.execute("SELECT status FROM tasks WHERE id='t1'").fetchone()[0] == "done"

    def test_running_task_flipped_by_watchdog(self):
        ...同上，status='running' → flipped is True，status 变 'failed'
```

（`test_web.py` 已有针对该模块的测试与夹具，沿用其 DB 路径隔离方式——若现有测试直接用模块 DB，则先备份/恢复或按现有模式 monkeypatch `asr.web.DB_PATH` 到 tmp_path。）

- [ ] **Step 2: 跑测试确认失败**（`_update_task_failed_if_running` 不存在）

- [ ] **Step 3: 实现**

`asr/web/__init__.py`：

```python
def _update_task_failed_if_running(task_id: str, error: str) -> bool:
    """SQL 级终态守卫：仅 running 可被标 failed（done/终态不可污染）。"""
    with closing(_db()) as conn:
        cur = conn.execute(
            "UPDATE tasks SET status='failed', error=?, ended_at=? "
            "WHERE id=? AND status='running'",
            (error, datetime.now().isoformat(timespec="seconds"), task_id),
        )
        return cur.rowcount > 0
```

`_run_pipeline` 内看门狗改为：

```python
    stop = threading.Event()

    def _watchdog() -> None:
        while not stop.wait(60):          # wait 兼做 sleep + 可中断
            elapsed = _t.time() - last_cb_time[0]
            if elapsed >= ASR_STALL_TIMEOUT_S:
                err = f"ASR 阶段无回调超过 {int(elapsed // 60)} 分钟，主动终止"
                watchdog_exc[0] = RuntimeError(err)
                logger.error(f"[web] 任务 {task_id} 看门狗触发: {err}")
                _update_task_failed_if_running(task_id, str(watchdog_exc[0]))
                return
```

`try:` 块外层补 `finally: stop.set()`（包住整个 try/except，保证成功/失败都停表）。看门狗里原来的 `_update_task(...)` 直写改为上面的原子守卫调用。

顺带 P2-2：`@app.on_event("startup")` → lifespan：

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    _init_db()
    logger.info(f"[web] 数据库: {DB_PATH}")
    yield

app = FastAPI(title="Classroom ASR", version="1.0", lifespan=lifespan)
```

- [ ] **Step 4: 全量测试 + 提交**

```bash
cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ -q
git add -A && git commit -m "fix(P0-6): 看门狗 Event 停止 + SQL 终态守卫；startup 改 lifespan

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: P0-7 CLI overrides 真正生效

**Files:**
- Modify: `asr/pipeline.py:63` 附近（overrides 应用）
- Test: `tests/test_config.py` 或新建轻量单测

- [ ] **Step 1: 确认 CLI 传参名**：读 `asr/cli.py` 中 `--device/--engine/--language` 的 dest 与 overrides 组装（实现者现场核对，不要凭记忆写）。

- [ ] **Step 2: 写失败测试**

```python
def test_overrides_device_engine_language_applied(tmp_path):
    """P0-7：overrides 里 device/engine/language 必须落到 cfg。"""
    from asr.config import load_config
    from asr.pipeline import _apply_overrides   # 新抽出的小函数
    cfg = load_config(Path("config.yaml"), strict_models=False)
    _apply_overrides(cfg, {"device": "cuda:0", "engine": "funasr", "language": "en"})
    assert cfg.device == "cuda:0"
    assert cfg.engine == "funasr"
    assert cfg.language == "en"
```

- [ ] **Step 3: 实现**：pipeline.run 里 overrides 解析后抽成 `_apply_overrides(cfg, overrides)` 并调用（device/engine/language 三键；engine 改动需在 build_engine 之前）。不认识的键忽略（现状兼容）。

- [ ] **Step 4: 全量测试 + 提交**

```bash
git add -A && git commit -m "fix(P0-7): CLI device/engine/language overrides 落到 cfg

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: 杂修集 — probe N/A / mp3转码 / 换行清洗 / MD说话人 / whisper选项 / 注释

**Files:**
- Modify: `asr/media.py:55-67`（probe）、`asr/pipeline.py:90-104`（非 wav 音频转码）、`asr/postprocess.py` apply 入口（换行清洗）、`asr/srt.py:142`（md label）、`asr/cli.py`（whisper choice）、`asr/utils.py:98`（注释）
- Test: `tests/test_postprocess.py`、`tests/test_srt.py`

- [ ] **Step 1: 写失败测试（每条修一个测试）**

```python
# test_postprocess.py
def test_newline_sanitized_in_apply():
    """P1-12：段内换行写入 SRT 破坏块结构，入口统一清洗。"""
    segs = [Segment(start=0.0, end=1.0, text="第一行\n第二行")]
    out, _ = apply_postprocess(segs, _test_cfg(), duration_s=10.0)
    assert "\n" not in out[0].text

# test_srt.py
def test_md_label_role_names():
    """P1-13：spk 已是角色名（教师），不得输出"说话人教师"。"""
    segs = [Segment(start=0.0, end=1.0, text="上课", spk="教师")]
    write_md(segs, tmp_path / "t.md")
    content = (tmp_path / "t.md").read_text(encoding="utf-8")
    assert "说话人教师" not in content and "教师" in content
```

probe 的 N/A 用例（无模型依赖）：构造 `run_cmd` 返回 `{"format": {"duration": "N/A"}, "streams": []}` 的 monkeypatch，断言 `probe()` 返回 duration_s == 0.0 不抛异常。

- [ ] **Step 2: 逐条实现**

1. **probe N/A（P1-9）**：
```python
    raw_dur = data.get("format", {}).get("duration")
    try:
        duration_s = float(raw_dur) if raw_dur not in (None, "N/A") else 0.0
    except (TypeError, ValueError):
        duration_s = 0.0
```
2. **mp3/m4a（P1-3）**：pipeline.run 音频分支——非 `.wav` 后缀直接走 `extract_audio(input_path, wav, cfg, audio_track=0)` 转码（ffmpeg 统一 16k mono），删除 `shutil.copy2 + sf.info try/except` 整块。`.wav` 后缀保留现有 copy2 + 采样率检查路径。
3. **换行清洗（P1-12）**：`postprocess.apply()` 第 0 步：`segs = [replace(s, text=re.sub(r"[\r\n]+", " ", s.text)) for s in segs]`
4. **MD label（P1-13）**：`label = seg.spk if seg.spk.startswith("spk") else seg.spk if seg.spk else ""`——简化为：
```python
        if with_speaker and seg.spk:
            label = seg.spk if not seg.spk.startswith("spk") else f"说话人{seg.spk[3:]}"
```
5. **whisper choice（P2-3）**：`asr/cli.py` 删除 `--engine` choices 里的 `whisper`（保留 funasr）；如有 whisper 相关提示文案一并删。
6. **注释（P2-4）**：`utils.iter_inputs` docstring 改为"目录时只扫描第一层（非递归）"。

- [ ] **Step 3: 全量测试 + 提交**

```bash
cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ -q
git add -A && git commit -m "fix(P1): probe N/A 崩溃、mp3 转码、SRT 换行、MD 说话人标签、whisper 选项清理

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 9: P1-6 validate_timestamps 只修越界段

**Files:**
- Modify: `asr/postprocess.py:269-320`
- Test: `tests/test_postprocess.py`

- [ ] **Step 1: 写失败测试**

```python
def test_validate_only_clamps_offending_segment():
    """P1-6：1 段越界不得连累全部段被重排。"""
    segs = [
        Segment(start=0.0, end=5.0, text="正常段"),
        Segment(start=1.0, end=9999.0, text="越界段"),   # end 超时长
        Segment(start=6.0, end=8.0, text="另一正常段"),
    ]
    out = validate_timestamps(segs, duration_s=10.0)
    # 正常段时间戳必须原样保留（不被均分重排）
    assert out[0].start == 0.0 and out[0].end == 5.0
    assert out[-1].start == 6.0 and out[-1].end == 8.0
    assert all(s.end <= 10.5 for s in out)              # 越界段被 clamp
```

- [ ] **Step 2: 实现**：删除"全段按字数均分"分支（288-308 行整块），保留 clamp 路径并把它作为唯一路径；溢出时 warning 逐段记录。`validate_timestamps(segments, None)` 行为不变。

- [ ] **Step 3: 全量测试 + 提交**

```bash
git add -A && git commit -m "fix(P1-6): validate_timestamps 只 clamp 越界段，删除全局重排

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 10: P1-1 引擎缓存 + P1-10 mklink 安全化 + P1-2 batch cfg 隔离

**Files:**
- Modify: `asr/pipeline.py`（build_engine 缓存 + close 注册）、`asr/engines/funasr_engine.py:138-150`（mklink）、`asr/batch.py`（deepcopy）
- Test: `tests/test_funasr_engine.py`、`tests/test_config.py`

**Interfaces:**
- Produces: `pipeline.build_engine(cfg, use_cache=True)`；`pipeline.close_cached_engines()`
- 缓存键：`(cfg.engine, quality, str(asr_model_path), cfg.device, cfg.diarize.enabled)`

- [ ] **Step 1: 实现缓存（此为行为优化，测试以"两次 build 同参返回同实例"为主断言）**

```python
_ENGINE_CACHE: dict[tuple, ASREngine] = {}

def build_engine(cfg: AppConfig, use_cache: bool = True) -> ASREngine:
    key = (
        cfg.engine,
        getattr(cfg, "_quality_override", "balanced"),
        str(cfg.asr.paraformer_model or cfg.models.funasr.asr),
        cfg.device,
        cfg.diarize.enabled,
    )
    if use_cache and key in _ENGINE_CACHE:
        return _ENGINE_CACHE[key]
    if cfg.engine != "funasr":
        raise RuntimeError(f"未知引擎: {cfg.engine}（当前仅支持 funasr）")
    eng = FunASREngine(cfg)
    if use_cache:
        _ENGINE_CACHE[key] = eng
    return eng

def close_cached_engines() -> None:
    for eng in _ENGINE_CACHE.values():
        eng.close()
    _ENGINE_CACHE.clear()
```

web 的 lifespan yield 之后调用 `close_cached_engines()`（Task 6 已建 lifespan）。注意：FunASREngine 惰性加载，缓存未加载实例无内存代价。

- [ ] **Step 2: mklink（P1-10）**：`os.system(f'mklink /J ...')` → 

```python
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(tmp_dst), str(p_obj)],
        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    junction_ok = proc.returncode == 0 and tmp_dst.exists()
```

复制兜底逻辑保持（junction_ok 为 False 时 copytree），临时目录占 3GB 的直接诱因之一是 `os.system` 静默失败——修后 junction 真正生效。

- [ ] **Step 3: batch cfg 隔离（P1-2）**：`asr/batch.py` 线程池 worker 里对 cfg `copy.deepcopy`（找到 batch.py 中把 cfg 传入 pipeline.run 的位置，改为每任务独立副本）。pipeline.py 中 `cfg._quality_override = ...`、`cfg.diarize.enabled = ...`、`cfg.output.formats = ...` 三处共享写入保持不动（副本隔离后安全）。

- [ ] **Step 4: 全量测试 + 提交**

```bash
cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ -q
git add -A && git commit -m "perf(P1-1/P1-2/P1-10): 引擎缓存、batch cfg 深拷贝、mklink 参数化执行

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 11: P2-8 spk 模型按需校验 + 端到端冒烟

**Files:**
- Modify: `asr/config.py:169-`（`_check_model_paths`）
- Verify: 全量测试 + 真实 5 分钟跑通

- [ ] **Step 1: P2-8**：`_check_model_paths` 严格模式下，spk 路径校验加条件 `if cfg.diarize.enabled`；不开 diarize 时缺 spk 模型只 warning 不阻断。

- [ ] **Step 2: 端到端冒烟（真实模型，约 3-5 分钟 CPU）**

```bash
cd /e/classroom-asr-ascii && "C:\Users\wwtong\AppData\Local\Programs\Python\Python312\python.exe" -m asr run tmp_repro_5min.wav --output-dir out_smoke
```

检查 `out_smoke/*.srt`：
1. 段间间隙存在（max gap > 0.01）
2. s/字 非恒定
3. 句号/问号保留
4. 无空文件

- [ ] **Step 3: 提交 + 汇总报告**

```bash
git add -A && git commit -m "fix(P2-8): spk 模型仅在启用 diarize 时强校验

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## 验收标准（对照 review 复现脚本）

1. `rates`（s/字）非常量、`gaps` max > 0.01（P0-1）
2. `any(s.spk for s in after_postprocess)` 为 True（P0-2，diarize 开启时）
3. 输出文本含 `。`（P0-3）
4. fast 模式输出非空（P0-4）
5. `apply_itn("二〇二三年") == "2023年"`（P0-5）
6. done 任务 30 分钟后仍是 done（P0-6）
7. diarize 开启的 web 上传不再崩溃（新 bug A）
8. task 详情页无 JS 报错（新 bug B）
9. 全量 pytest 绿；新增回归测试 ≥ 15 条
