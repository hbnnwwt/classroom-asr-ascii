"""diarize.py 单测。"""
from asr.engines.base import Segment
from asr.diarize import (
    aggregate_durations, assign_roles, apply_roles, diarize,
)


def test_aggregate_durations():
    """按时长聚合 spk。"""
    segs = [
        Segment(start=0.0, end=10.0, text="a", spk="spk0"),
        Segment(start=10.0, end=20.0, text="b", spk="spk0"),
        Segment(start=20.0, end=25.0, text="c", spk="spk1"),
    ]
    d = aggregate_durations(segs)
    assert d == {"spk0": 20.0, "spk1": 5.0}


def test_aggregate_durations_skips_no_spk():
    """无 spk 的段不计入聚合。"""
    segs = [
        Segment(start=0.0, end=10.0, text="a", spk="spk0"),
        Segment(start=10.0, end=15.0, text="b", spk=None),
    ]
    d = aggregate_durations(segs)
    assert d == {"spk0": 10.0}


def test_assign_roles_teacher_is_longest():
    """时长最大者 → 教师。"""
    segs = [
        Segment(start=0.0, end=60.0, text="老师讲 60s", spk="spk0"),
        Segment(start=60.0, end=70.0, text="学生 10s", spk="spk1"),
    ]
    role = assign_roles(segs, max_speakers=4)
    assert role == {"spk0": "教师", "spk1": "学生A"}


def test_assign_roles_max_speakers_limits_students():
    """max_speakers=4 时，教师 + 3 个学生共 4 个角色；超出者 → 其他。"""
    segs = [
        Segment(start=0.0, end=60.0, text="t", spk="spk0"),
        Segment(start=60.0, end=70.0, text="s1", spk="spk1"),
        Segment(start=70.0, end=80.0, text="s2", spk="spk2"),
        Segment(start=80.0, end=90.0, text="s3", spk="spk3"),
        Segment(start=90.0, end=100.0, text="s4", spk="spk4"),  # 第 5 个
    ]
    role = assign_roles(segs, max_speakers=4)
    assert role["spk0"] == "教师"
    # spk1-spk3 → 学生A/B/C
    students = [v for k, v in role.items() if v.startswith("学生")]
    assert len(students) == 3
    # spk4 → 其他
    assert role["spk4"] == "其他"


def test_assign_roles_student_order_by_duration_then_first_seen():
    """学生角色按时长降序；同长度时按首次出现顺序。"""
    segs = [
        Segment(start=0.0, end=60.0, text="t", spk="spk0"),   # 教师 60s
        Segment(start=60.0, end=80.0, text="x", spk="spk1"),  # 20s，先出现
        Segment(start=80.0, end=100.0, text="y", spk="spk2"), # 20s，后出现
    ]
    role = assign_roles(segs, max_speakers=4)
    assert role == {"spk0": "教师", "spk1": "学生A", "spk2": "学生B"}


def test_apply_roles_replaces_spk():
    """apply_roles 把 spk 替换为角色名。"""
    segs = [
        Segment(start=0.0, end=10.0, text="a", spk="spk0"),
        Segment(start=10.0, end=15.0, text="b", spk="spk1"),
    ]
    role_map = {"spk0": "教师", "spk1": "学生A"}
    out = apply_roles(segs, role_map)
    assert out[0].spk == "教师"
    assert out[1].spk == "学生A"
    # 原对象未改
    assert segs[0].spk == "spk0"


def test_apply_roles_empty_map_noop():
    """空 role_map → 原样返回。"""
    segs = [Segment(start=0.0, end=1.0, text="a", spk="spk0")]
    out = apply_roles(segs, {})
    assert out[0].spk == "spk0"


def test_diarize_one_shot():
    """一站式 diarize：聚合 + 分配 + 应用。"""
    segs = [
        Segment(start=0.0, end=60.0, text="老师讲", spk="spk0"),
        Segment(start=60.0, end=70.0, text="学生问", spk="spk1"),
    ]
    out, role_map = diarize(segs, max_speakers=4)
    assert role_map == {"spk0": "教师", "spk1": "学生A"}
    assert out[0].spk == "教师"
    assert out[1].spk == "学生A"


def test_assign_roles_empty_segments():
    """空段列表 → 空映射。"""
    role = assign_roles([], max_speakers=4)
    assert role == {}