"""AI 反馈的 prompt 构建：点评报告 + 优化逐字稿，两次独立调用。

点评与优化分开调：合输出会顶模型输出上限（截断风险真实），分开各自
5~8 千 token，稳，且独立重试互不连坐。
"""
from __future__ import annotations

import json

FEEDBACK_SYSTEM = """你是一位资深的教学评课专家兼文字编辑。用户会提供一段课堂录音的逐字稿（可能带"说话人: "前缀）和一份课堂数据统计（JSON）。请输出 Markdown 反馈报告，结构固定：

# AI 课堂反馈

## 一、教学反馈
依次四个小节：### 讲解结构、### 节奏、### 师生互动、### 语言表达。
每条意见必须附依据，格式："依据：〈原文引用（≤50 字）或统计事实〉"。
先写具体优点，再写改进建议，建议合计不超过 6 条。

## 二、稿件反馈
- 疑似识别错误：逐条列出原句与猜测正字（同音字、专有名词等）
- 可读性问题：长句无标点、指代不明等

## 三、课堂数据
把统计 JSON 整理成表格（语速/提问/口头禅/说话人占比）。

硬性约束：
- 不得编造原文不存在的内容
- 统计数字只能引用给定 JSON 里的值，不得自行计算或估算
- 全文用中文，直接输出报告，不要任何前言或解释。"""

POLISH_SYSTEM = """你是一位细心的文字编辑。把下面的课堂逐字稿整理成可读版本，输出 Markdown：
1. 删除口头禅和无意义重复，修正标点，按语义分段
2. 红线：不改原意、不增删信息、不做书面化改写，保留口语的自然度
3. 文末加 "## 本课要点" 一节：3~6 条要点，每条一行
全文用中文，直接输出整理稿，不要任何前言或解释。"""


def build_feedback_messages(transcript: str, stats: dict) -> list[dict]:
    user = ("课堂逐字稿：\n" + transcript
            + "\n\n课堂数据统计（JSON）：\n"
            + json.dumps(stats, ensure_ascii=False))
    return [{"role": "system", "content": FEEDBACK_SYSTEM},
            {"role": "user", "content": user}]


def build_polish_messages(transcript: str) -> list[dict]:
    return [{"role": "system", "content": POLISH_SYSTEM},
            {"role": "user", "content": transcript}]
