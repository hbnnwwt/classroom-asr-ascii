from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from asr.engines.base import Segment
from asr.srt import write_all


def test_dotted_stems_do_not_overwrite_each_other(tmp_path, template_config):
    cfg = template_config.output
    for stem in ("lesson.1", "lesson.2"):
        written = write_all([Segment(0, 1, stem)], tmp_path / stem, cfg)
        assert {p.name for p in written.values()} == {stem + "." + fmt for fmt in cfg.formats}
    assert "lesson.1" in (tmp_path / "lesson.1.txt").read_text(encoding="utf-8")


def test_atomic_publish_preserves_last_version_on_replace_error(tmp_path, monkeypatch):
    from asr import artifacts
    target = tmp_path / "lesson.feedback.md"
    target.write_text("last complete version", encoding="utf-8")
    def fail_replace(source, destination):
        assert Path(source).parent == target.parent
        raise OSError("replace failed")
    monkeypatch.setattr(artifacts.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        artifacts.atomic_write_text(target, "new version")
    assert target.read_text(encoding="utf-8") == "last complete version"
    assert list(tmp_path.iterdir()) == [target]


def test_batch_rejects_stem_collision_before_starting(monkeypatch, tmp_path, template_config):
    import asr.batch as batch
    calls = []
    monkeypatch.setattr(batch, "pipeline_run", lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match="lesson"):
        batch.batch_run([tmp_path / "a/lesson.mp4", tmp_path / "b/lesson.wav"],
                        template_config, overrides={"output_dir": tmp_path / "out"})
    assert not calls


def test_pipeline_final_segments_wav_names_and_cleanup(monkeypatch, tmp_path, template_config):
    import asr.pipeline as pipeline
    cfg = template_config
    cfg.output.formats = ["srt"]
    cfg.postprocess.itn = False
    cfg.postprocess.glossary = None
    cfg.media.keep_wav = True
    cfg.media.denoise = False
    cfg.output.dir = tmp_path / "out"
    scratch = tmp_path / "scratch"
    def make_tmpdir(**kwargs):
        scratch.mkdir()
        return scratch
    monkeypatch.setattr(pipeline, "make_tmpdir", make_tmpdir)
    monkeypatch.setattr(pipeline, "probe", lambda p: SimpleNamespace(duration_s=1))
    engine = SimpleNamespace(transcribe=lambda *a, **k: [Segment(0, 1, "最终文字")])
    monkeypatch.setattr(pipeline, "build_engine", lambda *a, **k: engine)
    for stem in ("lesson.1", "lesson.2"):
        source = tmp_path / (stem + ".wav")
        with wave.open(str(source), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 16000)
        result = pipeline.run(source, cfg)
        assert [s.text for s in result.segments] == ["最终文字"]
        assert not scratch.exists()
        assert (cfg.output.dir / "_wav" / (stem + ".wav")).is_file()
    assert len(list((cfg.output.dir / "_wav").glob("*.wav"))) == 2
