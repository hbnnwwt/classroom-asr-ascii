"""mdrender：LLM 产物 markdown → HTML 的极简渲染器测试。

覆盖面 = 本项目 prompt 硬性约定的语法（asr/feedback.py）：
标题 / 段落 / 加粗 / 行内代码 / 无序有序列表 / 表格 / 全文转义防注入。
"""
import pytest

from asr.mdrender import render_markdown


# ---------------------------------------------------------------------------
# 标题：# ## ### ####（prompt 用到三级，留到四级）
# ---------------------------------------------------------------------------
def test_headings():
    out = render_markdown("# 一级\n## 二级\n### 三级")
    assert "<h1>一级</h1>" in out
    assert "<h2>二级</h2>" in out
    assert "<h3>三级</h3>" in out


def test_heading_text_escaped_and_inline():
    """标题里的内联语法照常生效，标签照常转义。"""
    out = render_markdown("## **重点**：<x>")
    assert "<strong>重点</strong>" in out
    assert "<x>" not in out


# ---------------------------------------------------------------------------
# 段落：连续普通行合并；空行分段
# ---------------------------------------------------------------------------
def test_paragraph_joins_lines():
    out = render_markdown("第一行\n第二行")
    assert "<p>第一行 第二行</p>" in out


def test_blank_line_splits_paragraphs():
    out = render_markdown("段一\n\n段二")
    assert "<p>段一</p>" in out
    assert "<p>段二</p>" in out


# ---------------------------------------------------------------------------
# 行内：加粗、行内代码
# ---------------------------------------------------------------------------
def test_bold():
    assert "<strong>重点</strong>" in render_markdown("**重点**")


def test_inline_code():
    assert "<code>x=1</code>" in render_markdown("变量 `x=1` 结束")


def test_plain_text_untouched():
    """没有星号的文本不许被塞进 strong。"""
    assert "<strong>" not in render_markdown("普通文本 2*3=6")


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------
def test_unordered_list():
    out = render_markdown("- 甲\n- 乙")
    assert out.count("<li>") == 2
    assert "<li>甲</li>" in out and "<li>乙</li>" in out
    assert "<ul>" in out and "</ul>" in out


def test_ordered_list():
    out = render_markdown("1. 第一\n2. 第二")
    assert "<ol>" in out and "</ol>" in out
    assert "<li>第一</li>" in out


def test_list_items_inline_bold():
    """列表项里的加粗照常渲染（点评建议常见）。"""
    assert "<li><strong>优点</strong>：好</li>" in render_markdown("- **优点**：好")


# ---------------------------------------------------------------------------
# 表格：prompt 第三节"课堂数据"硬性要求表格
# ---------------------------------------------------------------------------
def test_table():
    md = "| 指标 | 数值 |\n|---|---|\n| 语速 | 132 |\n| 提问数 | 18 |"
    out = render_markdown(md)
    assert "<table>" in out and "</table>" in out
    assert "<th>指标</th>" in out and "<th>数值</th>" in out
    assert "<td>132</td>" in out and "<td>18</td>" in out
    assert "<td>语速</td>" in out  # 第一列也是数据


def test_table_inline_and_separator_variants():
    """表头带加粗、分隔行带冒号对齐写法都要活。"""
    md = "| **指标** | 值 |\n|:---|---:|\n| a | 1 |"
    out = render_markdown(md)
    assert "<th><strong>指标</strong></th>" in out
    assert "<td>a</td>" in out


def test_lone_pipe_line_not_table():
    """单行 | 开头但没有分隔行 → 不是表格，按段落处理不炸。"""
    out = render_markdown("他说：| 分隔符")
    assert "<table>" not in out
    assert "|" in out


