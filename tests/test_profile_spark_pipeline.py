"""Isolated profiler instruments stage boundaries without altering production."""

import json
from types import SimpleNamespace

import pytest

from scripts import profile_spark_pipeline as profiler


def test_refuses_existing_attempt(tmp_path):
    with pytest.raises(FileExistsError):
        profiler.run_profile(
            tmp_path / "absent.mp4",
            "lyrics",
            tmp_path,
            interval=0.01,
        )


def test_mocked_pipeline_records_stage_cost_and_restores_wrapper(monkeypatch, tmp_path):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"mock non-media source")
    stage = tmp_path / "profile"
    original = profiler.upstream.extract

    def fake_extract(path, folder, settings):
        assert path == source
        return {"duration": 1.0}

    monkeypatch.setattr(profiler.upstream, "extract", fake_extract)

    def fake_pipeline(video, lyrics, work, settings, *, progress):
        work.mkdir(parents=True)
        progress("Fake stage")
        info = profiler.upstream.extract(video, work, settings)
        assert info["duration"] == 1
        return SimpleNamespace(as_dict=lambda: {"video": str(video)})

    monkeypatch.setattr(profiler.upstream, "make_karaoke_video", fake_pipeline)
    monkeypatch.setattr(profiler, "gpu_info", lambda: {"gpu_mib": 500, "gpu_util": 0})
    monkeypatch.setattr(
        profiler,
        "process_info",
        lambda: {
            "rss_mib": 12,
            "process_count": 1,
            "process_cpu_pct": 0,
        },
    )
    result = profiler.run_profile(
        source,
        "line1\nline2",
        stage,
        interval=0.01,
        tifa_root=tmp_path,
    )
    assert result["video"] == str(source)
    metrics = json.loads((stage / "metrics.json").read_text())
    assert metrics["status"] == "done"
    assert metrics["lyric_line_count"] == 2
    assert metrics["resources"]
    assert any(x["name"] == "extract" and x["success"] for x in metrics["events"])
    # Runtime import still points at the original script before the test mock.
    assert profiler.upstream.extract is fake_extract
    assert original is not fake_extract


def test_failure_is_recorded_and_restores_wrapped_stage(monkeypatch, tmp_path):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"mock")
    stage = tmp_path / "profile"

    def fail(*args, **kwargs):
        raise ValueError("simulated bad input")

    monkeypatch.setattr(profiler.upstream, "make_karaoke_video", fail)
    monkeypatch.setattr(profiler, "gpu_info", lambda: {})
    monkeypatch.setattr(profiler, "process_info", lambda: {})
    with pytest.raises(ValueError, match="simulated"):
        profiler.run_profile(source, "lyrics", stage, interval=0.01, tifa_root=tmp_path)
    assert json.loads((stage / "metrics.json").read_text())["status"] == "failed"
