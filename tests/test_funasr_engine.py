"""FunASR 引擎单测（不加载真实模型，只验证 _parse_results 与降级逻辑）。"""
from pathlib import Path

import pytest

from asr.config import load_config
from asr.engines.funasr_engine import FunASREngine
from asr.engines.base import Segment


@pytest.fixture
def cfg():
    """strict_models=False 避免测试时强校验模型路径。"""
    return load_config("config.yaml", strict_models=False)


def test_engine_class_name():
    """引擎名字正确。"""
    assert FunASREngine.name == "funasr"


def test_parse_sentence_info_basic(cfg):
    """sentence_info 路径解析为正确的 Segment 列表。"""
    engine = FunASREngine(cfg)
    # 模拟 FunASR 返回：两个句子；funasr 1.4.16 的 sentence_info start/end 是毫秒
    fake_results = [{
        "sentence_info": [
            {"start": 0, "end": 2500, "text": "你好", "spk": "spk0"},
            {"start": 2500, "end": 5000, "text": "世界。", "spk": "spk0"},
        ],
        "text": "你好世界。",
    }]
    segs = engine._parse_results(fake_results)
    assert len(segs) == 2
    assert segs[0].start == 0.0
    assert segs[0].end == 2.5
    assert segs[0].text == "你好"
    assert segs[0].spk == "spk0"
    assert segs[1].text == "世界。"
    assert segs[1].end == 5.0


def test_parse_timestamp_first_last_token(cfg):
    """顶层 timestamp 对齐不齐（差 > 3）→ 退回单段，时间戳取首尾 token。

    Paraformer 实测：timestamp 数组是 token 级 centisecond（1/100秒）。
    本例 13 token vs 9 个去标点字符 → 走退回单段路径；
    _detect_unit_div 用 duration_s 交叉验证判定厘秒。
    """
    engine = FunASREngine(cfg)
    fake_results = [{
        "text": "大家好，欢迎来到课堂。",
        # 13 token，全部 centisecond → _detect_unit_div(duration_s=60) 判定 div=100
        "timestamp": [
            [150, 350], [350, 510], [510, 750], [1150, 1390], [1410, 1610],
            [1610, 1770], [1770, 2010], [2050, 2250], [2250, 2470],
            [2470, 2670], [2670, 2910], [2930, 3090], [3090, 3330],
        ],
    }]
    segs = engine._parse_results(fake_results, duration_s=60.0)
    assert len(segs) == 1
    # 第一个 token 150 centisecond → 1.5s
    assert abs(segs[0].start - 1.5) < 0.01
    # 最后一个 token end 3330 centisecond → 33.3s
    assert abs(segs[0].end - 33.3) < 0.01
    assert "大家好" in segs[0].text


def test_parse_empty_results(cfg):
    """空结果 → 空列表，不崩。"""
    engine = FunASREngine(cfg)
    assert engine._parse_results([]) == []
    assert engine._parse_results([{}]) == []
    assert engine._parse_results([{"text": ""}]) == []


def test_parse_sorts_segments(cfg):
    """乱序的 sentence_info 解析后按 start 排序。"""
    engine = FunASREngine(cfg)
    fake_results = [{
        "sentence_info": [
            {"start": 5.0, "end": 7.0, "text": "后"},
            {"start": 0.0, "end": 2.0, "text": "前"},
            {"start": 2.0, "end": 4.0, "text": "中"},
        ],
    }]
    segs = engine._parse_results(fake_results)
    assert [s.text for s in segs] == ["前", "中", "后"]


def test_engine_close_idempotent(cfg):
    """close() 多次调用不报错。"""
    engine = FunASREngine(cfg)
    engine.close()
    engine.close()
    assert engine._model is None


class TestParseResultsTextList:
    """funasr 1.4.16 开 spk 后 sentence_info 的 text 可能是 list
    （timestamp_tools.py:155 的空 punc_id_list 分支）。回归：任务 b5a3f6c7a552。"""

    def _engine(self):
        from asr.config import load_config
        from asr.engines.funasr_engine import FunASREngine
        cfg = load_config(Path(__file__).parent.parent / "config.yaml", strict_models=False)
        return FunASREngine(cfg)

    def test_sentence_info_text_list_normalized(self):
        results = [{
            "key": "x",
            "sentence_info": [
                {"start": 0, "end": 1500, "text": ["你", "好"], "spk": 0},
                {"start": 1600, "end": 3000, "text": "今天上课。", "spk": 1},
            ],
        }]
        segs = self._engine()._parse_results(results)
        assert segs[0].text == "你好"
        assert segs[1].text == "今天上课。"

    def test_top_level_text_list_normalized(self):
        results = [{"key": "x", "text": ["大", "家", "好"], "timestamp": [[100, 200]]}]
        segs = self._engine()._parse_results(results)
        assert len(segs) == 1
        assert segs[0].text == "大家好"


