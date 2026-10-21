# 开发记录：实现偏差 · 阶段进度 · 已知边界

施工阶段的决策记录。使用说明看 [README](../README.md)，方案原文见 方案.txt。

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

## 阶段进度

| 阶段 | 状态 | 真实证据 |
|---|---|---|
| P1 骨架 + check | ✅ | asr check 通过，11/11 测试 |
| P2 whisper_engine | ⏸ **跳过** | 按用户要求禁用 whisper |
| P3 FunASR 引擎 | ✅ | 真实中文转写，19/19 测试 |
| P4 postprocess + writers | ✅ | 端到端生成五件套，48/48 测试（含 SRT 软约束 + 弱标点修复） |
| P5 diarize | ✅ | diarize.py 角色启发式（教师/学生A-E/其他），9/9 单测；真实视频待生产时验证 |
| P6 batch + 并发 | ✅ | batch 跑通，进度条 + 失败隔离 |
| P7 长视频 + README FAQ | ✅ | README；51s 测试通过 |
| P8 Web UI | ✅ | FastAPI + SQLite + 轮询；10/10 测试 |
| P9 Web 增强 | ✅ | 多段上传合并、AI 反馈（LLM 点评/优化稿/在线阅读/修改痕迹 diff）、设置页；pytest 270 passed |

**验收清单（方案第 8 节）：12 项中 9 通过 / 2 部分通过 / 1 待生产验证**。

## 已知边界

1. **断句合并算法**：当前按字数比例插值时间戳，对短句重复（如多遍 wav）会产生过度切片。生产建议用真实课堂单段长录音。
2. **Paraformer timestamp 异常**：某些版本下 end 值远超 duration，已硬裁剪（`validate_timestamps`）。
3. **说话人分离真实数据未验证**：代码完成（diarize.py + 引擎 spk_model 集成），9/9 单测通过；真实视频未用 `--diarize` 重跑（重跑需 24 分钟）。
4. **全班齐读/齐答时分离不可靠**：cam++ 在重叠语音场景下不可靠，建议关闭 `--diarize`。
5. **SRT 每条 ≤ 8 秒形式验收已放弃**：FunASR 粗时间戳导致每段 22-170s 是常态，软约束 + warning 保留；根治需要 FunASR token-level timestamp 重建。
6. **GPU 未在本机验证**：当前机器 `nvidia-smi` 报 "Unknown Error"，未配 NVIDIA 驱动。CPU 上 RTF ≈ 0.13 已验证。
7. **Web UI 单文件 only**：批量请走 CLI `python -m asr batch`。
