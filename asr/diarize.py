"""
说话人分离后处理（方案 5.6 节）：
- 输入：FunASR 返回的带 spk 标签的 segments（spk="spk0"/"spk1"/...）
- 输出：把 spk 标签映射为"教师 / 学生A / 学生B / 学生C / 其他"
- 规则：按时长占比降序 → 教师；其余按出现顺序 → 学生A/B/C；超 max_speakers → 其他

注意：
- 全班齐读/齐答时分离结果不可靠，应关闭 --diarize
- 不修改 segments 文本，只改 spk 字段
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Tuple

from loguru import logger

from .engines.base import Segment


# 角色标签（按时长排序映射）
_ROLE_TEACHER = "教师"
_ROLE_OTHERS = "其他"
_STUDENT_LABELS = ["学生A", "学生B", "学生C", "学生D", "学生E"]


def aggregate_durations(segments: List[Segment]) -> Dict[str, float]:
    """统计每个 spk 的总说话时长（秒）。"""
    durations: Dict[str, float] = defaultdict(float)
    for s in segments:
        if s.spk:
            durations[s.spk] += s.duration
    return dict(durations)


def assign_roles(
    segments: List[Segment],
    *,
    max_speakers: int = 4,
) -> Dict[str, str]:
    """
    给每个 spk 分配角色标签。

    规则：
    1. 按时长降序排，第一名 → 教师
    2. 其余按出现顺序 → 学生A / 学生B / 学生C / ...
    3. 总说话人数 > max_speakers 时，最后多余者 → "其他"

    Args:
        segments: 引擎返回的原始段（spk="spk0"/"spk1"/...）
        max_speakers: 保留的最大说话人数；超出者统一标"其他"

    Returns:
        {原始spk: 角色名} 映射，例如 {"spk0": "教师", "spk1": "学生A", "spk2": "学生B"}
    """
    durations = aggregate_durations(segments)
    if not durations:
        return {}

    # 按时长降序排（最大者优先）
    by_duration = sorted(durations.items(), key=lambda kv: -kv[1])
    n_speakers = len(by_duration)

    # 按"出现顺序"排序非最长者：先扫 segments 取每个 spk 首次出现索引
    first_seen: Dict[str, int] = {}
    for i, s in enumerate(segments):
        if s.spk and s.spk not in first_seen:
            first_seen[s.spk] = i

    # 时长最长者 → 教师
    role_map: Dict[str, str] = {}
    role_map[by_duration[0][0]] = _ROLE_TEACHER

    # 其余按时长降序排（同时长时按首次出现顺序）
    rest = by_duration[1:]
    rest.sort(key=lambda kv: (-kv[1], first_seen.get(kv[0], 0)))

    # 在 max_speakers-1 个槽位内分配学生角色
    student_slots = max_speakers - 1  # 教师占 1 个
    for i, (spk, _dur) in enumerate(rest):
        if i < student_slots and i < len(_STUDENT_LABELS):
            role_map[spk] = _STUDENT_LABELS[i]
        else:
            role_map[spk] = _ROLE_OTHERS

    logger.info(
        f"[diarize] 检测到 {n_speakers} 个说话人，max_speakers={max_speakers}："
        + ", ".join(f"{spk}={role_map[spk]} ({durations[spk]:.1f}s)" for spk, _ in by_duration)
    )
    return role_map


def apply_roles(
    segments: List[Segment],
    role_map: Dict[str, str],
) -> List[Segment]:
    """
    把 segments 里的 spk 替换为角色标签。返回新列表，不修改原对象。
    """
    from dataclasses import replace
    if not role_map:
        return segments
    out: List[Segment] = []
    for s in segments:
        if s.spk and s.spk in role_map:
            out.append(replace(s, spk=role_map[s.spk]))
        else:
            out.append(s)
    return out


def diarize(
    segments: List[Segment],
    *,
    max_speakers: int = 4,
) -> Tuple[List[Segment], Dict[str, str]]:
    """
    一站式：聚合 → 分配 → 应用。

    Returns:
        (新段列表, 角色映射)
    """
    role_map = assign_roles(segments, max_speakers=max_speakers)
    return apply_roles(segments, role_map), role_map