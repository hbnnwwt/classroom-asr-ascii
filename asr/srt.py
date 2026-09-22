"""
输出格式化（方案 5.7 节）：
- SRT / VTT 字幕
- MD 带说话人
- TXT 可选时间戳
- JSON 一等产物（含 meta + segments）

所有时间戳统一用 fmt_ts 格式化。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import List

from loguru import logger

from .config import OutputConfig
from .engines.base import Segment
from .utils import ensure_dir


# ---------------------------------------------------------------------------
# 时间戳格式化
# ---------------------------------------------------------------------------
def fmt_ts(seconds: float, *, srt: bool = True) -> str:
    """
    秒 → 字幕时间字符串。
    SRT: 00:01:02,345 （毫秒，逗号分隔）
    VTT: 00:01:02.345 （毫秒，点分隔）
    """
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    h = total_ms // 3_600_000
    m = (total_ms % 3_600_000) // 60_000
    s = (total_ms % 60_000) // 1000
    ms = total_ms % 1000
    sep = "," if srt else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


# ---------------------------------------------------------------------------
# SRT 行长度切分（方案 5.5 / 5.7）
# ---------------------------------------------------------------------------
def _wrap_text(text: str, max_chars_per_line: int) -> List[str]:
    """按 max_chars_per_line 切分文本为多行（按字符粗切，不做语言感知）。

    后处理：把以 weak 标点（，、）开头的行拼到上一行末尾，
    避免"，"、"、"等成为独立一行（视觉上断裂）。
    """
    if len(text) <= max_chars_per_line:
        return [text]
    lines = []
    for i in range(0, len(text), max_chars_per_line):
        lines.append(text[i:i + max_chars_per_line])
    # 合并以 weak 标点开头的行到上一行
    merged: List[str] = []
    _SOFT = "，、"
    for line in lines:
        if line and line[0] in _SOFT and merged:
            merged[-1] = merged[-1] + line
        else:
            merged.append(line)
    return merged


# ---------------------------------------------------------------------------
# SRT 写出
# ---------------------------------------------------------------------------
def write_srt(segments: List[Segment], path: Path, cfg: OutputConfig) -> None:
    """
    SRT 格式：
      1
      00:00:01,500 --> 00:00:04,200
      大家好，欢迎来到课堂。

    规则：
      - 每行 ≤ srt_max_chars_per_line（行内切分，不动时间戳）
      - 段时长 > srt_max_duration_s 仅打 warning，不硬切条
        （避免按字符粗切导致 3 字一段破坏可读性；
         FunASR 粗粒度时间戳的根治在引擎层而非 SRT 层）
    """
    ensure_dir(path.parent)
    max_chars = cfg.srt_max_chars_per_line
    max_dur = cfg.srt_max_duration_s

    out_lines: List[str] = []
    idx = 1
    for seg in segments:
        if seg.end <= seg.start or not seg.text.strip():
            continue
        duration = seg.end - seg.start
        if duration > max_dur:
            logger.warning(
                f"SRT 段时长 {duration:.1f}s > {max_dur}s，保留原始时间戳不切条: "
                f"[{seg.start:.2f}->{seg.end:.2f}] {seg.text!r}"
            )
        _emit_srt_entry(out_lines, idx, seg.start, seg.end, seg.text, max_chars)
        idx += 1

    path.write_text("\n".join(out_lines) + "\n", encoding=cfg.srt_encoding)


def _emit_srt_entry(out: List[str], idx: int, s: float, e: float, text: str, max_chars: int):
    out.append(str(idx))
    out.append(f"{fmt_ts(s, srt=True)} --> {fmt_ts(e, srt=True)}")
    for line in _wrap_text(text, max_chars):
        out.append(line)
    out.append("")  # 空行分隔


# ---------------------------------------------------------------------------
# VTT 写出
# ---------------------------------------------------------------------------
def write_vtt(segments: List[Segment], path: Path, cfg: OutputConfig) -> None:
    ensure_dir(path.parent)
    out_lines = ["WEBVTT", ""]
    for i, seg in enumerate(segments, 1):
        if seg.end <= seg.start:
            continue
        out_lines.append(str(i))
        out_lines.append(f"{fmt_ts(seg.start, srt=False)} --> {fmt_ts(seg.end, srt=False)}")
        for line in _wrap_text(seg.text, cfg.srt_max_chars_per_line):
            out_lines.append(line)
        out_lines.append("")
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# MD 写出（带说话人）
# ---------------------------------------------------------------------------
def write_md(segments: List[Segment], path: Path, *, with_speaker: bool = False) -> None:
    ensure_dir(path.parent)
    out_lines = ["# 转写结果", ""]
    for seg in segments:
        ts = fmt_ts(seg.start, srt=True)
        if with_speaker and seg.spk:
            # spk0 → "说话人0"
            label = f"说话人{seg.spk.replace('spk', '')}"
            out_lines.append(f"**[{ts}] {label}：** {seg.text}")
        else:
            out_lines.append(f"**[{ts}]** {seg.text}")
        out_lines.append("")
    path.write_text("\n".join(out_lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# TXT 写出
# ---------------------------------------------------------------------------
def write_txt(segments: List[Segment], path: Path, *, with_ts: bool = False) -> None:
    ensure_dir(path.parent)
    out_lines = []
    for seg in segments:
        if with_ts:
            out_lines.append(f"[{fmt_ts(seg.start, srt=True)}] {seg.text}")
        else:
            out_lines.append(seg.text)
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# JSON 写出（一等产物）
# ---------------------------------------------------------------------------
def write_json(
    segments: List[Segment],
    path: Path,
    *,
    meta: dict | None = None,
    replace_log: list[str] | None = None,
) -> None:
    """
    JSON 结构：
    {
      "meta": {file, duration, engine, model, language, created_at, elapsed_s, rtf},
      "segments": [{start, end, text, spk, conf}],
      "replace_log": [...]
    }
    """
    ensure_dir(path.parent)
    meta = meta or {}
    meta.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    payload = {
        "meta": meta,
        "segments": [
            {
                "start": round(s.start, 3),
                "end": round(s.end, 3),
                "text": s.text,
                "spk": s.spk,
                "conf": s.conf,
            }
            for s in segments
        ],
    }
    if replace_log:
        payload["replace_log"] = replace_log
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 一站式写出
# ---------------------------------------------------------------------------
def write_all(
    segments: List[Segment],
    base_path: Path,
    cfg: OutputConfig,
    *,
    meta: dict | None = None,
    replace_log: list[str] | None = None,
) -> dict[str, Path]:
    """根据 cfg.formats 写出一组文件，返回 {format: path}。"""
    base_path = Path(base_path)
    ensure_dir(base_path.parent if base_path.suffix else base_path)
    written: dict[str, Path] = {}
    if "srt" in cfg.formats:
        p = base_path.with_suffix(".srt")
        write_srt(segments, p, cfg)
        written["srt"] = p
    if "vtt" in cfg.formats:
        p = base_path.with_suffix(".vtt")
        write_vtt(segments, p, cfg)
        written["vtt"] = p
    if "txt" in cfg.formats:
        p = base_path.with_suffix(".txt")
        write_txt(segments, p, with_ts=False)
        written["txt"] = p
    if "md" in cfg.formats:
        p = base_path.with_suffix(".md")
        write_md(segments, p, with_speaker=any(s.spk for s in segments))
        written["md"] = p
    if "json" in cfg.formats:
        p = base_path.with_suffix(".json")
        write_json(segments, p, meta=meta, replace_log=replace_log)
        written["json"] = p
    return written
