"""课堂数据统计测试：全部确定性计算，零 LLM。"""
from asr.analyze import FILLER_WORDS, compute_stats


def _seg(start, end, text, spk=None):
    s = {"start": start, "end": end, "text": text}
    if spk:
        s["spk"] = spk
    return s


def test_empty_segments():
    s = compute_stats([])
    assert s["duration_sec"] == 0.0 and s["chars"] == 0
    assert s["filler_top"] == [] and s["speakers"] == []
    assert s["questions"] == {}


def test_basic_no_speaker():
    segs = [
        _seg(0.0, 10.0, "那么我们开始上课。"),      # 10 字符
        _seg(10.0, 20.0, "这个就是今天的内容，好吗？"),
    ]
    s = compute_stats(segs)
    assert s["duration_sec"] == 20.0
    assert s["chars"] == 9 + 13           # 去空白后逐字数（brief 原注 10+14 为误算，实测 22）
    assert s["speed_cpm"] == round(22 / (20 / 60), 1)
    # 提问：第二段切句 "这个就是今天的内容，好吗？" → 句尾"吗？"计 1
    assert s["questions"] == {"整体": 1}
    # 口头禅：那么1、这个1、就是1（"吗"不在词表）
    words = [w for w, _ in s["filler_top"]]
    assert set(words) == {"那么", "这个", "就是"}


def test_speaker_buckets():
    segs = [
        _seg(0.0, 40.0, "老师讲四十秒的内容。", spk="教师"),
        _seg(40.0, 50.0, "学生回答。", spk="学生1"),
        _seg(50.0, 60.0, "继续讲。", spk="教师"),
    ]
    s = compute_stats(segs)
    assert s["duration_sec"] == 60.0
    assert [sp["name"] for sp in s["speakers"]] == ["教师", "学生1"]
    assert s["speakers"][0]["duration_sec"] == 50.0
    assert s["speakers"][0]["pct"] == round(50 / 60, 3)
    assert set(s["questions"]) == {"教师", "学生1"}


def test_filler_top_capped_and_zero_dropped():
    text = "那么那么那么那么然后然后这个这个其实"
    s = compute_stats([_seg(0, 10, text)])
    assert s["filler_top"][0] == ("那么", 4)
    assert all(c > 0 for _, c in s["filler_top"])
    assert len(s["filler_top"]) <= 5


def test_question_sentence_split():
    segs = [
        _seg(0, 10, "先讲第一点。接下来提问，大家听懂了吗？没懂的举手。"),
        _seg(10, 20, "很好，我们继续。"),
    ]
    s = compute_stats(segs)
    assert s["questions"] == {"整体": 1}    # 只有"大家听懂了吗？"一条


def test_question_pure_q_mark_and_no_punct():
    """问句两条互补规则：带标点问句靠句尾"？"抓；ASR 丢标点的问句靠末字兜底。

    "去还是不行？"句尾无吗/呢/吧——若切句吞掉"？"则两条规则都够不着，漏计。
    """
    segs = [
        _seg(0, 10, "去还是不行？我们投票决定。"),
        _seg(10, 20, "听懂了吗"),
    ]
    s = compute_stats(segs)
    assert s["questions"] == {"整体": 2}   # 纯"？"问句 1 + 丢标点"吗"兜底 1


def test_filler_word_table_no_single_char():
    """词表不含单字（"啊/嗯"子串误匹配率太高）。"""
    assert all(len(w) >= 2 for w in FILLER_WORDS)
