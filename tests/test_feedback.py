"""prompt 构建测试：不调 LLM，只验结构与硬约束在文。"""
import json

from asr.feedback import build_feedback_messages, build_polish_messages

STATS = {"duration_sec": 1800.0, "chars": 9000, "speed_cpm": 300.0,
         "filler_top": [("就是", 47)], "questions": {"教师": 12},
         "speakers": []}
TEXT = "教师: 那么我们开始上课。"


def test_feedback_messages_structure():
    msgs = build_feedback_messages(TEXT, STATS)
    assert [m["role"] for m in msgs] == ["system", "user"]
    # 统计事实原样进 user（LLM 只许引用给定数字，不许自己算）
    assert json.dumps(STATS, ensure_ascii=False) in msgs[1]["content"]
    assert TEXT in msgs[1]["content"]


def test_feedback_system_has_constraints():
    sysmsg = build_feedback_messages(TEXT, STATS)[0]["content"]
    for kw in ("讲解结构", "节奏", "师生互动", "语言表达",
               "不得编造", "疑似识别错误", "依据"):
        assert kw in sysmsg, f"system prompt 缺少约束关键词: {kw}"


def test_polish_messages_constraints():
    msgs = build_polish_messages(TEXT)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[1]["content"] == TEXT
    for kw in ("不改原意", "不增删信息", "本课要点", "口头禅"):
        assert kw in msgs[0]["content"], f"polish prompt 缺少红线: {kw}"
