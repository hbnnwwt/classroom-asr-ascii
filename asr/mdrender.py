"""极简 Markdown → HTML：只覆盖本项目 prompt 硬性约定的语法。

LLM 产物（.feedback.md / .polished.md）要在线阅读，渲染器只认
asr/feedback.py 里约定的格式：标题 / 段落 / 加粗 / 行内代码 /
无序有序列表 / 表格。宁缺毋滥——不做万能解析器；约定外的语法按
原文显示。渲染部分只用标准库。

修改痕迹（render_diff）用 diff-match-patch 引擎（Google Docs 同源，
纯 Python 无传递依赖）：字符级天然适配中文，自带 1s deadline——
整篇重写的极端情况宁可标记粗一点，也不许卡死请求。

安全铁律：LLM 输出是不可信数据。先全文 html.escape 再做 md 变换，
任何标签注入只能变成纯文本。
"""
from __future__ import annotations

import html
import re

from diff_match_patch import diff_match_patch

_H = re.compile(r"^(#{1,4})\s+(.*)$")
_UL = re.compile(r"^\s*[-*]\s+(.*)$")
_OL = re.compile(r"^\s*\d+\.\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")


def _inline(s: str) -> str:
    """行内变换（在已转义文本上）：加粗、行内代码。"""
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    return _CODE.sub(r"<code>\1</code>", s)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render_markdown(text: str) -> str:
    lines = [html.escape(ln) for ln in text.splitlines()]
    out: list[str] = []
    i, n = 0, len(lines)

    def _is_table_start(index: int) -> bool:
        return (lines[index].lstrip().startswith("|") and index + 1 < n
                and bool(_TABLE_SEP.match(lines[index + 1])))

    def _is_block_start(index: int) -> bool:
        s = lines[index]
        return bool(_H.match(s) or _UL.match(s) or _OL.match(s)
                    or _is_table_start(index))

    while i < n:
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue

        m = _H.match(ln)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue

        # 表格：| 开头且下一行是 |---| 分隔行
        if _is_table_start(i):
            out.append("<table><thead><tr>"
                       + "".join(f"<th>{_inline(c)}</th>" for c in _cells(ln))
                       + "</tr></thead><tbody>")
            i += 2
            while i < n and lines[i].lstrip().startswith("|"):
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>"
                                            for c in _cells(lines[i])) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue

        m = _UL.match(ln)
        if m:
            items = []
            while i < n and (mm := _UL.match(lines[i])):
                items.append(mm.group(1))
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(it)}</li>"
                                        for it in items) + "</ul>")
            continue

        m = _OL.match(ln)
        if m:
            items = []
            while i < n and (mm := _OL.match(lines[i])):
                items.append(mm.group(1))
                i += 1
            out.append("<ol>" + "".join(f"<li>{_inline(it)}</li>"
                                        for it in items) + "</ol>")
            continue

        # 段落：吃到下一个块级标记或空行为止
        para = []
        while i < n and lines[i].strip() and not _is_block_start(i):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# 红字校对视图：原稿 vs 优化稿
# ---------------------------------------------------------------------------
_DMP = diff_match_patch()
_DMP.Diff_Timeout = 1.0   # 秒（相对值）：bisect 超时兜底，整篇重写耗时封顶


def render_diff(original: str, polished: str) -> str:
    """红字校对视图：删除=红色划线（原文可见），新增=绿色。

    一遍字符级 diff + cleanupSemantic（把编辑边界归整到人类可读处），
    不再需要"句子对齐 + 块内精修"两级方案。deadline=1s：极端整篇
    重写时降级为较粗的标记，耗时封顶。换行在 equal 块里是段落边界；
    划线/新增块里的换行不显示（划线段落的换行只是噪音）。优化稿
    末尾"## 本课要点"是纯新增节，不参与比对，按普通 md 渲染接在
    后面。
    """
    m = re.search(r"^##\s*本课要点\s*$", polished, re.M)
    tail_md = ""
    if m:
        tail_md = polished[m.start():]
        polished = polished[:m.start()].rstrip("\n")

    # 不传 deadline 位置参数——它按绝对时间戳解读，传 1.0 会立刻"过期"
    # 退化成整删整增；超时兜底交给实例的 Diff_Timeout。
    diffs = _DMP.diff_main(original, polished)
    _DMP.diff_cleanupSemantic(diffs)

    parts: list[str] = []
    buf: list[str] = []

    def _flush():
        if buf:
            parts.append("<p>" + "".join(buf) + "</p>")
            buf.clear()

    for op, text in diffs:
        tag = None if op == 0 else ("del" if op < 0 else "ins")
        for i, seg in enumerate(text.split("\n")):
            if i:                       # seg 前有换行（段落边界）
                if tag:
                    continue            # 划线/新增块里的换行不显示
                _flush()
            if seg:
                esc = html.escape(seg)
                buf.append(esc if tag is None
                           else f'<{tag} class="diff-{tag}">{esc}</{tag}>')
    _flush()
    if tail_md:
        parts.append(render_markdown(tail_md))
    return "\n".join(parts)