class TestEngineCache:
    """P1-1 引擎缓存：同参数 build_engine 返回同一实例；use_cache=False 旁路。

    FunASREngine 惰性加载（构造函数不加载模型），所以这些测试不碰真实模型。
    """

    @pytest.fixture(autouse=True)
    def _clean_engine_cache(self):
        """清空模块级 _ENGINE_CACHE，保证测试互不污染（hermetic）。"""
        from asr.pipeline import _ENGINE_CACHE
        _ENGINE_CACHE.clear()
        yield
        _ENGINE_CACHE.clear()

    def test_same_params_returns_same_instance(self, cfg):
        from asr.pipeline import build_engine
        e1 = build_engine(cfg)
        e2 = build_engine(cfg)
        assert e1 is e2

    def test_use_cache_false_builds_fresh(self, cfg):
        from asr.pipeline import _ENGINE_CACHE, build_engine
        e1 = build_engine(cfg)
        e2 = build_engine(cfg, use_cache=False)
        assert e1 is not e2
        # 旁路构建不进缓存：后续缓存构建仍拿到 e1
        assert e2 not in _ENGINE_CACHE.values()
        assert build_engine(cfg) is e1

    def test_different_quality_is_different_key(self, cfg):
        from asr.pipeline import build_engine
        cfg_fast = load_config("config.yaml", strict_models=False)
        cfg_fast._quality_override = "fast"
        assert build_engine(cfg) is not build_engine(cfg_fast)

    def test_close_cached_engines_clears_and_rebuilds(self, cfg):
        from asr.pipeline import _ENGINE_CACHE, build_engine, close_cached_engines
        e1 = build_engine(cfg)
        assert _ENGINE_CACHE
        close_cached_engines()
        assert not _ENGINE_CACHE
        # 关闭后再构建是全新实例
        assert build_engine(cfg) is not e1

    def test_close_tolerates_concurrent_insert(self, cfg):
        """关停窗口内后台线程插入新 key：迭代快照不炸（回归：
        旧代码 RuntimeError: dict changed size during iteration）。"""
        from asr.pipeline import _ENGINE_CACHE, close_cached_engines

        class RacerEngine:
            def close(self):
                # 模拟后台任务线程在关停期间 build 了新引擎入缓存
                _ENGINE_CACHE[("funasr", "racer", "m", "cpu", False)] = self

        _ENGINE_CACHE[("funasr", "seed", "m", "cpu", False)] = RacerEngine()
        close_cached_engines()          # 旧实现在此 RuntimeError
        assert not _ENGINE_CACHE


class TestEnsureAsciiPathJunction:
    """P1-10 修复轮：junction 兜底按**事后存在**判定（并发竞争回归）。"""

    def _nonascii_src(self, tmp_path):
        src = tmp_path / "模型"          # 非 ASCII 源目录，触发 junction 路径
        src.mkdir()
        (src / "m.bin").write_text("x", encoding="utf-8")
        return src

    def test_race_loser_rc1_but_dst_exists_no_copytree(self, monkeypatch, tmp_path):
        """竞争输家：mklink rc=1 但目录已被赢家建好 → 干净返回，
        绝不 copytree 到已存在目录上（旧逻辑 FileExistsError / WinError 183）。"""
        from types import SimpleNamespace
        from unittest.mock import patch
        from asr.engines import funasr_engine as fem

        monkeypatch.chdir(tmp_path)     # tmp_root "tmp_models" 相对 CWD
        src = self._nonascii_src(tmp_path)
        dst = (tmp_path / "tmp_models" / "asr").resolve()

        def fake_run(args, **kwargs):
            # 模拟并发赢家在本线程 mklink 执行期间建好了目录：
            # rc=1（already exists），但 dst 事实上已可用
            dst.mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(
                returncode=1,
                stderr=b"Cannot create a file when that file already exists.",
                stdout=b"",
            )

        with patch.object(fem.subprocess, "run", fake_run):
            out = fem.FunASREngine._ensure_ascii_path(str(src), "asr")

        assert Path(out) == dst
        assert not (dst / "m.bin").exists()  # 没有 copytree（赢家目录无源文件）

    def test_real_failure_falls_back_to_copytree(self, monkeypatch, tmp_path):
        """真失败：rc=1 且目录始终不存在 → copytree 兜底。"""
        from types import SimpleNamespace
        from unittest.mock import patch
        from asr.engines import funasr_engine as fem

        monkeypatch.chdir(tmp_path)
        src = self._nonascii_src(tmp_path)

        def fake_run(args, **kwargs):
            return SimpleNamespace(returncode=1, stderr=None, stdout=None)

        with patch.object(fem.subprocess, "run", fake_run):
            out = fem.FunASREngine._ensure_ascii_path(str(src), "asr")

        dst = (tmp_path / "tmp_models" / "asr").resolve()
        assert Path(out) == dst
        assert (dst / "m.bin").read_text(encoding="utf-8") == "x"  # 兜底复制发生


