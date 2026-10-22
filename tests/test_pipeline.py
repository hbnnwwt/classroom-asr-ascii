"""端到端管线只用合成 WAV 和假引擎，不接触模型。"""
import wave
from types import SimpleNamespace

from asr.engines.base import Segment


def test_per_call_options_final_text_and_dotted_replacement_log(monkeypatch, tmp_path, template_config):
    import asr.pipeline as pipeline
    cfg = template_config
    cfg.media.denoise = False
    cfg.output.dir = tmp_path / "out"
    cfg.output.formats = ["srt"]
    glossary = tmp_path / "glossary.txt"
    glossary.write_text("错词 -> 正词\n", encoding="utf-8")
    original = cfg.model_copy(deep=True)
    source = tmp_path / "lesson.1.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 16000)
    observed = []
    def transcribe(path, hotwords=None, *, options=None, **callbacks):
        observed.append(options)
        return [Segment(0, 1, "错词")]
    monkeypatch.setattr(pipeline, "probe", lambda p: SimpleNamespace(duration_s=1))
    monkeypatch.setattr(pipeline, "build_engine", lambda *a, **k: SimpleNamespace(transcribe=transcribe))
    result = pipeline.run(source, cfg, {"language": "en", "glossary": str(glossary)})
    assert observed[0].language == "en"
    assert result.segments[0].text == "正词"
    assert result.written["replace_log"].name == "lesson.1.replace.log"
    assert result.written["srt"].name == "lesson.1.srt"
    assert "json" not in result.written
    assert cfg == original
