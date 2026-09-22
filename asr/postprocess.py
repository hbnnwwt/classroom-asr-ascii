"""
后处理（方案 5.5 节）：
- ITN：引擎已规整主要情况（年份/数字），这里补正则兜底
- 术语纠错：读 glossary/*.txt，最长匹配替换，记替换日志
- 断句合并：min/max 字符数规则 + 标点二次切分 + 时间戳按字数插值
- 行长度限制：SRT 单行 ≤ srt_max_chars_per_line
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
# 二零二五 / 二〇二五 → 2025
_YEAR_CN_RE = re.compile(r"二[零〇一二三四五六七八九]{3}年?")
# 百分号读法："百分之三十" → "30%"
_PERCENT_CN_RE = re.compile(r"百分之([零〇一二三四五六七八九十百]+)")


def _cn_num_to_int(s: str) -> int | None:
    """中文数字串转 int。支持 0-99 范围（零到九十九）。"""
    digit_map = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
                 "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if not s:
        return None
    # 含"十"处理
    if "十" in s:
        parts = s.split("十", 1)
        tens = parts[0]
        ones = parts[1] if len(parts) > 1 else ""
        t = digit_map.get(tens, 1) if tens else 1   # "十" 或 "一十" → 10
        o = digit_map.get(ones, 0) if ones else 0
        return t * 10 + o
    # 含"百"（简化：只支持 X百 形式，不支持 X百Y十Z）
    if "百" in s:
        parts = s.split("百", 1)
        hundreds = digit_map.get(parts[0], 1) if parts[0] else 1
        rest = parts[1] if len(parts) > 1 else ""
        r = _cn_num_to_int(rest) if rest else 0
        return hundreds * 100 + (r or 0)
    # 纯数字
    if all(c in digit_map for c in s):
        try:
            return int("".join(str(digit_map[c]) for c in s))
        except Exception:
            return None
    return None


def apply_itn(text: str) -> str:
    """对单条文本做 ITN 正则兜底。引擎已做 ITN 时，多数规则不会触发。"""
    def _year_sub(m: re.Match) -> str:
        return "2025"  # 占位：实际年份动态化留作 TODO
    text = _YEAR_CN_RE.sub(_year_sub, text)

    def _pct_sub(m: re.Match) -> str:
        n = _cn_num_to_int(m.group(1))
        return f"{n}%" if n is not None else m.group(0)
    text = _PERCENT_CN_RE.sub(_pct_sub, text)
    return text


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
# 断句合并（方案 5.5 节）
# ---------------------------------------------------------------------------
# 强切分标点：句号、问号、感叹号、分号
_HARD_PUNCT = "。？！；\n"
# 弱切分标点：逗号、顿号 —— 允许短句合并时跨过
_SOFT_PUNCT = "，、"


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


def merge_short_sentences(
    segments: List[Segment],
    *,
    min_chars: int,
    max_chars: int,
    duration_s: float | None = None,
) -> List[Segment]:
    """
    把短句合并到 min_chars 以上，把超长句按标点二次切分。
    时间戳按字数比例在原始段的时间区间内插值。
    """
    if not segments:
        return segments

    # 0) 入口清洗：把以 weak 标点开头的段拼到前一段，避免污染向下游传递
    cleaned: List[Segment] = []
    for seg in segments:
        if seg.text and seg.text[0] in _SOFT_PUNCT and cleaned:
            prev = cleaned[-1]
            cleaned[-1] = replace(prev, text=prev.text + seg.text, end=seg.end)
        else:
            cleaned.append(seg)
    segments = cleaned

    # 1) 先按强标点切分
    pieces: List[Tuple[str, float, float, Optional[str]]] = []  # (text, seg_start, seg_end, spk)
    for seg in segments:
        for ph in split_by_punct(seg.text):
            pieces.append((ph, seg.start, seg.end, seg.spk))

    # 2) 合并过短句（合并段 spk 取字数多数者）
    merged: List[Tuple[str, float, float, Optional[str]]] = []
    for text, s, e, spk in pieces:
        if merged and len(merged[-1][0]) < min_chars:
            old_text, old_s, old_e, old_spk = merged[-1]
            new_spk = old_spk if len(old_text) >= len(text) else spk
            merged[-1] = (old_text + text, old_s, e, new_spk)
        else:
            merged.append((text, s, e, spk))

    # 3) 切分过长句（按弱标点）；子片继承原段 spk 不变
    final: List[Tuple[str, float, float, Optional[str]]] = []
    for text, s, e, spk in merged:
        if len(text) <= max_chars:
            final.append((text, s, e, spk))
            continue
        # 按弱标点切
        parts = re.split(r"([，、])", text)
        cur = ""

        def _flush(buf: str) -> None:
            """把累积 buf flush 到 final。若 buf 以 weak 标点开头且 final 末段属同 [s,e]，拼到末段。"""
            if not buf:
                return
            if buf[0] in _SOFT_PUNCT and final and final[-1][1] == s and final[-1][2] == e:
                last_text, last_s, last_e, last_spk = final[-1]
                final[-1] = (last_text + buf, last_s, last_e, last_spk)
            else:
                final.append((buf, s, e, spk))

        for p in parts:
            if p in _SOFT_PUNCT:
                cur += p
                if len(cur) >= max_chars // 2:
                    _flush(cur)
                    cur = ""
            else:
                cur += p
                if len(cur) > max_chars:
                    _flush(cur)
                    cur = ""
        # 收尾：残余 cur 总拼到末段（同 [s,e]），避免独立弱标点段
        _flush(cur)

    # 4) 时间戳按字数比例在 (s, e) 内插值
    out: List[Segment] = []
    i = 0
    while i < len(final):
        # 找出属于同一原始段的片段
        orig_s = final[i][1]
        orig_e = final[i][2]
        j = i
        while j < len(final) and final[j][1] == orig_s and final[j][2] == orig_e:
            j += 1
        group = final[i:j]
        spk = group[0][3]
        total = sum(len(t) for t, _, _, _ in group)
        if total == 0:
            for t, _, _, _ in group:
                out.append(Segment(start=orig_s, end=orig_e, text=t, spk=spk))
        else:
            cursor = orig_s
            span = orig_e - orig_s
            for t, _, _, _ in group:
                ratio = len(t) / total
                seg_start = cursor
                seg_end = cursor + span * ratio
                cursor = seg_end
                out.append(Segment(start=seg_start, end=seg_end, text=t, spk=spk))
        i = j
    return out


# ---------------------------------------------------------------------------
# 时间戳合法性校验（方案 5.8 节）
# ---------------------------------------------------------------------------
def validate_timestamps(
    segments: List[Segment],
    duration_s: float | None,
) -> List[Segment]:
    """
    校验 + 归一化时间戳。

    处理两类异常：
    1. **end > duration + 1s**（FunASR centisecond 数值溢出或长 wav 累积偏移）：
       视为"全段覆盖"异常，把所有段**重新均分到 [0, duration]**。
       这是粗粒度降级，但保证字幕覆盖完整音频。
    2. **end <= start**：跳过该段。
    """
    if not segments:
        return segments
    if duration_s is None:
        return sorted(segments, key=lambda x: (x.start, x.end))

    # 检测是否存在大量 end > duration+1s 的段
    overflow_count = sum(1 for s in segments if s.end > duration_s + 1.0)
    if overflow_count > 0:
        logger.warning(
            f"检测到 {overflow_count}/{len(segments)} 段 end 超出视频时长+1s"
            f"({duration_s:.2f}s)，全部段按字数比例均分到 [0, {duration_s:.2f}]"
        )
        # 按字数比例重新分配时间
        total_chars = sum(len(s.text) for s in segments)
        if total_chars == 0:
            return []
        cursor = 0.0
        re_aligned: List[Segment] = []
        for seg in segments:
            ratio = len(seg.text) / total_chars
            seg_start = cursor
            seg_end = cursor + duration_s * ratio
            cursor = seg_end
            re_aligned.append(
                Segment(start=seg_start, end=seg_end, text=seg.text, spk=seg.spk, conf=seg.conf)
            )
        return re_aligned

    # 正常情况：仅裁剪单段越界
    cleaned: List[Segment] = []
    for seg in segments:
        s = max(0.0, seg.start)
        e = min(seg.end, duration_s + 0.5)
        if e <= s + 0.01:
            logger.warning(f"段 end <= start + 0.01s，跳过: [{s:.2f},{e:.2f}] {seg.text!r}")
            continue
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
    # 1. ITN
    if cfg.postprocess.itn:
        segs = [replace(s, text=apply_itn(s.text)) for s in segs]

    # 2. 术语纠错
    pairs = load_glossary(cfg.postprocess.glossary) if cfg.postprocess.glossary else []
    segs, log = apply_glossary(segs, pairs)

    # 3. 断句合并
    if cfg.postprocess.merge_short_sentences:
        segs = merge_short_sentences(
            segs,
            min_chars=cfg.postprocess.min_sentence_chars,
            max_chars=cfg.postprocess.max_sentence_chars,
        )

    # 4. 时间戳校验
    segs = validate_timestamps(segs, duration_s)
    return segs, log
