# classroom-asr 深度代码审查

审查日期：2026-09-22
审查范围：`asr/` 全部模块（15 个源文件）、`config.yaml`、真实产物 `out_real/`
验证方式：静态阅读 + 用真实产物数据复核 + 针对怀疑点写最小复现脚本实测
测试现状：93 passed（但见 P2-5，测试与真实产物脱节）

---

## 一句话结论

**系统能跑通，但核心产物是坏的**：94 分钟真实视频产出的 SRT，时间戳是按字数线性插值编造的（s/字 恒为 2.5107，段间间隙严格为 0），所有句号问号感叹号被丢弃，说话人分离功能从未真正执行过。而 93 个测试全绿。

---

## P0 — 功能性错误（产物不可用 / 静默数据错误）

| # | 问题 | 位置 | 证据 | 影响 |
|---|---|---|---|---|
| P0-1 | **字幕时间轴是伪造的** | `funasr_engine.py:293-297` | 见下"实测证据" | SRT/VTT 完全无法对齐音频，不可用 |
| P0-2 | **说话人分离从未执行** | `postprocess.py:252,261` + `pipeline.py:147` | 实测 spk `[spk0,spk1,spk0]` → `[None,None,None]` | `--diarize` 是死开关，README 声称 P5 ✅ 是假象 |
| P0-3 | **强标点全部丢弃** | `postprocess.py:141-154` `split_by_punct` | `out_real` 句号 0 / 问号 0 / 感叹号 0 | 转写文本不可读 |
| P0-4 | **`--quality fast` 产出空文件** | `funasr_engine.py:282-284` → `postprocess.py:315` | 实测 2 段 → 0 段 | 用户拿到 0 字节结果且无报错 |
| P0-5 | **ITN 年份硬编码致数据错误** | `postprocess.py:62-64` | 实测 `二〇二三年` → `2025` | 静默把错误年份写进转写稿 |
| P0-6 | **看门狗把已完成任务改成 failed** | `web/__init__.py:517-534` | 代码审查 | 任务完成 30 分钟后状态被污染 |
| P0-7 | **CLI 参数完全无效** | `pipeline.py` 从不读 `overrides["device"/"engine"/"language"]` | 代码审查 | README 里的 `--device cuda:0` 是骗人的 |

### P0-1 实测证据

```
总字数 2216，段数 91，duration 5628.24s，首段 start=50.8，末段 end=5614.602

每段 秒/字：2.5108 / 2.5107 / 2.5107 / 2.5108 / 2.5107 ...
s/字  min=2.5107  max=2.5108  平均=2.5107      ← 恒定到小数点后 4 位
相邻段间隙 max = 0.000000                       ← 91 段首尾严格相接
```

真实 VAD 分段**不可能**段间间隙为 0（必有静音间隔），也不可能 s/字 恒定。这说明整个 94 分钟视频只解析出了 **1 个**原始 Segment，91 段全部由它按字数均分而来。

**根因链**：
1. `funasr_engine.py:293-297`：走顶层 `timestamp` 分支时，只取 `ts[0]` 和 `ts[-1]` 两个 token 的时间，**中间几百个 token 的时间戳全部丢弃**，整段文本塞进 1 个 `Segment`。
2. `postprocess.py:239-262`：`merge_short_sentences` 只能在这个巨大区间里按字数比例插值。
3. `srt.py:96-100`：8 秒硬约束已降级为 warning，实测段长 22~188 秒直接放行。

### P0-2 根因

`merge_short_sentences` 重建 `Segment` 时不传 `spk`（第 252、261 行），`spk` 全丢。
而 `pipeline.py:147` 的守卫是 `if cfg.diarize.enabled and any(s.spk for s in segs)` —— 恒为 False，**diarize 分支永远进不去**。
`diarize.py` 的 9 个单测只测了它自己，没有测 pipeline 集成，所以全绿。

### P0-5 根因

```python
def _year_sub(m): return "2025"   # 占位：实际年份动态化留作 TODO
```
TODO 直接进了生产路径，任何 `二X三X年` 都被写成 2025。且正则 `二[零〇一二三四五六七八九]{3}年?` 不匹配 `一九九九年`，覆盖不全。

### P0-6 根因

`_watchdog` 是 `while True: sleep(60)`，**没有停止条件**。任务 done 后 `last_cb_time` 不再更新，30 分钟后 `elapsed >= ASR_STALL_TIMEOUT_S` 成立 → 把已完成任务的 status 改成 `failed`。同时主线程可能正写 `done`，构成竞态。

---

