"""
FunASR 引擎（默认引擎，**唯一启用**的引擎）。

实现要点（方案 5.3 节）：
- **VAD 预切架构**：独立 VAD 先跑全文 → 按语音边界分组切片 → asr(vad) 栈逐片转写
  → 独立 punc 逐片加标点 → 逐字对齐切段 → 偏移拼接。不依赖 funasr 的长音频
  合并路径（实测 94 分钟整跑非确定性：全篇无标点+单巨段；300s 片仍偶发时间锚
  污染，见 transcribe 注释）。
- punc 独立调用而非入栈：流式 ct-punc 对特定形状输入会崩（1 字文本两条独立
  死路），独立调用可捕获降级；无 punc 栈输出的原始文本是空格分隔 token 流，
  punc 负责去空格+插标点，_punct_ok 校验字符守恒。
- diarize 与否同一条主路径（text+字级 timestamp 切段）；sentence_info
  仅作 spk 标注层（按时间重叠贴标签）与无字级 ts 时的降级段来源。
- hotword 参数在 FunASR 1.1+ 已移除，捕获 TypeError 自动降级（不报错）。
- 高精度模式 --quality high 时把 model 切换到 paraformer_model。
- 字符级 timestamp 与文本逐字对齐、按强标点切段；单位（毫秒/秒）用音频总时长交叉验证。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Callable

from loguru import logger

from .base import ASREngine, Segment
from ..config import AppConfig


class FunASREngine(ASREngine):
    """FunASR AutoModel 封装"""

    name = "funasr"

    def __init__(self, cfg: AppConfig):
        super().__init__(cfg)
        self.cfg = cfg
        # 延迟加载：模型在首次 transcribe 时才 import AutoModel，
        # 这样 FunASR 未装时仍能跑 asr check / asr run --engine whisper(已禁用)
        self._model = None
        self._loaded = False
        self._vad_model = None       # 独立 VAD（预切用，与 asr 栈内 VAD 各一份）
        self._vad_loaded = False
        self._punc_model = None      # 独立 punc（防崩降级需要单独捕获，见 _punctuate）
        self._punc_loaded = False
        self._hotword_supported = True  # 探测：失败一次后置 False

    # ------------------------------------------------------------------
    # 模型加载
    # ------------------------------------------------------------------
    def _load_model(self):
        """加载模型（幂等）。返回 self._model——transcribe 用本地引用调用
        generate，避免关停线程并发置空 self._model 时的 NoneType 崩溃。"""
        if self._loaded:
            return self._model
        try:
            from funasr import AutoModel  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "FunASR 未安装。请先 pip install -r requirements.txt\n"
                "或在能联网的机器跑 scripts/download_models.py 后再使用。"
            ) from e

        from funasr import AutoModel

        # --quality 决定 ASR 模型：
        #   fast     → SenseVoice-Small（最快，但**无时间戳输出**，仅纯文本）
        #   balanced → Paraformer-Large（带 sentence_info 时间戳，**默认**）
        #   high     → Paraformer-Large + 高精度（当前同一模型；未来换 Fun-ASR-Nano）
        #
        # 重要：SenseVoice 是端到端模型，不输出时间戳。如需时间戳，必须用 Paraformer。
        # 因此默认 quality=balanced，让"教师单讲"场景拿到的结果可直接做字幕。
        quality = getattr(self.cfg, "_quality_override", "balanced")
        if quality == "fast":
            asr_path = str(self.cfg.models.funasr.asr)
            logger.info(f"FunASR fast 模式 → SenseVoice（无时间戳输出）: {asr_path}")
        elif self.cfg.asr.paraformer_model:
            asr_path = str(self.cfg.asr.paraformer_model)
            logger.info(f"FunASR {quality} 模式 → Paraformer-Large: {asr_path}")
        else:
            asr_path = str(self.cfg.models.funasr.asr)
            logger.warning(
                "paraformer_model 路径未配置，回退到 SenseVoice（无时间戳）"
            )

        # 路径校验（再确认一次，启动时已校验，这里兜底）
        for name, p in [
            ("asr", asr_path),
            ("vad", str(self.cfg.models.funasr.vad)),
            ("punc", str(self.cfg.models.funasr.punc)),
        ]:
            if not Path(p).exists():
                raise FileNotFoundError(
                    f"FunASR 模型路径不存在 [{name}]: {p}\n"
                    f"请先在能联网的机器跑 scripts/download_models.py"
                )

        # Windows + sentencepiece 中文路径兼容性：
        # sentencepiece 用 fopen 打开 bpe.model，窄字符路径在 Windows 上会因
        # GBK 编码炸裂（典型表现：file exists 但报 "No such file"）。
        # 解法：把所有模型路径里的非 ASCII 段重定向到 tmp_models/ 的副本/链接。
        asr_path = self._ensure_ascii_path(asr_path, "asr")
        vad_path = self._ensure_ascii_path(str(self.cfg.models.funasr.vad), "vad")

        # 离线锁：阻止 FunASR 启动时尝试联网
        import os
        os.environ["MODELSCOPE_OFFLINE"] = "1"

        spk_model = str(self.cfg.models.funasr.spk) if self.cfg.diarize.enabled else None
        if spk_model:
            spk_model = self._ensure_ascii_path(spk_model, "spk")

        # punc 不入栈：流式 ct-punc 在栈内对特定形状输入会直接炸掉整个 generate
        # （见 _punctuate 注释），独立调用才能捕获降级。punc 由 _load_punc_model
        # 单独加载、在文本层逐片施加。
        self._model = AutoModel(
            model=asr_path,
            vad_model=vad_path,
            punc_model=None,
            spk_model=spk_model,
            device=self.cfg.device,
            disable_update=True,
            ncpu=max(1, self.cfg.num_workers),
        )
        self._loaded = True
        logger.info("FunASR 模型加载完成（asr+vad，punc 独立）")
        return self._model

    def _load_vad_model(self):
        """独立 VAD 模型（幂等）。预切架构用它先跑一遍全文拿语音段边界；
        fsmn-vad 确定性、RTF≈0.007（15 分钟音频 6.6s）。"""
        if self._vad_loaded:
            return self._vad_model
        from funasr import AutoModel

        import os
        os.environ["MODELSCOPE_OFFLINE"] = "1"

        vad_path = self._ensure_ascii_path(str(self.cfg.models.funasr.vad), "vad")
        if not Path(vad_path).exists():
            raise FileNotFoundError(f"VAD 模型路径不存在: {vad_path}")
        self._vad_model = AutoModel(
            model=vad_path,
            device=self.cfg.device,
            disable_update=True,
            ncpu=max(1, self.cfg.num_workers),
        )
        self._vad_loaded = True
        logger.info("独立 VAD 模型加载完成")
        return self._vad_model

    def _load_punc_model(self):
        """独立 punc 模型（幂等）。"""
        if self._punc_loaded:
            return self._punc_model
        from funasr import AutoModel

        import os
        os.environ["MODELSCOPE_OFFLINE"] = "1"

        punc_path = self._ensure_ascii_path(str(self.cfg.models.funasr.punc), "punc")
        if not Path(punc_path).exists():
            raise FileNotFoundError(f"punc 模型路径不存在: {punc_path}")
        self._punc_model = AutoModel(
            model=punc_path,
            device=self.cfg.device,
            disable_update=True,
        )
        self._punc_loaded = True
        logger.info("独立 punc 模型加载完成")
        return self._punc_model

    @staticmethod
    def _ensure_ascii_path(p: str, alias: str) -> str:
        """
        Windows 下 sentencepiece 不支持中文路径。
        检测到非 ASCII 时，把整个模型目录用 junction（Windows 目录链接）
        指向仓库根的 tmp_models/<alias>/，返回新路径。
        junction 不占磁盘空间，对 FunASR 透明。
        """
        p_obj = Path(p).resolve()
        try:
            p_obj.as_posix().encode("ascii")
            return p_obj.as_posix()
        except UnicodeEncodeError:
            pass

        # 非 ASCII：创建 junction
        import os
        tmp_root = Path("tmp_models")
        tmp_dst = (tmp_root / alias).resolve()
        if not tmp_dst.exists():
            tmp_root.mkdir(parents=True, exist_ok=True)
            logger.warning(
                f"检测到非 ASCII 模型路径 {p_obj}，建立 junction 到 {tmp_dst} 规避 sentencepiece Windows 兼容性 bug"
            )
            if sys.platform == "win32":
                # Windows 目录 junction：mklink /J（P1-10：参数化 subprocess
                # 替代 os.system，返回码真实可信）。rc 只用于日志定位失败
                # 原因；兜底与否按**事后存在**判定——并发竞争下输家 rc=1 但
                # 目录已被赢家建好，那也是成功，绝不 copytree 到已存在目录
                # 上（FileExistsError / WinError 183）。
                proc = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(tmp_dst), str(p_obj)],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if proc.returncode != 0:
                    stderr = proc.stderr
                    if isinstance(stderr, bytes):
                        stderr = stderr.decode(errors="replace")
                    logger.warning(
                        f"mklink rc={proc.returncode}: {(stderr or '')[-200:]}"
                    )
            else:
                # 非 Windows：符号链接（理论上不会走到这里）
                os.symlink(p_obj, tmp_dst, target_is_directory=True)

            if not tmp_dst.exists():
                # 事后仍不存在（真失败）→ 退而求其次：完整复制（占空间但能跑）
                import shutil
                logger.warning(f"junction 失败，改用复制 {p_obj} -> {tmp_dst}")
                shutil.copytree(p_obj, tmp_dst)
        return tmp_dst.as_posix()

    # ------------------------------------------------------------------
    # 转写主入口
    # ------------------------------------------------------------------
    # VAD 预切架构（取代早期 300s 定长切片）。证据链：
    # 1. funasr 长音频合并路径不可信：94 分钟整跑非确定性地产出单巨段/无标点
    #    （15 分钟同一音频 ts 数 2778/517 随机跳变）；
    # 2. 300s 定长切片把灾难降为噪声但未根治：94 分钟实测仍有 7/866 段
    #    "幽灵段"（时间锚被合并循环污染：起点复制邻段，跨度拖到 55~162s，
    #    0.8% 段在字幕里钉屏近 3 分钟）——只要一次 generate 含多个 VAD 段，
    #    合并循环就有污染机会；
    # 3. 独立 VAD 预切后每片只含一小组相邻语音段（单 batch、墙钟 ≤30s），
    #    污染空间被结构性消除；片间长静音天然不存在，"一句话拖过课中休息"
    #    的超长段一并消失；
    # 4. 副产收益：静音不进批处理，实测 RTF 0.18 → 0.12。
    VAD_PAD_S = 0.3      # 切片前后留白（保护语音边界的起振/收尾）
    GROUP_MAX_S = 30.0   # 组墙钟上限（含组内静音）
    GROUP_GAP_S = 2.0    # 相邻语音段并入同组的最大间隙

    def transcribe(
        self,
        wav: Path,
        hotwords: Optional[List[str]] = None,
        on_progress: Optional["Callable[[float, str], None]"] = None,
    ) -> List[Segment]:
        """
        转写一个 wav 文件。统一走 VAD 预切路径（见类常量注释）；
        预切机械性失败（非 PCM wave / VAD 模型异常）时降落伞退回整文件
        单次转写——老合并路径有已知缺陷，但保住可用性。
        on_progress(ratio: 0~1, msg)：转写过程中多次触发，
        用于 UI 显示"已识别 X%"。回调失败不影响主流程。
        """
        wav = Path(wav)
        if not wav.exists():
            raise FileNotFoundError(f"输入 wav 不存在: {wav}")

        from ..media import get_wav_duration_s
        duration_total = get_wav_duration_s(wav)

        try:
            return self._transcribe_vad_sliced(wav, duration_total, hotwords, on_progress)
        except Exception as e:  # noqa: BLE001 降落伞：任何预切失败都不让任务归零
            logger.error(
                f"VAD 预切路径失败（{type(e).__name__}: {e}），退回整文件转写: {wav}"
            )
            return self._transcribe_whole(wav, duration_total, hotwords, on_progress)

    def _transcribe_vad_sliced(
        self,
        wav: Path,
        duration_total: float,
        hotwords: Optional[List[str]],
        on_progress: Optional["Callable[[float, str], None]"],
    ) -> List[Segment]:
        """VAD 预切主路径：独立 VAD 全文检测 → 分组切片 → 逐片转写 → 偏移拼接。"""
        import wave

        if on_progress:
            try:
                on_progress(0.02, f"语音活动检测 · {int(duration_total)}s")
            except Exception:  # noqa: BLE001
                pass

        vad = self._load_vad_model()
        vad_segs = vad.generate(input=str(wav))[0].get("value") or []
        groups = _group_vad_segments(vad_segs, self.GROUP_GAP_S, self.GROUP_MAX_S)
        if not groups:
            logger.warning(f"VAD 未检测到语音，返回空结果: {wav}")
            return []
        logger.info(f"VAD 检测 {len(vad_segs)} 个语音段 → 分 {len(groups)} 组转写")

        with wave.open(str(wav), "rb") as w:
            params = w.getparams()
        sr = params.framerate
        file_dur = params.nframes / sr

        segments: List[Segment] = []
        n_empty = 0
        for gi, (gs, ge) in enumerate(groups):
            a = max(0.0, gs - self.VAD_PAD_S)
            b = min(file_dur, ge + self.VAD_PAD_S)
            if b - a <= 0:
                continue
            part_path = wav.with_name(f"{wav.stem}.grp{gi}.wav")
            with wave.open(str(wav), "rb") as r, wave.open(str(part_path), "wb") as ow:
                r.setpos(int(a * sr))
                ow.setparams(params._replace(nframes=0))
                ow.writeframes(r.readframes(int((b - a) * sr)))

            def _cb(ratio: float, msg: str, _a=a, _w=b - a) -> None:
                # 片内 ratio → 全片 ratio；msg 重算成全片口径
                if on_progress:
                    done_s = _a + ratio * _w
                    on_progress(
                        min(0.99, done_s / duration_total),
                        f"已识别 {int(done_s / duration_total * 100)}%"
                        f" · ~{int(done_s)}s / {int(duration_total)}s",
                    )

            try:
                res = self._generate_slice(part_path, b - a, hotwords, _cb)
            finally:
                part_path.unlink(missing_ok=True)

            item = dict(res[0]) if res else {}
            raw = _as_text(item.get("text"))
            if not raw:
                n_empty += 1  # 内层 VAD 丢弃的极短语音（孤字语气词等）
                continue
            # punc 在文本层施加（去空格+插标点）；diarize 时 sentence_info
            # 与顶层 text/timestamp 并存，spk 由 _parse_results 按时间重叠
            # 贴到主路径切出的段上
            item["text"] = self._punctuate(raw)
            part_segs = self._parse_results([item], duration_s=b - a)
            segments.extend(
                Segment(start=s.start + a, end=s.end + a, text=s.text, spk=s.spk)
                for s in part_segs
            )

        if n_empty:
            logger.info(f"{n_empty} 组无有效语音内容（内层 VAD 丢弃，多为孤字语气词）")
        segments.sort(key=lambda s: (s.start, s.end))
        return segments

    def _transcribe_whole(
        self,
        wav: Path,
        duration_total: float,
        hotwords: Optional[List[str]],
        on_progress: Optional["Callable[[float, str], None]"],
    ) -> List[Segment]:
        """降落伞路径：整文件单次 generate + 文本层 punc。
        会经过 funasr 的长音频合并路径（有已知缺陷，见类常量注释），
        仅在 VAD 预切机械性失败时保底使用。"""
        results = self._generate_slice(wav, duration_total, hotwords, on_progress)
        for item in results or []:
            t = _as_text(item.get("text"))
            if t:
                item["text"] = self._punctuate(t)
        return self._parse_results(results, duration_s=duration_total)

    def _generate_slice(
        self,
        wav: Path,
        duration_s: float,
        hotwords: Optional[List[str]],
        on_progress: Optional["Callable[[float, str], None]"],
    ):
        """
        对一个 wav（切片或整文件）跑 asr 栈 generate（栈内无 punc），
        返回 funasr 原始 results（text 为空格分隔 token 流，punc 由调用方施加）。
        on_progress(ratio: 0~1, msg: str)：FunASR 每处理完一个 batch 触发一次，
        用于 UI 显示"已识别 X%"。回调失败不影响主流程。
        """
        # 本地绑定：关停线程可能在我们拿到模型后并发置空 self._model，
        # 后续 generate（含 TypeError 重试）都用这个本地引用，无锁。
        model = self._load_model()
        wav = Path(wav)
        if not wav.exists():
            raise FileNotFoundError(f"输入 wav 不存在: {wav}")

        # 准备 hotword 字符串（FunASR 1.1+ 已移除此参数，下面会捕获并降级）
        hotword_str = " ".join(hotwords) if (hotwords and self.cfg.asr.hotword_enabled) else None

        # FunASR progress_callback(end_idx, num_samples)：VAD 段级进度（不是秒）。
        # monotonic clamp + 上限 0.95（留 5% 给收尾）。msg 会被上层包装成全片口径。
        max_ratio_seen = [0.0]  # 用 list 是为了闭包内可写

        def _pcb(end_idx: int, num_samples: int) -> None:
            if not on_progress or num_samples <= 0:
                return
            try:
                raw = end_idx / num_samples
                ratio = min(0.95, max(0.0, raw))
                # monotonic clamp：回退 ratio 时忽略
                if ratio < max_ratio_seen[0]:
                    return
                max_ratio_seen[0] = ratio
                secs_done = int(ratio * duration_s)
                on_progress(ratio, f"已识别 {int(ratio * 100)}% · ~{secs_done}s / {int(duration_s)}s")
            except Exception:  # noqa: BLE001
                pass

        kwargs = dict(
            input=str(wav),
            batch_size_s=self.cfg.asr.batch_size_s,
            language=self.cfg.language,  # R14：--language 真实生效（默认 zh 不变）
            use_itn=self.cfg.asr.use_itn,
            return_spk_res=self.cfg.diarize.enabled,
        )
        if hotword_str and self._hotword_supported:
            kwargs["hotword"] = hotword_str
        if on_progress:
            kwargs["progress_callback"] = _pcb

        # 调用模型
        try:
            results = model.generate(**kwargs)
        except TypeError as e:
            # hotword 参数不支持 → 降级为"术语表后处理"
            if "hotword" in str(e):
                logger.warning(
                    "当前 FunASR 版本不支持 hotword 参数，自动降级为术语表后处理"
                )
                self._hotword_supported = False
                kwargs.pop("hotword", None)
                results = model.generate(**kwargs)
            else:
                raise

        # 跑完置 100%
        if on_progress:
            try:
                on_progress(1.0, f"识别完成 · {int(duration_s)}s")
            except Exception:  # noqa: BLE001
                pass

        return results

    def _punctuate(self, text: str, depth: int = 0) -> str:
        """独立 punc 调用（幂等模型惰性加载）+ 形状防崩 + 字符守恒校验。

        funasr 1.4.16 流式 ct-punc（ct_transformer_streaming）对特定形状输入
        有两条独立死路，真机实测：
        1. 1 字文本：tokenizer 词表路径产出 DoubleTensor → embed 直接 RuntimeError；
           1 字迷你句：topk 结果 (1,1) 2-D 跳过 squeeze → int() TypeError。
           课堂音频孤字语气词组（嗯/对/好）必踩。
        2. 解法：崩了对半切开递归重试（形状改变极大概率避开）；1 字无标点
           可加，原样返回。
        另外 punc 输出去空格+插标点（输入是空格分隔 token 流），必须用
        _punct_ok 校验"去空格去标点后字符守恒"，失败则退回无标点原文。
        """
        if not text or depth > 6:
            return text
        try:
            model = self._load_punc_model()
            out = _as_text(model.generate(input=text)[0].get("text"))
        except (TypeError, RuntimeError, IndexError) as e:
            if len(text) <= 1:
                return text
            logger.warning(f"punc 崩溃（{type(e).__name__}: {e}），文本对半重试: {text[:20]!r}")
            mid = len(text) // 2
            return self._punctuate(text[:mid], depth + 1) + self._punctuate(text[mid:], depth + 1)
        if out and _punct_ok(text, out):
            return out
        if out:
            logger.warning("punc 输出未通过字符守恒校验，退回无标点原文")
        return text

    # ------------------------------------------------------------------
    # 结果解析（统一为 List[Segment]，时间戳归一为秒）
    # ------------------------------------------------------------------
    def _parse_results(self, results, duration_s: float | None = None) -> List[Segment]:
        """解析 funasr generate() 返回 → List[Segment]（秒）。

        主路径只有一条：text + 字符级 timestamp（timestamp 毫秒（R12），
        逐字对应）。开 spk（diarize）时顶层 text/timestamp **同样返回**
        （真机实测 sentence_info 与之并存），spk 作为标注层按时间重叠
        贴到主路径切出的段上（_spk_at）。

        不用 sentence_info 做段来源：句边界=内层 VAD 段，切片后组内无长
        静音时整组钉成一"句"（真机 60s 巨段），句文本还是无标点的
        字间空格 token 流。仅当顶层 timestamp 缺失（fast 模式）时才降级
        用它的句级时间戳。

        降级链：字级 ts 切段 → sentence_info 句级切段 → 整段占满时长。
        """
        segments: List[Segment] = []
        for item in results or []:
            text = _as_text(item.get("text"))
            ts = item.get("timestamp") or []
            si = item.get("sentence_info") or []
            if not ts:
                # 降级：无字级 timestamp（fast 模式，或开 spk 但顶层 text 缺失
                # 的形状）→ sentence_info 句级时间戳；再没有则整段占满时长
                if si:
                    for s in si:
                        t = _as_text(s.get("text"))
                        if not t:
                            continue
                        segments.append(Segment(
                            start=float(s.get("start", 0)) / 1000.0,
                            end=float(s.get("end", 0)) / 1000.0,
                            text=t,
                            spk=_as_spk(s.get("spk")),
                        ))
                elif text:
                    logger.warning("FunASR 返回无时间戳（fast 模式），整段占满时长")
                    segments.append(Segment(start=0.0, end=duration_s or 0.0, text=text))
                continue
            if not text:
                continue

            # 旧"ts 数与字符数不齐退回单段"门禁已删：切片架构下 ts 与 text
            # 同源同次识别，失配只是口吃重复/BPE 合并（实测 213/223），
            # 拒切会把整片钉成一条 35s 字幕；逐字对齐的漂移 ≤ 失配数×字宽
            # （约 1.5s），远好于不切。ts 耗尽的残余由 _split_by_char_ts
            # 收尾并入末段（守恒不丢字）。
            part = _split_by_char_ts(text, ts, duration_s)
            if si:
                # spk 标注层：按时间重叠取最大覆盖句的说话人
                part = [
                    Segment(start=s.start, end=s.end, text=s.text,
                            spk=_spk_at(si, s.start, s.end))
                    for s in part
                ]
            segments.extend(part)

        segments.sort(key=lambda s: (s.start, s.end))
        return segments

    def close(self) -> None:
        self._model = None
        self._loaded = False
        self._vad_model = None
        self._vad_loaded = False
        self._punc_model = None
        self._punc_loaded = False


# ---------------------------------------------------------------------------
# 文本归一辅助：funasr 的 text 字段可能是 str 或 list
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 说话人归一：funasr 的 spk 可能是 int 或 "spk0" 字符串
# ---------------------------------------------------------------------------
def _as_spk(v) -> Optional[str]:
    if v is None:
        return None
    return str(v) if str(v).startswith("spk") else f"spk{v}"


def _spk_at(sentences: list, seg_start: float, seg_end: float) -> Optional[str]:
    """段 [start,end]（秒）的说话人：取 sentence_info 中重叠最大的句子。

    sentence_info 的 start/end 为毫秒（funasr 1.4.16 实测）。段边界来自
    字级 ts，句子边界来自内层 VAD——一个段可跨句（句间停顿 < _GAP_CUT_S
    不断段），取重叠最大者即可，零重叠（不应发生）返回 None。
    """
    best: Optional[str] = None
    best_ov = 0.0
    for s in sentences:
        ov = min(seg_end, s.get("end", 0) / 1000.0) - max(seg_start, s.get("start", 0) / 1000.0)
        if ov > best_ov:
            best, best_ov = _as_spk(s.get("spk")), ov
    return best


# 强标点：切段边界；弱标点与空白：无 timestamp，对齐时不消费 ts 槽位但保留在文本（R13）
_HARD_PUNCT = "。？！；\n"
_NO_TS_CHARS = set("，、：“”‘’（）()《》\n\r\t ")

# 软标点切分点：长语流无强标点时（真机实测 15 分钟课堂 23/119 段 >8s，
# 最长 34s——字幕钉屏），超过 _MAX_SEG_SPAN_S 在最后一个软标点处断开，
# 时间锚精确（逐字 ts 在手）。与 config 的 srt_max_duration_s=8 对齐。
# 大间隙兜底切分：无标点的稀疏语气词链（真机实测"嗯想不想嗯嗯"6 字拖
# 29.6s、24 字拖 20.5s——课堂管理性闲谈），字间停顿 > _GAP_CUT_S 处断开
# （0.8s 与 fsmn-vad 的 max_end_silence 同源：静音级停顿）。
_MAX_SEG_SPAN_S = 8.0
_SOFT_CUT = set("，、：")
_GAP_CUT_S = 0.8


# ---------------------------------------------------------------------------
# VAD 分组：独立 VAD 输出（毫秒）→ 语音组（秒），gap 内并入、墙钟封顶
# ---------------------------------------------------------------------------
def _group_vad_segments(vad_segs_ms: list, gap_s: float, wall_max_s: float) -> list:
    """[[start_ms, end_ms], ...] → [[start_s, end_s], ...]。
    相邻语音段间隙 ≤ gap_s 并入同组（组墙钟 ≤ wall_max_s），
    组边界即切片边界——片内只含一小组连续语音，长静音永远不进切片。"""
    groups: list = []
    for s_ms, e_ms in vad_segs_ms or []:
        s, e = s_ms / 1000.0, e_ms / 1000.0
        if groups and s - groups[-1][1] <= gap_s and s - groups[-1][0] <= wall_max_s:
            groups[-1][1] = e
        else:
            groups.append([s, e])
    return groups


# ---------------------------------------------------------------------------
# punc 输出合法性：punc 只插入标点/删空格，去空格去标点后必须与原文一致
# ---------------------------------------------------------------------------
_PUNC_ALL = _HARD_PUNCT + "，、：“”‘’（）()《》"


def _punct_ok(raw: str, out: str) -> bool:
    r = raw.replace(" ", "")
    o = "".join(c for c in out if c not in _PUNC_ALL).replace(" ", "")
    return o == r


def _split_by_char_ts(text: str, ts: list, duration_s: float | None) -> List[Segment]:
    """token 级 timestamp 与字符逐一对齐，按强标点切段；超长段在最近切点二次切分。

    funasr 1.4.16 Paraformer 实测（scripts/repro_text_list.py）：timestamp
    长度 ≈ 去标点后字符数（口吃/BPE 合并有小幅失配，残余走耗尽并入），
    每项 [start,end] 对应一个汉字，单位毫秒（R12）。

    切点两类，统一为"最近登记的切点"（后到覆盖先到）：
    - 软标点切点：标点后断开（时间锚=标点前末字 t1 / 后首字 s）；
    - 大间隙切点：无标点的稀疏语气词链，字间停顿 > _GAP_CUT_S 处断开。
    跨度 > _MAX_SEG_SPAN_S 才真正触发切分，否则切点静默作废。
    """
    div = _detect_unit_div(ts, duration_s)
    out: List[Segment] = []
    buf: list[str] = []
    t0 = t1 = prev_t1 = None
    ti = 0
    cut = None         # 切分点：buf 索引（头部含到该索引为止）
    cut_end_t = None   # 头部结束时间（切分点前最后一个锚定字符的 t1）
    next_t0 = None     # 尾部起始时间（切分点后第一个锚定字符的 s）
    for ch in text:
        if ch in _HARD_PUNCT:
            if buf and t0 is not None:
                buf.append(ch)
                out.append(Segment(start=t0 / div, end=t1 / div, text="".join(buf)))
                buf, t0 = [], None
            else:
                buf.append(ch)   # R16：无时间锚的孤立标点并入待收尾处置，不崩不丢
            continue
        if ch in _NO_TS_CHARS:
            buf.append(ch)   # R13：保留字符；不消费 ts 槽位（对齐口径一致）
            if ch in _SOFT_CUT:
                cut, cut_end_t, next_t0 = len(buf), t1, None
            continue
        if ti >= len(ts):
            buf.append(ch)              # 时间戳耗尽：残余文本留给收尾并入
            continue
        s, e = ts[ti]
        ti += 1
        if t0 is None:
            t0 = s
        # 切点登记：大间隙兜底优先（信息更完整），否则补全软标点切点的 next_t0
        if prev_t1 is not None and buf and (s - prev_t1) / div > _GAP_CUT_S:
            cut, cut_end_t, next_t0 = len(buf), prev_t1, s
        elif cut is not None and next_t0 is None:
            next_t0 = s
        t1 = e
        prev_t1 = e
        buf.append(ch)
        # 超长段在最近切点断开（见 _SOFT_CUT / _GAP_CUT_S 注释）
        if (t1 - t0) / div > _MAX_SEG_SPAN_S and cut is not None and next_t0 is not None:
            head, buf = buf[:cut], buf[cut:]
            out.append(Segment(start=t0 / div, end=cut_end_t / div, text="".join(head)))
            t0 = next_t0
            cut = cut_end_t = next_t0 = None
    if buf and t0 is not None:
        out.append(Segment(start=t0 / div, end=t1 / div, text="".join(buf)))
    elif buf and out:                   # 时间戳耗尽的残余并入末段，不丢文本
        last = out[-1]
        out[-1] = Segment(start=last.start, end=last.end, text=last.text + "".join(buf))
    return [s for s in out if s.text.strip()]


def _detect_unit_div(ts: list, duration_s: float | None) -> float:
    """证据判定 timestamp 单位：毫秒(1000) 还是秒(1)。

    funasr 1.4.16 实测毫秒（R12：300.5s 真机冒烟末值 285440 → 285.44s，
    全部段落在时长内且单调；旧"厘秒"结论是对老版本带 bug 输出的循环验证，
    厘秒档从未在真实数据出现，R12 后删除）。
    """
    if not ts:
        return 1.0
    last_end = float(ts[-1][1])
    if duration_s and duration_s > 0:
        # 原值 ≤ 时长×1.5 → 按秒读放得下，只能是秒；超界 → 毫秒
        if last_end <= duration_s * 1.5:
            return 1.0
        return 1000.0
    return 1.0 if last_end <= 3600 else 1000.0
