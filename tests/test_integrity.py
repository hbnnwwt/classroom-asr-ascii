"""Cross-step regressions: preserve text, values, and parser progress."""
import sys

import pytest

from asr.engines.funasr_engine import _split_by_char_ts
from asr.mdrender import render_markdown
from asr.postprocess import apply_itn, validate_timestamps


def test_hard_sentence_boundary_discards_previous_soft_cut():
    text = "甲，乙。" + "一二三四五六七八九十" * 2 + "。"
    timestamps = [[i * 500, i * 500 + 400] for i in range(22)]
    clean = validate_timestamps(_split_by_char_ts(text, timestamps, 15), 15)
    assert "".join(s.text for s in clean) == text
    assert all(s.start <= s.end for s in clean)
    assert all(a.end <= b.start for a, b in zip(clean, clean[1:]))


@pytest.mark.parametrize("text", [
    "百分之一百二十", "百分之三点五", "百分之负三", "百分之九十九点九",
    "百分之三十万", "百分之三千五百", "百分之3.5",
])
def test_unsupported_percent_keeps_the_entire_number(text):
    assert apply_itn(text) == text


@pytest.mark.parametrize("text, expected", [
    ("百分之九十九", "99%"), ("百分之三十五", "35%"),
    ("百分之十", "10%"), ("二〇二三年", "2023年"),
])
def test_supported_number_conversion_remains(text, expected):
    assert apply_itn(text) == expected


@pytest.mark.parametrize("markdown", [
    "| orphan", "plain\n| orphan", "| a |\nnot a separator", "| <script>alert(1)</script>",
])
def test_incomplete_table_always_consumes_input(markdown):
    events = 0
    previous_trace = sys.gettrace()

    def guard(frame, event, arg):
        nonlocal events
        if event == "line" and frame.f_code is render_markdown.__code__:
            events += 1
            if events > 2000:
                raise RuntimeError("Markdown parser did not advance")
        return guard

    try:
        sys.settrace(guard)
        rendered = render_markdown(markdown)
    finally:
        sys.settrace(previous_trace)
    assert "|" in rendered
    assert "<table>" not in rendered
    assert "<script>" not in rendered
