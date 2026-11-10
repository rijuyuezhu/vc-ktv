"""The optional native AV1 TV cache must be safe and fall back to H.264."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import vc_ktv.processing.media_preparation as media


@pytest.mark.parametrize(
    (
        "source_codec",
        "source_height",
        "small_codec",
        "small_height",
        "small_duration",
        "small_bytes",
        "use_native",
    ),
    [
        ("av1", 1080, "av1", 720, 0.2, 2048, True),
        ("av1", 1080, "h264", 720, 0.2, 2048, False),
        ("av1", 1080, "av1", 1080, 0.2, 2048, False),
        ("av1", 1080, "av1", 720, 3.0, 2048, False),
        ("av1", 1080, "av1", 720, 0.2, 70000, False),
        ("h264", 1080, "av1", 720, 0.2, 2048, False),
        ("av1", 720, "av1", 720, 0.2, 2048, False),
    ],
)
def test_native_av1_cache_only_if_compatible_and_small(
    tmp_path,
    monkeypatch,
    source_codec,
    source_height,
    small_codec,
    small_height,
    small_duration,
    small_bytes,
    use_native,
):
    source = tmp_path / "original.mp4"
    source.write_bytes(b"o" * 100000)
    archive = tmp_path / "archive"
    archive.mkdir()
    calls = []

    def download(bvid, page, directory, *, max_height, prefer_av1):
        assert (bvid, page, max_height, prefer_av1) == ("BV0000000001", 1, 720, True)
        candidate = directory / "source.mp4"
        candidate.write_bytes(b"a" * small_bytes)
        calls.append("download")
        return candidate

    def run(command, **kwargs):
        if command[0] == "ffprobe":
            is_source = Path(command[-1]) == source
            data = {
                "streams": [
                    {
                        "codec_name": source_codec if is_source else small_codec,
                        "height": source_height if is_source else small_height,
                    }
                ],
                "format": {"duration": "0.2" if is_source else str(small_duration)},
            }
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(data))
        calls.append("encode")
        Path(command[-1]).write_bytes(b"h" * 2048)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(media, "media_download", download)
    monkeypatch.setattr(media.subprocess, "run", run)
    media.prepare_tv_source(source, archive, 0.2, 3.0, native_av1=("BV0000000001", 1))
    cached = archive / "tv-source.mp4"
    assert cached.is_file()
    assert cached.read_bytes() == (b"a" if use_native else b"h") * 2048
    assert ("encode" in calls) is not use_native
    assert source.read_bytes() == b"o" * 100000
    assert not list(archive.glob("tv-native-*"))


def test_native_av1_download_failure_falls_back_to_h264(tmp_path, monkeypatch):
    source = tmp_path / "original.mp4"
    source.write_bytes(b"o" * 100000)
    archive = tmp_path / "archive"
    archive.mkdir()
    calls = []

    def failed_download(*args, **kwargs):
        raise RuntimeError("video cannot be downloaded")

    def run(command, **kwargs):
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"streams":[{"codec_name":"av1","height":1080}],"format":{"duration":"0.2"}}',
            )
        calls.append("encode")
        Path(command[-1]).write_bytes(b"h" * 2048)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(media, "media_download", failed_download)
    monkeypatch.setattr(media.subprocess, "run", run)
    media.prepare_tv_source(source, archive, 0.2, 3.0, native_av1=("BV0000000001", 1))
    assert calls == ["encode"]
    assert (archive / "tv-source.mp4").read_bytes() == b"h" * 2048


def test_low_bitrate_source_does_not_download_secondary_variant(tmp_path, monkeypatch):
    source = tmp_path / "original.mp4"
    source.write_bytes(b"o" * 2000)
    archive = tmp_path / "archive"
    archive.mkdir()

    def fail(*args, **kwargs):
        pytest.fail("low bitrate must not trigger download or encode")

    monkeypatch.setattr(media, "media_download", fail)
    monkeypatch.setattr(media.subprocess, "run", fail)
    media.prepare_tv_source(source, archive, 1.0, 3.0, native_av1=("BV0000000001", 1))
    assert list(archive.iterdir()) == []


@pytest.mark.parametrize(
    ("changed", "eligible"),
    [
        ({}, True),
        ({"source_file": "5-upload-aabbcc.mp4"}, False),
        ({"source_type": "netease"}, False),
        ({"original_codec_mode": "h264"}, False),
        ({"original_codec_mode": "default"}, False),
        ({"source_file": "4-original-not-the-same-song.mp4"}, False),
    ],
)
def test_native_variant_only_for_downloaded_bilibili_av1(changed, eligible):
    from vc_ktv.processing.worker import _native_av1_variant

    row = {
        "id": 5,
        "bvid": "BV0000000001",
        "page": 2,
        "source_type": "bilibili",
        "source_file": "5-original-abcdef.mp4",
        "original_codec_mode": "av1",
        **changed,
    }
    assert _native_av1_variant(row) == (("BV0000000001", 2) if eligible else None)
