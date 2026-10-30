"""Fast Mode latency is a performance target, never a per-song hard deadline."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from ktv_app.aligners import fast
from ktv_app.timeline import Line, Timeline
from vc_ktv import upstream_pipeline


def test_fast_alignment_continues_after_160_seconds(monkeypatch, tmp_path):
    line = Line(id="1", text="你好世界")
    done = []

    @contextmanager
    def session(_):
        class Worker:
            def align(self, audio, lines, duration, origin, work, **kw):
                done.append((audio, kw))
                return Timeline(duration=duration, origin=origin, lines=lines)

        yield Worker()

    monkeypatch.setattr(
        fast,
        "recognize",
        lambda *args: {
            "chunks": [],
            "asr_elapsed": 200.0,
        },
    )
    monkeypatch.setattr(
        fast,
        "plan_blocks",
        lambda *args: ([(0, 1, 0.0, 20.0)], [10.0]),
    )
    monkeypatch.setattr(fast, "crop", lambda audio, output, *_: Path(output).touch())
    monkeypatch.setattr(
        fast,
        "TIFAAdapter",
        lambda settings: SimpleNamespace(session=session),
    )
    # Simulate a very slow ASR/TIFA path without sleeping in the test.
    ticks = iter([100.0, 400.0])
    monkeypatch.setattr(fast, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    result = fast.align_fast(
        tmp_path / "audio.wav",
        [line],
        20.0,
        0,
        tmp_path / "aligned",
        settings=object(),
    )
    assert result.duration == 20.0
    assert len(done) == 1
    assert done[0][1] == {}  # TIFA's own watchdog, not a shrinking shared budget.


def test_fast_render_uses_independent_timeout_not_remaining_budget(
    monkeypatch, tmp_path
):
    src = tmp_path / "original.mp4"
    src.write_bytes(b"video")
    invoked = []

    monkeypatch.setattr(
        upstream_pipeline,
        "extract",
        lambda *_: {"duration": 250.0, "audio_offset": 0.0},
    )
    monkeypatch.setattr(
        upstream_pipeline,
        "preprocess",
        lambda *_: [Line(id="1", text="你好世界")],
    )
    from ktv_app.aligners import fast as fast_module

    monkeypatch.setattr(
        fast_module,
        "align_fast",
        lambda *args: SimpleNamespace(origin=0.0, diagnostics={}),
    )
    monkeypatch.setattr(upstream_pipeline, "remove_estimates", lambda x: x)
    monkeypatch.setattr(upstream_pipeline, "complete_timeline", lambda x, **kw: x)

    def write_bundle(timeline, style, bundle):
        bundle.mkdir()
        for name in ("lyrics.ass", "lyrics.lrc", "timeline.json", "export_report.json"):
            (bundle / name).write_text("test")

    monkeypatch.setattr(upstream_pipeline, "write_bundle", write_bundle)

    def render(source, bundle, info, settings, options, name):
        invoked.append(name)
        target = bundle / name
        target.write_bytes(b"rendered")
        return target

    monkeypatch.setattr(upstream_pipeline, "render", render)
    result = upstream_pipeline.make_karaoke_video(
        src, "你好世界", tmp_path / "attempt", object(), mode="fast"
    )
    assert invoked == ["karaoke.mp4"]
    assert result.video.read_bytes() == b"rendered"