## P1 — 可靠性 / 性能 / 安全

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| P1-1 | **每文件重新加载 2GB 模型** | `pipeline.py:119` `build_engine` | 每次 `run` 新建引擎，无缓存；`engine.close()` 从未调用 → 批量 10 个文件加载 10 次模型 + 内存累积 |
| P1-2 | **batch 多线程共享并原地改 cfg** | `pipeline.py:117-118,142,160` | `cfg.diarize.enabled` / `cfg.postprocess.glossary` / `cfg.output.formats` / `cfg._quality_override` 全在共享对象上写，线程池下数据竞争 |
| P1-3 | **mp3/m4a 输入静默失败** | `pipeline.py:92-104` | 直接 `shutil.copy2` 成 `.wav` 文件名，`sf.info` 抛错被 `except Exception: pass` 吞掉 → 拿 mp3 内容喂给 FunASR |
| P1-4 | **两套矛盾的时间单位逻辑** | `utils.py:120` vs `funasr_engine.py:311` | `normalize_time_unit` 靠 `duration_s*1.2` 判定，`_detect_time_unit` 靠"均值>30"判定；且 `sentence_info` 分支调 `normalize_time_unit` 时**不传 duration_s**，只剩 `>1e6` 兜底 → 毫秒返回值会变成几万秒 |
| P1-5 | **`_detect_time_unit` 启发式不可靠** | `funasr_engine.py:327` | "均值>30 即 centisecond"：若音频前 30 秒静音、首个 token 在 45 秒 → 秒被误判为 centisecond，时间缩小 100 倍 |
| P1-6 | **validate_timestamps 一拖全毁** | `postprocess.py:288-308` | 只要有 1 段 `end > duration+1s`，就把**全部**段按字数重排，毁掉本来正确的时间戳 |
| P1-7 | **Web 无任何认证 / 限流 / 大小限制** | `web/__init__.py:177-216` | README 还教用户 `--host 0.0.0.0` 开放局域网 → 任意人可上传任意文件、删除项目 |
| P1-8 | **transcript 全量重写** | `web/__init__.py:602-619` | 每 0.15~0.5s 把**完整** segments JSON 写一次 DB，长视频几千次全量写 |
| P1-9 | **`probe()` 遇 N/A 崩溃** | `media.py:61` | fragmented mp4 的 `format.duration` 为 `"N/A"`，`float("N/A")` 直接 ValueError |
| P1-10 | **`os.system('mklink /J ...')`** | `funasr_engine.py:140` | 路径来自配置文件，存在命令注入面；且不检查返回码，失败时退化为 `copytree` —— 实测 `tmp_models/` 已占 **3.0GB** |
| P1-11 | **死代码残留** | `postprocess.py:230-239` | `for ...: pass  # 见下方改进` 整块废弃循环 + 未使用的 `cur_seg_start_idx`，紧接着重新 `out = []` |
| P1-12 | **SRT 文本含裸换行** | `srt.py:110` | 段文本内含 `\n` 直接写入，破坏字幕块结构（实测 out_real 第 2 段有换行） |
| P1-13 | **`write_md` 说话人文案错** | `srt.py:142` | diarize 后 spk 已是"教师"，`replace('spk','')` 无效 → 输出"说话人教师" |

---

## P2 — 工程卫生

| # | 问题 |
|---|---|
| P2-1 | **无 git 仓库、无 .gitignore**；`models/` 2.1G + `tmp_models/` 3.0G + `uploads/` 2.0G + `lecture.mp4` 522MB ≈ **7.6GB** 二进制散落工作区 |
| P2-2 | `@app.on_event("startup")` 已弃用（pytest 已报 DeprecationWarning），应改 lifespan |
| P2-3 | `--engine whisper` 仍在 CLI choice 里，选了必崩（`whisper_engine` 是 stub） |
| P2-4 | `iter_inputs` 注释写"递归扫描一层"，实现是非递归 |
| P2-5 | **93 个测试全绿但与真实产物脱节**：`test_postprocess.py:88` 正向断言 `split_by_punct("你好。今天") == ["你好","今天"]` —— 把"丢弃标点"这个 bug 固化成了规范。缺少端到端断言（标点保留、spk 保留、时间戳比率非常量） |
| P2-6 | `AppConfig._quality_override` 用隐式私有属性传参（pydantic 允许下划线属性，但类型检查器与静态分析都看不见） |
| P2-7 | 子类 `transcribe` 多了 `on_progress` 参数，与抽象基类签名不一致（LSP 违反） |
| P2-8 | `_check_model_paths` 严格模式下校验 spk 模型，但不开 `--diarize` 时根本不需要它 → 缺 spk 会阻断所有运行 |

---

## 修复优先级建议

**第一梯队（决定产品能不能用）**
1. 重写 `funasr_engine._parse_results`：优先解析 `sentence_info`；走 `timestamp` 分支时保留 token 序列，建立 token→字符对齐，按 VAD 自然分段切分，而不是只取首尾两个 token。这是 P0-1 的根治，其余都是补丁。
2. `merge_short_sentences` 全程保留 `spk`，并把 diarize 移到 postprocess **之前**（说话人先定，再按人切句）。
3. `split_by_punct` 切分后把标点回填到句尾；同时补一条端到端断言"输出文本标点数 == 输入标点数"。

**第二梯队（决定能不能交付给别人用）**
4. `apply_itn` 要么正确实现中文年份转换，要么干脆删掉——宁可不转换也不能静默写错。
5. 引擎实例缓存 + 显式 `close()`；batch 每个线程独立 `cfg`（深拷贝）。
6. 看门狗加 `threading.Event` 停止信号；任务终态（done/failed）后必须 join。
7. 让 overrides 真正生效：`device`/`engine`/`language` 在 pipeline 里落到 cfg；或干脆从 CLI 移除这些 option。
8. 补 `.gitignore` + 清理 `tmp_models/`（3GB）、`uploads/`（2GB）。

**第三梯队**
9. Web 加基础认证与上传大小限制。
10. 统一时间单位逻辑为一套，并强制传入 `duration_s`。
11. `validate_timestamps` 改为"只修越界的那一段"，不做全局重排。

---

## 复现脚本（供回归验证）

```python
# P0-1：检测时间戳是否为插值伪造
rates = [(s["end"] - s["start"]) / len(s["text"]) for s in segs]
assert max(rates) - min(rates) > 1e-3, "s/字 恒定 → 时间戳是插值编造的"
gaps = [segs[i+1]["start"] - segs[i]["end"] for i in range(len(segs)-1)]
assert max(gaps) > 0.01, "段间无间隙 → 不是真实 VAD 分段"

# P0-2：diarize 是否真的生效
assert any(s.spk for s in after_postprocess), "postprocess 抹掉了 spk"

# P0-3：标点是否被吃
assert sum(t.count("。") for t in texts) > 0

# P0-4/5
assert len(out) > 0, "fast 模式产出空文件"
assert apply_itn("二〇二三年") == "2023"
```
