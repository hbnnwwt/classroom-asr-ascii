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

    funasr 1.4.16 实测 timestamp 为毫秒（R12）。本例 13 token vs 9 个去标点
    字符 → 走退回单段路径；_detect_unit_div 用 duration_s 交叉验证判定毫秒。
    """
    engine = FunASREngine(cfg)
    fake_results = [{
        "text": "大家好，欢迎来到课堂。",
        # 13 token，全部毫秒 → _detect_unit_div(duration_s=60) 判定 div=1000
        "timestamp": [
            [150, 350], [350, 510], [510, 750], [1150, 1390], [1410, 1610],
            [1610, 1770], [1770, 2010], [2050, 2250], [2250, 2470],
            [2470, 2670], [2670, 2910], [2930, 3090], [3090, 3330],
        ],
    }]
    segs = engine._parse_results(fake_results, duration_s=60.0)
    assert len(segs) == 1
    # 第一个 token 150 ms → 0.15s
    assert abs(segs[0].start - 0.15) < 0.01
    # 最后一个 token end 3330 ms → 3.33s
    assert abs(segs[0].end - 3.33) < 0.01
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

    def test_different_use_itn_is_different_key(self, cfg):
        """R15：use_itn/hotword_enabled/batch_size_s/num_workers 影响 generate
        行为，必须进缓存键——长驻 web 进程改这些配置不需重启即生效。"""
        from asr.pipeline import build_engine
        cfg2 = load_config("config.yaml", strict_models=False)
        cfg2.asr.use_itn = not cfg.asr.use_itn
        assert build_engine(cfg) is not build_engine(cfg2)

    def test_asr_model_path_mirrors_engine_selection(self, cfg):
        """R15：_asr_model_path 必须镜像 FunASREngine._load_model 的路径选择
        （fast → SenseVoice；有 paraformer_model → Paraformer；否则回退），
        防两侧漂移。"""
        from asr.pipeline import _asr_model_path
        # balanced + paraformer_model 已配置 → paraformer（与旧键元素一致）
        assert _asr_model_path(cfg) == str(cfg.asr.paraformer_model)
        # fast → 无视 paraformer，直接 SenseVoice（旧键会错记 paraformer 路径）
        cfg_fast = load_config("config.yaml", strict_models=False)
        cfg_fast._quality_override = "fast"
        assert _asr_model_path(cfg_fast) == str(cfg_fast.models.funasr.asr)
        # 非 fast 且 paraformer 未配置 → 回退 SenseVoice
        cfg_nop = load_config("config.yaml", strict_models=False)
        cfg_nop.asr.paraformer_model = None
        assert _asr_model_path(cfg_nop) == str(cfg_nop.models.funasr.asr)

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


class TestTranscribeGenerateKwargs:
    """R14：--language 必须真实落到 generate 参数，而不是只改输出元数据。

    不加载真实模型：_loaded=True + 假 model 对象让 _load_model 直接返回；
    get_wav_duration_s 在 transcribe 内部 from ..media import，patch 模块属性即生效。
    """

    def test_generate_receives_cfg_language(self, cfg, tmp_path):
        from unittest.mock import patch

        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF-fake")

        captured: dict = {}

        class FakeModel:
            def generate(self, **kwargs):
                captured.update(kwargs)
                return [{"text": "大家好", "timestamp": [[0, 300], [300, 600], [600, 900]]}]

        cfg.language = "en"
        engine = FunASREngine(cfg)
        engine._model = FakeModel()
        engine._loaded = True                     # 跳过真实模型加载

        with patch("asr.media.get_wav_duration_s", return_value=5.0):
            engine.transcribe(wav)

        assert captured["language"] == "en"       # 旧代码硬编码 "zh"
        assert captured["use_itn"] is cfg.asr.use_itn
        assert captured["batch_size_s"] == cfg.asr.batch_size_s


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
        # 5 汉字 + 1 句号；timestamp 毫秒（R12：funasr 1.4.16 实测单位），
        # 逐字对应。实测形状（根因调查）：ts 数 = 去标点字符数，标点不占
        # timestamp，句间间隙来自 ts 本身（非插值）。
        # 注：计划原稿断言 5.0/9.0/9.5 隐含"句号占一个 ts"，与实测形状和
        # _split_by_char_ts 的对齐规则矛盾，已按实测修正数据与断言。
        text = "大家好。再见"
        ts = [[1000, 2000], [2000, 3000], [3000, 4000], [9000, 9500], [9500, 10000]]
        segs = _split_by_char_ts(text, ts, duration_s=10.0)
        assert len(segs) == 2
        assert segs[0].text == "大家好。"          # 标点保留在句尾
        assert segs[0].start == 1.0 and segs[0].end == 4.0
        assert segs[1].text == "再见"
        assert segs[1].start == 9.0                 # 真实间隙，不是紧贴上一段
        assert segs[1].end == 10.0

    def test_unit_detection_milliseconds(self):
        from asr.engines.funasr_engine import _detect_unit_div
        # R12 真机实测（300.5s 冒烟）：末值 285440 ms = 285.44s ≤ 300.5×1.5
        # 的读法成立（超界按秒读不通）→ 毫秒。funasr 1.4.16 全程毫秒。
        assert _detect_unit_div([[5080, 5320], [285000, 285440]], 300.5) == 1000.0

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

    def test_split_by_char_ts_keeps_soft_punct(self):
        """R13：软标点（，、：（）等）必须保留在输出文本，只是不消费 ts 槽位。

        ts 数按 n_text 口径（排除 _NO_TS_CHARS ∪ _HARD_PUNCT）构造：
        逗号在文本里但**不占** ts 项。断言：软标点守恒 + 段数/时间戳与
        无逗号等价文本完全一致（对齐不受影响）。
        """
        from asr.engines.funasr_engine import _split_by_char_ts, _NO_TS_CHARS, _HARD_PUNCT
        text = "大家好，今天上课。再见"
        n = sum(1 for c in text if c not in _NO_TS_CHARS and c not in _HARD_PUNCT)
        assert n == 9                              # 大家好今天上课再见
        ts = [[1000 + i * 500, 1200 + i * 500] for i in range(n)]
        segs = _split_by_char_ts(text, ts, duration_s=5.0)
        # 与"无逗号等价文本"逐段对比：段数与时间戳完全一致
        segs_plain = _split_by_char_ts("大家好今天上课。再见", ts, duration_s=5.0)
        assert len(segs) == len(segs_plain) == 2
        assert [(s.start, s.end) for s in segs] == [(s.start, s.end) for s in segs_plain]
        # 软标点守恒：逗号保留在第一段文本中（旧代码静默丢弃）
        assert segs[0].text == "大家好，今天上课。"
        assert segs[1].text == "再见"

    def test_parse_results_soft_punct_conserved(self):
        """R13 全链路守恒：输入文本 N 个软标点 → 输出各段软标点总数 == N。"""
        from asr.engines.funasr_engine import _NO_TS_CHARS, _HARD_PUNCT
        text = "大家好，今天：上课（开始）。再见、同学们"
        n = sum(1 for c in text if c not in _NO_TS_CHARS and c not in _HARD_PUNCT)
        ts = [[500 + i * 300, 700 + i * 300] for i in range(n)]
        segs = self._engine()._parse_results(
            [{"key": "k", "text": text, "timestamp": ts}], duration_s=3.0)
        soft = set(_NO_TS_CHARS) - set("\n\r\t ")   # 只统计可见软标点
        n_in = sum(1 for c in text if c in soft)
        n_out = sum(1 for s in segs for c in s.text if c in soft)
        assert n_in == 5                            # ，：（）、
        assert n_out == n_in

    def test_leading_soft_punct_then_hard_punct_no_crash(self):
        """终审复审回归：句首孤立软标点（R13 后会进 buf）遇强标点时
        buf 非空但 t0 is None——旧 flush 直接 t0/div → TypeError（web 500）。
        期望：不崩、字符守恒、锚定首个 ts。"""
        from asr.engines.funasr_engine import _split_by_char_ts
        ts = [[1000, 1200], [2000, 2200], [3000, 3200]]   # 毫秒，div=1000
        segs = _split_by_char_ts("，。大家好", ts, duration_s=5.0)
        assert len(segs) == 1
        assert segs[0].text == "，。大家好"         # 字符守恒
        assert segs[0].start == 1.0                 # 锚定 ts[0][0] = 1000ms

    def test_unanchored_trailing_punct_merges_into_last(self):
        """终审复审：句界 flush 后的孤立标点（。，。）无时间锚——走既有
        收尾并入语义，末段文本以 "。，。" 结尾，不崩不丢。"""
        from asr.engines.funasr_engine import _split_by_char_ts
        ts = [[1000, 1200], [2000, 2200], [3000, 3200]]
        segs = _split_by_char_ts("大家好。，。", ts, duration_s=5.0)
        assert len(segs) == 1
        assert segs[0].text == "大家好。，。"        # 并入末段，无丢字

    def test_pure_punct_text_returns_empty(self):
        """终审复审：纯标点文本（无任何可锚字符）→ 空列表，不崩、
        不产空段/纯标点段（既有收尾语义对 empty-out 的自然结果）。"""
        from asr.engines.funasr_engine import _split_by_char_ts
        ts = [[1000, 1200], [2000, 2200], [3000, 3200]]
        segs = _split_by_char_ts("，，。", ts, duration_s=5.0)
        assert segs == []
        assert all(s.text.strip() for s in segs)    # 不产空文本段
