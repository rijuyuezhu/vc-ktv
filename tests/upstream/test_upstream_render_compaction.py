"""Render compact downloads from original media, retaining audio and resolution."""

from pathlib import Path
from types import SimpleNamespace

from ktv_app import rendering


def render_setup(
    tmp_path, monkeypatch, first_size, compact_size=None, *, enabled=False
):
    folder = tmp_path / "subtitles"
    folder.mkdir()
    (folder / "lyrics.ass").write_text("[Script Info]\n")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original-data")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        dest = Path(cmd[-1])
        if ".compact." in dest.name:
            if compact_size is None:
                raise RuntimeError("encoder unavailable")
            size = compact_size
        else:
            size = first_size
        with dest.open("wb") as f:
            f.truncate(size)

    monkeypatch.setattr(rendering, "run", fake_run)
    info = {
        "duration": 255.935667,
        "raw": {
            "streams": [
                {"codec_type": "audio", "codec_name": "aac", "bit_rate": "317375"}
            ]
        },
    }
    settings = SimpleNamespace(tool=lambda name: name)
    output = rendering.render(
        source,
        folder,
        info,
        settings,
        rendering.RenderOptions(compact_video=enabled),
        "karaoke.mp4",
    )
    return source, folder, output, calls


def test_compact_large_render_under_30_mb_without_reencoding_audio(
    tmp_path, monkeypatch
):
    source, folder, output, calls = render_setup(
        tmp_path,
        monkeypatch,
        first_size=103_622_915,
        compact_size=27_016_038,
        enabled=True,
    )
    assert output.stat().st_size == 27_016_038
    assert len(calls) == 2
    assert "-crf" in calls[0]
    assert "-crf" not in calls[1]
    assert "-b:v" in calls[1]
    bitrate = int(calls[1][calls[1].index("-b:v") + 1])
    assert 500_000 < bitrate < 550_000
    assert "libx264" in calls[1]
    assert calls[0][calls[0].index("-vf") + 1] == calls[1][calls[1].index("-vf") + 1]
    assert calls[1][calls[1].index("-c:a:0") + 1] == "copy"
    assert source.read_bytes() == b"original-data"
    assert not (folder / "karaoke.compact.mp4").exists()


def test_small_render_skips_compaction(tmp_path, monkeypatch):
    _, folder, output, calls = render_setup(tmp_path, monkeypatch, 10_823_065)
    assert len(calls) == 1
    assert output.stat().st_size == 10_823_065
    assert not (folder / "karaoke.compact.mp4").exists()


def test_compact_failure_preserves_playable_render(tmp_path, monkeypatch):
    _, folder, output, calls = render_setup(
        tmp_path, monkeypatch, 103_622_915, enabled=True
    )
    assert len(calls) == 2
    assert output.stat().st_size == 103_622_915
    assert not (folder / "karaoke.compact.mp4").exists()


def test_default_keeps_large_first_render_without_second_pass(tmp_path, monkeypatch):
    assert rendering.RenderOptions().compact_video is False
    _, folder, output, calls = render_setup(
        tmp_path, monkeypatch, first_size=103_622_915, compact_size=27_016_038
    )
    assert len(calls) == 1
    assert output.stat().st_size == 103_622_915
    assert not (folder / "karaoke.compact.mp4").exists()
