"""Reject videos without audio before accepting them as a song source."""

import subprocess

import pytest

from vc_ktv.processing.media_preparation import probe_media


def test_no_audio_track_rejected_at_upload_validation(tmp_path):
    path = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=24",
            "-t",
            "1",
            "-an",
            "-c:v",
            "libx264",
            path,
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    with pytest.raises(ValueError, match="视频无效"):
        probe_media(path)