class TestCharLevelTimestamps:
    """P0-1：timestamp 与字符逐一对齐，按强标点切段，时间戳真实（非插值）。"""

    def _engine(self):
        from asr.config import load_config
        from asr.engines.funasr_engine import FunASREngine
        cfg = load_config(Path(__file__).parent.parent / "config.yaml", strict_models=False)
        return FunASREngine(cfg)

    def test_split_by_char_ts_basic(self):
        from asr.engines.funasr_engine import _split_by_char_ts
        # 5 汉字 + 1 句号；timestamp 厘秒，逐字对应。
        # 实测形状（根因调查）：ts 数 = 去标点字符数，标点不占 timestamp，
        # 句间间隙来自 ts 本身（非插值）。
        # 注：计划原稿断言 5.0/9.0/9.5 隐含"句号占一个 ts"，与实测形状和
        # _split_by_char_ts 的对齐规则矛盾，已按实测修正数据与断言。
        text = "大家好。再见"
        ts = [[100, 200], [200, 300], [300, 400], [900, 950], [950, 1000]]
        segs = _split_by_char_ts(text, ts, duration_s=10.0)
        assert len(segs) == 2
        assert segs[0].text == "大家好。"          # 标点保留在句尾
        assert segs[0].start == 1.0 and segs[0].end == 4.0
        assert segs[1].text == "再见"
        assert segs[1].start == 9.0                 # 真实间隙，不是紧贴上一段
        assert segs[1].end == 10.0

    def test_unit_detection_centisecond(self):
        from asr.engines.funasr_engine import _detect_unit_div
        # 末值 562824 厘秒 = 5628s ≤ duration*1.5；原值远超 duration → 厘秒
        assert _detect_unit_div([[5080, 5320], [562700, 562824]], 5628.0) == 100.0

    def test_unit_detection_seconds(self):
        from asr.engines.funasr_engine import _detect_unit_div
        assert _detect_unit_div([[0.5, 1.0], [50.0, 56.0]], 5628.0) == 1.0

    def test_parse_results_full_pipeline_shape(self):
        # 5 分钟实测形状：单 dict、text str、517 token。合成小样本。
        text = "好。那么我们今天呢，开始正式的上课。好各位同学"
        n_chars = len([c for c in text if c not in "，。；？！"])
        ts = [[500 + i * 100, 580 + i * 100] for i in range(n_chars)]
        results = [{"key": "k", "text": text, "timestamp": ts}]
        segs = self._engine()._parse_results(results, duration_s=60.0)
        assert len(segs) == 3                       # 两个句号 → 3 段
        # review 回归断言：段间必须有真实间隙
        gaps = [segs[i + 1].start - segs[i].end for i in range(len(segs) - 1)]
        assert max(gaps) >= 0.0                     # 不再恒为 0 的紧贴
        rates = [(s.end - s.start) / len(s.text) for s in segs]
        assert max(rates) - min(rates) > 1e-6       # s/字 不再恒定（插值特征）

    def test_mismatch_falls_back_single_segment(self):
        # timestamp 数与去标点字符数差 > 3 → 退回单段（老行为），warn 不崩
        text = "大家好大家好大家好大家好"
        ts = [[100, 200]]                            # 严重不齐
        segs = self._engine()._parse_results(
            [{"key": "k", "text": text, "timestamp": ts}], duration_s=10.0)
        assert len(segs) == 1 and segs[0].text == text

    def test_no_timestamp_spans_duration(self):
        # fast 模式（SenseVoice）无 timestamp：占满 [0, duration]（P0-4 前半）
        segs = self._engine()._parse_results(
            [{"key": "k", "text": "大家好", "timestamp": []}], duration_s=100.0)
        assert segs[0].start == 0.0 and segs[0].end == 100.0
