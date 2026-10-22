from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import copy
import threading
import time
import wave

import pytest


@pytest.fixture(autouse=True)
def clean_cache():
    from asr.pipeline import close_cached_engines
    close_cached_engines()
    yield
    close_cached_engines()


@pytest.mark.parametrize("field", ["vad", "punc", "spk"])
def test_each_loaded_model_path_affects_cache(field, template_config, tmp_path):
    from asr.pipeline import build_engine
    cfg = template_config
    cfg.diarize.enabled = True
    other = cfg.model_copy(deep=True)
    setattr(other.models.funasr, field, tmp_path / (field + "-other"))
    assert build_engine(cfg) is not build_engine(other)


def test_options_do_not_require_reloading_models(template_config):
    from asr.pipeline import build_engine
    cfg = template_config
    other = cfg.model_copy(deep=True)
    other.language = "en"
    other.asr.use_itn = not cfg.asr.use_itn
    other.asr.batch_size_s = cfg.asr.batch_size_s + 1
    assert build_engine(cfg) is build_engine(other)


def test_concurrent_build_constructs_one_engine(monkeypatch, template_config):
    import asr.pipeline as pipeline
    original = pipeline.FunASREngine
    builds = []
    def construct(*args, **kwargs):
        builds.append(1)
        time.sleep(.03)
        return original(*args, **kwargs)
    monkeypatch.setattr(pipeline, "FunASREngine", construct)
    with ThreadPoolExecutor(max_workers=8) as pool:
        instances = list(pool.map(lambda _: pipeline.build_engine(template_config), range(8)))
    assert len(builds) == 1
    assert all(instance is instances[0] for instance in instances)


def test_transcribe_serializes_calls_and_close_waits(monkeypatch, template_config, tmp_path):
    from asr.engines.base import Segment, TranscriptionOptions
    from asr.engines.funasr_engine import FunASREngine
    import asr.media
    engine = FunASREngine(template_config)
    source = tmp_path / "a.wav"
    source.touch()
    monkeypatch.setattr(asr.media, "get_wav_duration_s", lambda p: 1)
    active = 0
    maximum = 0
    entered = threading.Event()
    release = threading.Event()
    observed = []
    def fake_transcribe(wav, duration, hotwords, on_progress, on_segments=None, *, options=None):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        entered.set()
        assert release.wait(3)
        observed.append(options.language)
        active -= 1
        return [Segment(0, 1, options.language)]
    monkeypatch.setattr(engine, "_transcribe_vad_sliced", fake_transcribe)
    engine._model = object()
    engine._loaded = True
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(engine.transcribe, source, options=TranscriptionOptions(language="zh"))
        assert entered.wait(2)
        close = pool.submit(engine.close)
        time.sleep(.03)
        assert not close.done()
        assert engine._model is not None
        release.set()
        assert first.result()[0].text == "zh"
        close.result()
        second = pool.submit(engine.transcribe, source, options=TranscriptionOptions(language="en"))
        assert second.result()[0].text == "en"
    assert maximum == 1
    assert observed == ["zh", "en"]


def test_pipeline_does_not_mutate_caller_config(monkeypatch, tmp_path, template_config):
    import asr.pipeline as pipeline
    cfg = template_config
    snapshot = copy.deepcopy(cfg.__dict__)
    def missing_probe(path):
        raise RuntimeError("stop before extraction")
    source = tmp_path / "a.wav"
    source.touch()
    monkeypatch.setattr(pipeline, "probe", missing_probe)
    with pytest.raises(RuntimeError, match="stop before extraction"):
        pipeline.run(source, cfg, overrides={"language": "en", "device": "cpu"})
    assert cfg.__dict__ == snapshot


def test_model_alias_depends_on_source_path(monkeypatch, tmp_path):
    from asr.engines.funasr_engine import FunASREngine
    monkeypatch.chdir(tmp_path)
    first, second = tmp_path / "模型甲", tmp_path / "模型乙"
    first.mkdir()
    second.mkdir()
    (first / "marker").write_text("first")
    (second / "marker").write_text("second")
    a = FunASREngine._ensure_ascii_path(str(first), "asr")
    b = FunASREngine._ensure_ascii_path(str(second), "asr")
    assert a != b
    assert (Path(a) / "marker").read_text() == "first"
    assert (Path(b) / "marker").read_text() == "second"


def test_shared_engine_forwards_each_calls_options(monkeypatch, template_config, tmp_path):
    from asr.engines.base import TranscriptionOptions
    from asr.pipeline import build_engine
    source = tmp_path / "input.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 16000)
    cfg = template_config
    engine = build_engine(cfg)
    cfg.language = "en"
    cfg.asr.use_itn = False
    cfg.asr.batch_size_s = 42
    assert build_engine(cfg) is engine
    captured = []
    class Model:
        def generate(self, **kwargs):
            captured.append(kwargs)
            return [{"text": "测试", "timestamp": [[0, 300], [300, 600]]}]
    class Vad:
        def generate(self, **kwargs):
            return [{"value": [[0, 900]]}]
    engine._model, engine._loaded = Model(), True
    monkeypatch.setattr(engine, "_load_vad_model", lambda: Vad())
    monkeypatch.setattr(engine, "_punctuate", lambda text: text)
    engine.transcribe(source, options=TranscriptionOptions(language="zh", use_itn=True, batch_size_s=30))
    engine.transcribe(source, options=TranscriptionOptions.from_config(cfg))
    assert [(c["language"], c["use_itn"], c["batch_size_s"]) for c in captured] == [
        ("zh", True, 30), ("en", False, 42)]


def test_two_transcriptions_never_overlap(monkeypatch, template_config, tmp_path):
    from asr.engines.funasr_engine import FunASREngine
    import asr.media
    source = tmp_path / "input.wav"
    source.touch()
    monkeypatch.setattr(asr.media, "get_wav_duration_s", lambda p: 1)
    engine = FunASREngine(template_config)
    active, maximum = 0, 0
    def fake_transcribe(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        time.sleep(.03)
        active -= 1
        return []
    monkeypatch.setattr(engine, "_transcribe_vad_sliced", fake_transcribe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: engine.transcribe(source), range(2)))
    assert maximum == 1
