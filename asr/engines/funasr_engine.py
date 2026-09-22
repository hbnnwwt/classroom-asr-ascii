"""
FunASR 引擎（默认引擎，**唯一启用**的引擎）。

实现要点（方案 5.3 节）：
- AutoModel 加载 ASR/VAD/PUNC/SPK 四件套；SPK 仅在 --diarize 时启用。
- 优先使用 sentence_info（带字级别时间戳 + spk），拿不到退回顶层 timestamp。
- hotword 参数在 FunASR 1.1+ 已移除，捕获 TypeError 自动降级（不报错）。
- 高精度模式 --quality high 时把 model 切换到 paraformer_model。
- 时间戳统一通过 normalize_time_unit 归一为秒。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional, Callable

from loguru import logger

from .base import ASREngine, Segment
from ..config import AppConfig
from ..utils import normalize_time_unit


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
        self._hotword_supported = True  # 探测：失败一次后置 False

    # ------------------------------------------------------------------
    # 模型加载
    # ------------------------------------------------------------------
    def _load_model(self):
        if self._loaded:
            return
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
        punc_path = self._ensure_ascii_path(str(self.cfg.models.funasr.punc), "punc")

        # 离线锁：阻止 FunASR 启动时尝试联网
        import os
        os.environ["MODELSCOPE_OFFLINE"] = "1"

        spk_model = str(self.cfg.models.funasr.spk) if self.cfg.diarize.enabled else None
        if spk_model:
            spk_model = self._ensure_ascii_path(spk_model, "spk")

        self._model = AutoModel(
            model=asr_path,
            vad_model=vad_path,
            punc_model=punc_path,
            spk_model=spk_model,
            device=self.cfg.device,
            disable_update=True,
            ncpu=max(1, self.cfg.num_workers),
        )
        self._loaded = True
        logger.info("FunASR 模型加载完成")

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
                # Windows 目录 junction：mklink /J
                os.system(f'mklink /J "{tmp_dst}" "{p_obj}" >nul 2>&1')
            else:
                # 非 Windows：符号链接（理论上不会走到这里）
                os.symlink(p_obj, tmp_dst, target_is_directory=True)

        if not tmp_dst.exists():
            # junction 创建失败 → 退而求其次：完整复制（占空间但能跑）
            import shutil
            logger.warning(f"junction 失败，改用复制 {p_obj} -> {tmp_dst}")
            shutil.copytree(p_obj, tmp_dst)
        return tmp_dst.as_posix()

    # ------------------------------------------------------------------
    # 转写主入口
    # ------------------------------------------------------------------
    def transcribe(
        self,
        wav: Path,
        hotwords: Optional[List[str]] = None,
        on_progress: Optional["Callable[[float, str], None]"] = None,
    ) -> List[Segment]:
        """
        转写一个 wav 文件。
        on_progress(ratio: 0~1, msg: str)：FunASR 每处理完一个 batch 触发一次，
        用于 UI 显示"已识别 X%"。回调失败不影响主流程。
        """
        self._load_model()
        wav = Path(wav)
        if not wav.exists():
            raise FileNotFoundError(f"输入 wav 不存在: {wav}")

        # 准备 hotword 字符串（FunASR 1.1+ 已移除此参数，下面会捕获并降级）
        hotword_str = " ".join(hotwords) if (hotwords and self.cfg.asr.hotword_enabled) else None

        # FunASR progress_callback(end_idx, num_samples)：VAD 段级进度（不是秒）
        # FunASR 内部 ASR 主体走完后还会做 PUNC，PUNC 也会触发这个 callback，但
        # 那时 num_samples/end_idx 语义变了（按字符），ratio 会乱跳。所以这里加
        # monotonic clamp + 上限 0.95（留 5% 给 PUNC/收尾）。
        max_ratio_seen = [0.0]  # 用 list 是为了闭包内可写

        def _pcb(end_idx: int, num_samples: int) -> None:
            if not on_progress or num_samples <= 0:
                return
            try:
                raw = end_idx / num_samples
                ratio = min(0.95, max(0.0, raw))
                # monotonic clamp：旧 callback（如 PUNC）回退 ratio 时忽略
                if ratio < max_ratio_seen[0]:
                    return
                max_ratio_seen[0] = ratio
                secs_done = int(ratio * duration_total)
                on_progress(ratio, f"已识别 {int(ratio * 100)}% · ~{secs_done}s / {int(duration_total)}s")
            except Exception:  # noqa: BLE001
                pass

        # 提前算好总时长（回调里要用）
        from ..media import get_wav_duration_s
        duration_total = get_wav_duration_s(wav)

        kwargs = dict(
            input=str(wav),
            batch_size_s=self.cfg.asr.batch_size_s,
            language="zh",
            use_itn=self.cfg.asr.use_itn,
            return_spk_res=self.cfg.diarize.enabled,
        )
        if hotword_str and self._hotword_supported:
            kwargs["hotword"] = hotword_str
        if on_progress:
            kwargs["progress_callback"] = _pcb

        # 调用模型
        try:
            results = self._model.generate(**kwargs)
        except TypeError as e:
            # hotword 参数不支持 → 降级为"术语表后处理"
            if "hotword" in str(e):
                logger.warning(
                    "当前 FunASR 版本不支持 hotword 参数，自动降级为术语表后处理"
                )
                self._hotword_supported = False
                kwargs.pop("hotword", None)
                if "progress_callback" in kwargs:
                    kwargs["progress_callback"] = _pcb
                results = self._model.generate(**kwargs)
            else:
                raise

        # 跑完置 100%
        if on_progress:
            try:
                on_progress(1.0, f"识别完成 · {int(duration_total)}s")
            except Exception:  # noqa: BLE001
                pass

        return self._parse_results(results)

    # ------------------------------------------------------------------
    # 结果解析（统一为 List[Segment]，时间戳归一为秒）
    # ------------------------------------------------------------------
    def _parse_results(self, results) -> List[Segment]:
        """
        FunASR 返回结构（典型）：
        - results: List[dict]
        - 每个 dict 含：
          - "sentence_info": List[{start, end, text, spk?, ...}]   ← 优先
          - "timestamp": List[[s_ms, e_ms], ...]                    ← 顶层毫秒
          - "text": str                                            ← 全文文本
        """
        segments: List[Segment] = []
        if not results:
            return segments

        for item in results:
            # 1) 优先 sentence_info（带 spk）
            if "sentence_info" in item and item["sentence_info"]:
                for s in item["sentence_info"]:
                    start = normalize_time_unit(s.get("start", 0))
                    end = normalize_time_unit(s.get("end", 0))
                    text = _as_text(s.get("text"))
                    spk = s.get("spk")
                    if not text:
                        continue
                    segments.append(
                        Segment(start=start, end=end, text=text, spk=spk)
                    )
                continue

            # 2) sentence_info 不存在 → 用顶层 text + token-level timestamp
            #
            # Paraformer-large-vad-punc 实测行为（funasr 1.4.16）：
            # - timestamp 数组长度 = 文本字符数（去标点后），单位是 centisecond（1/100秒）
            # - 例：[150, 350] = 1.50s ~ 3.50s
            # - 不带 token→char 对齐；无法精确映射回原文本标点位置
            #
            # 当前 P3 实现：如实保留 token-level 数据，作为 Segment 逐字符切分输出。
            # 短句合并、按标点切分 → 留给 P4 postprocess 处理。
            text = _as_text(item.get("text"))
            ts = item.get("timestamp") or []
            if not text:
                continue

            if not ts:
                logger.warning("FunASR 返回无时间戳，使用 0 占位")
                segments.append(Segment(start=0.0, end=0.0, text=text))
                continue

            # 单位探测：centisecond vs 秒
            unit_div = _detect_time_unit(ts, text)

            # 字符级 timestamp 是 token 级，无法精确回贴到带标点的原文本。
            # 当前 P3 实现：整段文本作为 1 个 Segment，时间戳取首尾 token。
            # 真正的"按标点切分 + 时间戳插值"留 P4 postprocess 处理。
            start_tok = ts[0]
            end_tok = ts[-1]
            start = float(start_tok[0]) / unit_div
            end = float(end_tok[1]) / unit_div
            segments.append(Segment(start=start, end=end, text=text))

        # 排序 + 校验
        segments.sort(key=lambda s: (s.start, s.end))
        return segments

    def close(self) -> None:
        self._model = None
        self._loaded = False


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
# 时间单位探测辅助：centisecond vs 秒
# ---------------------------------------------------------------------------
def _detect_time_unit(timestamps, text: str) -> float:
    """
    探测 Paraformer 返回的 timestamp 单位。
    - Paraformer-large-vad-punc 实测返回 centisecond（1/100 秒）
    - 启发式：取所有值的均值，> 30 视为 centisecond（因为 30s 以上的课程极少见）
      否则视为秒
    """
    if not timestamps:
        return 1.0
    vals = []
    for s, e in timestamps[:20]:  # 抽前 20 段足够
        vals.append(float(s))
        vals.append(float(e))
    if not vals:
        return 1.0
    avg = sum(vals) / len(vals)
    if avg > 30:
        return 100.0   # centisecond
    return 1.0         # 已是秒
