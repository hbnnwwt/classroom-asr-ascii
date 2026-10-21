"""极简 Markdown → HTML：只覆盖本项目 prompt 硬性约定的语法。

LLM 产物（.feedback.md / .polished.md）要在线阅读，渲染器只认
asr/feedback.py 里约定的格式：标题 / 段落 / 加粗 / 行内代码 /
无序有序列表 / 表格。宁缺毋滥——不引第三方依赖（项目全程离线），
不做万能解析器；约定外的语法按原文显示。

安全铁律：LLM 输出是不可信数据。先全文 html.escape 再做 md 变换，
任何标签注入只能变成纯文本。
"""
from __future__ import annotations

import difflib
import html
import re

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

    def _is_block_start(s: str) -> bool:
        return bool(_H.match(s) or _UL.match(s) or _OL.match(s)
                    or s.lstrip().startswith("|"))

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
        if ln.lstrip().startswith("|") and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
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
        while i < n and lines[i].strip() and not _is_block_start(lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# 红字校对视图：原稿 vs 优化稿
# ---------------------------------------------------------------------------
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")


def _sentences(text: str) -> list[str]:
    """分句 token：句末标点后断开；换行独立成 token（段落边界）。"""
    toks: list[str] = []
    for line in text.split("\n"):
        toks.extend(t for t in _SENT_SPLIT.split(line) if t)
        toks.append("\n")
    return toks[:-1]  # 末尾换行不是段落边界


def render_diff(original: str, polished: str) -> str:
    """红字校对视图：删除=红色划线（原文可见），新增=绿色。

    两级 diff（全 stdlib）：句子对齐管全局（几百个 token，快），
    替换块内做字级精修（git --word-diff 的味道）——标点修正只标
    变化的字符，整句重写才整句换色。优化稿末尾"## 本课要点"是
    纯新增节，不参与比对，按普通 md 渲染接在后面。
    """
    m = re.search(r"^##\s*本课要点\s*$", polished, re.M)
    tail_md = ""
    if m:
        tail_md = polished[m.start():]
        polished = polished[:m.start()].rstrip("\n")

    def _del(s): return f'<del class="diff-del">{html.escape(s)}</del>'
    def _ins(s): return f'<ins class="diff-ins">{html.escape(s)}</ins>'

    parts: list[str] = []
    buf: list[str] = []

    def _flush():
        if buf:
            parts.append("<p>" + "".join(buf) + "</p>")
            buf.clear()

    a, b = _sentences(original), _sentences(polished)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            for t in a[i1:i2]:
                _flush() if t == "\n" else buf.append(html.escape(t))
        elif tag == "delete":
            # 删掉的段落边界不显示（划线段落的换行只是噪音）
            buf.extend(_del(t) for t in a[i1:i2] if t != "\n")
        elif tag == "insert":
            for t in b[j1:j2]:
                _flush() if t == "\n" else buf.append(_ins(t))
        else:  # replace：块内字级精修
            ta = "".join(t for t in a[i1:i2] if t != "\n")
            tb = "".join(t for t in b[j1:j2] if t != "\n")
            for ct, ci1, ci2, cj1, cj2 in difflib.SequenceMatcher(
                    a=ta, b=tb, autojunk=False).get_opcodes():
                if ct == "equal":
                    buf.append(html.escape(ta[ci1:ci2]))
                elif ct == "delete":
                    buf.append(_del(ta[ci1:ci2]))
                elif ct == "insert":
                    buf.append(_ins(tb[cj1:cj2]))
                else:
                    buf.append(_del(ta[ci1:ci2]))
                    buf.append(_ins(tb[cj1:cj2]))
    _flush()
    if tail_md:
        parts.append(render_markdown(tail_md))
    return "\n".join(parts)
