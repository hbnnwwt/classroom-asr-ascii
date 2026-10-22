"""
后处理（方案 5.5 节）：
- ITN：引擎已规整主要情况（年份/数字），这里补正则兜底
- 术语纠错：读 glossary/*.txt，最长匹配替换，记替换日志
- 断句合并已删除（会跨静音合并短句 + 插值销毁真时间锚，见 apply 注释）；
  断句职责在引擎层（asr/engines/funasr_engine._split_by_char_ts）
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Tuple

from loguru import logger

from .config import AppConfig
from .engines.base import Segment


# ---------------------------------------------------------------------------
# ITN 正则兜底（引擎 ITN 不覆盖的部分）
# ---------------------------------------------------------------------------
_CN_DIGIT = {"零": "0", "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
             "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
_YEAR_CN_RE = re.compile(r"([零〇一二三四五六七八九]{4})年")
# 百分号读法："百分之三十" → "30%"
_PERCENT_CN_RE = re.compile(r"百分之([正负+\-零〇一二两三四五六七八九十百千万亿点\d.]+)")


def _cn_num_to_int(s: str) -> int | None:
    """中文数字串转 int。支持 0-99 范围（零到九十九）。"""
    digit_map = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
                 "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if s in digit_map:
        return digit_map[s]
    if re.fullmatch(r"[一二三四五六七八九]?十[一二三四五六七八九]?", s):
        tens, ones = s.split("十")
        return (digit_map[tens] if tens else 1) * 10 + (digit_map[ones] if ones else 0)
    return None


def apply_itn(text: str) -> str:
    """对单条文本做 ITN 正则兜底。只做有把握的转换，宁可不动也不写错。"""
    text = _YEAR_CN_RE.sub(
        lambda m: "".join(_CN_DIGIT[c] for c in m.group(1)) + "年", text)

    def _pct_sub(m: re.Match) -> str:
        n = _cn_num_to_int(m.group(1))
        return f"{n}%" if n is not None else m.group(0)
    return _PERCENT_CN_RE.sub(_pct_sub, text)


# ---------------------------------------------------------------------------
# 术语表加载与替换
# ---------------------------------------------------------------------------
def load_glossary(path: Path) -> List[Tuple[str, str]]:
    """
    加载术语表。返回 [(pattern, replacement), ...]，按 pattern 长度降序（最长优先）。
    支持格式：
      - `错误读音 -> 正确写法`  （替换）
      - `正确写法`             （仅记录，不替换）
    跳过空行和 # 注释。
    """
    if not path.exists():
        logger.warning(f"术语表不存在: {path}，跳过术语纠错")
        return []
    pairs: List[Tuple[str, str]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "->" in line:
                pat, rep = line.split("->", 1)
                pat, rep = pat.strip(), rep.strip()
                if pat and rep:
                    pairs.append((pat, rep))
            else:
                # 仅正确词：仅做"记录"，不进入替换逻辑（避免误伤）
                pass
    # 按 pattern 长度降序，最长优先（避免短 pattern 先匹配吞掉长的）
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    return pairs


def apply_glossary(
    segments: List[Segment],
    pairs: List[Tuple[str, str]],
) -> Tuple[List[Segment], List[str]]:
    """
    应用术语纠错，返回 (新段列表, 替换日志行)。
    日志格式：`<文件> | <start> | 原文 -> 新文`
    """
    if not pairs:
        return segments, []
    log: List[str] = []
    out: List[Segment] = []
    for seg in segments:
        new_text = seg.text
        for pat, rep in pairs:
            if pat in new_text:
                old = new_text
                new_text = new_text.replace(pat, rep)
                log.append(f"{seg.start:8.2f} | {pat!r} -> {rep!r}: {old!r} => {new_text!r}")
        if new_text != seg.text:
            out.append(replace(seg, text=new_text))
        else:
            out.append(seg)
    return out, log


# ---------------------------------------------------------------------------
# 时间戳合法性校验（方案 5.8 节）
# ---------------------------------------------------------------------------
def validate_timestamps(
    segments: List[Segment],
    duration_s: float | None,
) -> List[Segment]:
    """
    校验 + 归一化时间戳。

    处理两类异常（P1-6：逐段处理，越界只影响越界段本身）：
    1. **start < 0 或 end > duration + 0.5s**：clamp 到 [0, duration+0.5s]，
       其余段时间戳原样保留（引擎已输出字对齐时间戳，按字数全局重排
       会毁掉正常段的正确时间戳，已删除）。
    2. **end <= start**：跳过该段。
    """
    if not segments:
        return segments
    if duration_s is None:
        return sorted(segments, key=lambda x: (x.start, x.end))

    cleaned: List[Segment] = []
    for seg in segments:
        s = max(0.0, seg.start)
        e = min(seg.end, duration_s + 0.5)
        if e <= s + 0.01:
            logger.warning(f"段 end <= start + 0.01s，跳过: [{s:.2f},{e:.2f}] {seg.text!r}")
            continue
        if s != seg.start or e != seg.end:
            logger.warning(
                f"段时间戳越界，clamp 到 [{s:.2f},{e:.2f}]"
                f"(原 [{seg.start:.2f},{seg.end:.2f}]，时长 {duration_s:.2f}s): {seg.text!r}"
            )
        cleaned.append(Segment(start=s, end=e, text=seg.text, spk=seg.spk, conf=seg.conf))
    cleaned.sort(key=lambda x: (x.start, x.end))
    return cleaned


# ---------------------------------------------------------------------------
# 主入口：应用全部后处理
# ---------------------------------------------------------------------------
def apply(segments: List[Segment], cfg: AppConfig, *, duration_s: float | None = None) -> tuple[List[Segment], List[str]]:
    """
    应用全部后处理，返回 (新段列表, 替换日志)。
    """
    segs = segments
    # 0. 换行清洗：段内 \r\n 会破坏 SRT/MD 块结构，入口统一替换为空格
    segs = [replace(s, text=re.sub(r"[\r\n]+", " ", s.text)) for s in segs]

    # 1. ITN
    if cfg.postprocess.itn:
        segs = [replace(s, text=apply_itn(s.text)) for s in segs]

    # 2. 术语纠错
    pairs = load_glossary(cfg.postprocess.glossary) if cfg.postprocess.glossary else []
    segs, log = apply_glossary(segs, pairs)

    # 3. 断句合并已删除：该步骤为粗粒度时间戳时代设计——合并不看时间间隙，
    #    真机实测把跨 15s 静音的三段短语气词（嗯/想不想/嗯嗯）合并成一条
    #    29.6s 钉屏字幕，且其"超长句切分"用字数插值时间戳，会销毁引擎的
    #    逐字真时间锚。切分职责已全部收归引擎层（_split_by_char_ts：
    #    强标点 + 软标点 + 大间隙三类切点，时间锚全部真实）。

    # 4. 时间戳校验
    segs = validate_timestamps(segs, duration_s)
    return segs, log