# ---------------------------------------------------------------------------
# 安全：LLM 输出是不可信数据，先转义后渲染
# ---------------------------------------------------------------------------
def test_xss_script_neutralized():
    out = render_markdown("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_xss_img_onerror_neutralized():
    out = render_markdown("看图 <img src=x onerror=alert(1)> 完")
    assert "<img" not in out
    assert "&lt;img src=x onerror=alert(1)&gt;" in out


def test_xss_in_table_cell():
    md = "| a | b |\n|---|---|\n| <b>bold</b> | 1 |"
    out = render_markdown(md)
    assert "<b>" not in out
    assert "&lt;b&gt;bold&lt;/b&gt;" in out


# ---------------------------------------------------------------------------
# 未知语法降级：约定外的花哨语法按原文显示，不崩不吞
# ---------------------------------------------------------------------------
def test_link_syntax_stays_literal():
    """[文字](url) 不在约定内 → 原样保留，不造 <a>。"""
    out = render_markdown("见 [文档](http://x)")
    assert "<a " not in out
    assert "[文档](http://x)" in out


def test_horizontal_rule_is_plain_text():
    out = render_markdown("上文\n\n---\n\n下文")
    assert "---" in out          # 不吞内容
    assert "<hr" not in out      # 也不假装认识


def test_empty_input():
    assert render_markdown("") == ""
    assert render_markdown("\n\n\n") == ""


# ---------------------------------------------------------------------------
# 真实形状：一份按 prompt 生成的迷你点评
# ---------------------------------------------------------------------------
def test_realistic_feedback_shape():
    md = (
        "# AI 课堂反馈\n"
        "\n"
        "## 一、教学反馈\n"
        "### 讲解结构\n"
        "- **优点**：开头明确目标。依据：〈今天我们讲第三章〉\n"
        "- 建议：段落间小结。依据：〈连续 12 分钟无停顿〉\n"
        "\n"
        "## 三、课堂数据\n"
        "| 指标 | 数值 |\n"
        "|---|---|\n"
        "| 语速(字/分) | 132 |\n"
    )
    out = render_markdown(md)
    for frag in ("<h1>AI 课堂反馈</h1>", "<h2>一、教学反馈</h2>",
                 "<h3>讲解结构</h3>", "<strong>优点</strong>",
                 "<th>指标</th>", "<td>132</td>"):
        assert frag in out, f"缺: {frag}"


# ---------------------------------------------------------------------------
# 红字校对视图 render_diff：原稿 vs 优化稿（句子对齐 + 字级精修）
# ---------------------------------------------------------------------------
from asr.mdrender import render_diff


def test_diff_identical_no_marks():
    out = render_diff("今天讲第三章。", "今天讲第三章。")
    assert "diff-del" not in out and "diff-ins" not in out
    assert "今天讲第三章。" in out


def test_diff_deleted_fillers_struck_out():
    """删掉的口头禅：del 划线保留原文可见。"""
    out = render_diff("那个我们今天讲第三章。", "我们今天讲第三章。")
    assert '<del class="diff-del">那个</del>' in out
    assert "diff-ins" not in out


def test_diff_punctuation_fix_inline():
    """标点修正：字级精修只标出变化的字符对。"""
    out = render_diff("今天讲,第三章。", "今天讲，第三章。")
    assert '<del class="diff-del">,</del>' in out
    assert '<ins class="diff-ins">，</ins>' in out
    assert "今天讲" in out


def test_diff_added_sentence_marked_ins():
    out = render_diff("第一句。", "第一句。新加的句子。")
    assert '<ins class="diff-ins">新加的句子。</ins>' in out


def test_diff_summary_tail_not_compared():
    """"## 本课要点"是新增节：不参与比对，按 md 渲染接在后面。"""
    out = render_diff("第一句。", "第一句。\n\n## 本课要点\n- 要点一\n")
    assert "diff-ins" not in out          # 要点不标绿
    assert "<h2>本课要点</h2>" in out     # 正常渲染


def test_diff_paragraph_breaks_preserved():
    out = render_diff("第一段。\n第二段。", "第一段。\n第二段。")
    assert out.count("<p>") == 2


def test_diff_escapes_html():
    out = render_diff("<script>a</script>。", "正常。")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_diff_big_rewrite_bounded():
    """4400 字级整段重写也要秒回：diff 引擎自带 1s deadline 兜底，
    宁可标记粗一点也不许把同步请求卡死（difflib 两级版同款输入实测
    8.9s 且无上限）。"""
    import time
    a = "今天我们讲第三章的内容，首先回顾上节课的要点。" * 200
    b = "本节课进入第三章节，先复习上一讲的核心知识。" * 200
    t0 = time.perf_counter()
    out = render_diff(a, b)
    dt = time.perf_counter() - t0
    assert '<del class="diff-del">' in out and '<ins class="diff-ins">' in out
    assert out.count("<del") == out.count("</del>")   # deadline 降级也不许吐残缺 HTML
    assert dt < 6.0, f"整段重写耗时 {dt:.2f}s —— 性能回归"
