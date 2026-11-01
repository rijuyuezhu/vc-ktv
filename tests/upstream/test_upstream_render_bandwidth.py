"""One-pass H.264 encoding uses the configured budget while retaining source audio."""

from pathlib import Path
from types import SimpleNamespace

from ktv_app import rendering


def test_single_pass_video_cap_preserves_audio(tmp_path, monkeypatch):
    folder = tmp_path / "bundle"
    folder.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original audio and video")
    received = []

    def fake_run(cmd, **kwargs):
        received.append(cmd)
        Path(cmd[-1]).write_bytes(b"ENCODED")

    monkeypatch.setattr(rendering, "run", fake_run)
    info = {"raw": {"streams": [{"codec_type": "audio", "codec_name": "aac"}]}}
    settings = SimpleNamespace(tool=lambda t: t)
    output = rendering.render(
        source,
        folder,
        info,
        settings,
        rendering.RenderOptions(max_video_kbps=2616),
        "karaoke.mp4",
    )
    assert output.read_bytes() == b"ENCODED"
    assert source.read_bytes() == b"original audio and video"
    assert len(received) == 1
    cmd = received[0]
    assert cmd[cmd.index("-maxrate") + 1] == "2616k"
    assert cmd[cmd.index("-bufsize") + 1] == "2616k"
    assert cmd[cmd.index("-c:a:0") + 1] == "copy"
    assert "subtitles=filename='lyrics.ass'" in cmd[cmd.index("-vf") + 1]
    assert not list(folder.glob("*compact*"))
