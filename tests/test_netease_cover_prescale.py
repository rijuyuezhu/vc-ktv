"""Actual FFmpeg media contracts after pre-scaling NetEase cover art once."""

import concurrent.futures
import json
import subprocess

import pytest

from vc_ktv.sources.netease_media import assemble_album_video


def ffmpeg(*args):
    return subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *map(str, args),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )


def make_audio(tmp_path):
    audio = tmp_path / "audio.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:duration=3", audio)
    return audio


def make_cover(tmp_path, width, height, extension):
    cover = tmp_path / f"cover-{width}x{height}.{extension}"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate=1",
        "-frames:v",
        "1",
        cover,
    )
    return cover


def inspect(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("width", "height", "extension"),
    [(4000, 4000, "jpg"), (800, 800, "png"), (1900, 1000, "jpg")],
)
def test_real_album_video_prescale_keeps_media_contract_and_cleans_temp(
    tmp_path, width, height, extension
):
    audio = make_audio(tmp_path)
    cover = make_cover(tmp_path, width, height, extension)
    output = assemble_album_video(audio, cover, tmp_path / "out.mp4", 3)
    assert output == tmp_path / "out.mp4"
    assert output.stat().st_size > 1024
    metadata = inspect(output)
    video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
    sound = next(s for s in metadata["streams"] if s["codec_type"] == "audio")
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 1280, 720)
    assert sound["codec_name"] == "aac"
    assert abs(float(metadata["format"]["duration"]) - 3) < 0.1
    assert list(tmp_path.glob("netease-cover-*")) == []
    assert cover.exists(), "original album artwork must be kept unchanged"


def test_real_album_video_without_cover_still_works(tmp_path):
    audio = make_audio(tmp_path)
    result = assemble_album_video(audio, None, tmp_path / "out.mp4", 3)
    video = next(s for s in inspect(result)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1280, 720)
    assert list(tmp_path.glob("netease-cover-*")) == []


def test_bad_cover_does_not_leak_temp_artifact(tmp_path):
    audio = make_audio(tmp_path)
    invalid = tmp_path / "cover.jpg"
    invalid.write_bytes(b"not a valid image")
    with pytest.raises(subprocess.CalledProcessError):
        assemble_album_video(audio, invalid, tmp_path / "out.mp4", 3)
    assert list(tmp_path.glob("netease-cover-*")) == []


def test_parallel_cover_encode_isolated_and_cleaned(tmp_path):
    audio = make_audio(tmp_path)
    cover = make_cover(tmp_path, 1200, 1200, "jpg")

    def run(index):
        return assemble_album_video(audio, cover, tmp_path / f"out-{index}.mp4", 3)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(run, (1, 2)))
    assert len(outputs) == 2
    assert all(p.stat().st_size > 1024 for p in outputs)
    assert list(tmp_path.glob("netease-cover-*")) == []
