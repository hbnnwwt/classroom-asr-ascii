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

# 切句含全角/半角"？"：切掉后问句靠末字"吗/呢/吧"兜底识别——
# 若不切"？"，文中问句（…吗？没懂的…）会并入后句、句尾变非问字，漏计。
_SENT_SPLIT = re.compile(r"[。！！??？]")
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
