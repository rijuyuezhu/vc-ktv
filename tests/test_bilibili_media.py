import subprocess
from pathlib import Path

import httpx
import pytest

from vc_ktv import autoprep, bilibili_media


def test_only_trusted_cdn_hosts():
    allowed = bilibili_media._allowed
    assert allowed("https://cn-bj-01.bilivideo.com/test.m4s?token=1")
    for bad in (
        "https://example.com/download.mp4",
        "http://cn-bj-01.bilivideo.com/stream",
        "https://bilivideo.com.evil.test/stream",
        "https://127.0.0.1/stream",
    ):
        assert not allowed(bad), bad


def test_api_fallback_uses_public_dash_and_video_audio(monkeypatch, tmp_path):
    calls = []

    def handle(req):
        calls.append((req.url.host, req.url.path))
        if req.url.path == "/x/web-interface/view":
            return httpx.Response(
                200, json={"code": 0, "data": {"pages": [{"cid": 1234}]}}
            )
        if req.url.path == "/x/player/playurl":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "dash": {
                            "video": [
                                {
                                    "id": 32,
                                    "codecs": "hvc1.1",
                                    "baseUrl": "https://cdn.bilivideo.com/hevc",
                                },
                                {
                                    "id": 32,
                                    "codecs": "avc1.64001F",
                                    "baseUrl": "https://cdn.bilivideo.com/h264",
                                },
                            ],
                            "audio": [
                                {
                                    "id": 30232,
                                    "codecs": "mp4a.40.2",
                                    "baseUrl": "https://cdn.bilivideo.com/aac",
                                }
                            ],
                        }
                    },
                },
            )
        if req.url.host == "cdn.bilivideo.com":
            return httpx.Response(200, content=b"f" * 2300)
        raise AssertionError(str(req.url))

    client_constructor = httpx.Client
    monkeypatch.setattr(
        bilibili_media.httpx,
        "Client",
        lambda **kw: client_constructor(transport=httpx.MockTransport(handle), **kw),
    )

    def ffmpeg(cmd, **kw):
        assert "-c" in cmd and "copy" in cmd
        Path(cmd[-1]).write_bytes(b"valid-remux")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(bilibili_media.subprocess, "run", ffmpeg)
    output = bilibili_media.public_media_download("BV0000000002", 1, tmp_path)
    assert output.read_bytes() == b"valid-remux"
    assert ("cdn.bilivideo.com", "/h264") in calls
    assert ("cdn.bilivideo.com", "/aac") in calls
    assert ("cdn.bilivideo.com", "/hevc") not in calls


def test_no_public_access_will_not_download(monkeypatch, tmp_path):
    client_constructor = httpx.Client

    def handle(req):
        return httpx.Response(200, json={"code": -403, "message": "not authorized"})

    monkeypatch.setattr(
        bilibili_media.httpx,
        "Client",
        lambda **kw: client_constructor(transport=httpx.MockTransport(handle), **kw),
    )
    with pytest.raises(bilibili_media.PublicMediaUnavailable, match="未授权"):
        bilibili_media.public_media_download("BV0000000002", 1, tmp_path)
    assert not list(tmp_path.iterdir())


def test_yt_dlp_failure_tries_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(
        autoprep.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 1, stderr="Unable to extract initial state"
        ),
    )

    def fallback(bv, page, directory):
        assert bv == "BV0000000002" and page == 1
        path = directory / "source.mp4"
        path.write_bytes(b"fallback-source")
        return path

    monkeypatch.setattr(autoprep, "public_media_download", fallback)
    result = autoprep.media_download("BV0000000002", 1, tmp_path)
    assert result.read_bytes() == b"fallback-source"
