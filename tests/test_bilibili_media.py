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

    def fallback(bv, page, directory, *, max_height):
        assert bv == "BV0000000002" and page == 1 and max_height == 720
        path = directory / "source.mp4"
        path.write_bytes(b"fallback-source")
        return path

    monkeypatch.setattr(autoprep, "public_media_download", fallback)
    result = autoprep.media_download("BV0000000002", 1, tmp_path)
    assert result.read_bytes() == b"fallback-source"


def test_yt_dlp_prefers_720_and_retries_480(monkeypatch, tmp_path):
    attempts = []

    def run(cmd, **kwargs):
        assert kwargs.get("timeout") is None  # No overall download runtime limit.
        selected = cmd[cmd.index("-f") + 1]
        attempts.append(selected)
        if len(attempts) < 2:
            return subprocess.CompletedProcess(cmd, 1, stderr="CDN unavailable")
        (tmp_path / "source.mp4").write_bytes(b"480p-media")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(autoprep.subprocess, "run", run)
    monkeypatch.setattr(
        autoprep,
        "public_media_download",
        lambda *args, **kwargs: pytest.fail("Did not expect API fallback"),
    )
    result = autoprep.media_download("BV0000000002", 1, tmp_path)
    assert result.read_bytes() == b"480p-media"
    assert ["height<=720", "height<=480"] == [
        next(part for part in value.split("/") if "height<=" in part)
        .split("[")[1]
        .split("]")[0]
        for value in attempts
    ]


def test_public_api_prefers_720p_dash_stream(monkeypatch, tmp_path):
    calls = []

    def handle(req):
        if req.url.path == "/x/web-interface/view":
            return httpx.Response(
                200, json={"code": 0, "data": {"pages": [{"cid": 33}]}}
            )
        if req.url.path == "/x/player/playurl":
            assert req.url.params["qn"] == "64"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "dash": {
                            "video": [
                                {
                                    "id": 64,
                                    "codecs": "avc1",
                                    "baseUrl": "https://cdn.bilivideo.com/720",
                                },
                                {
                                    "id": 80,
                                    "codecs": "avc1",
                                    "baseUrl": "https://cdn.bilivideo.com/1080",
                                },
                                {
                                    "id": 112,
                                    "codecs": "avc1",
                                    "baseUrl": "https://cdn.bilivideo.com/1080plus",
                                },
                                {
                                    "id": 80,
                                    "codecs": "hvc1",
                                    "baseUrl": "https://cdn.bilivideo.com/hevc",
                                },
                            ],
                            "audio": [
                                {
                                    "codecs": "mp4a",
                                    "baseUrl": "https://cdn.bilivideo.com/aac",
                                }
                            ],
                        }
                    },
                },
            )
        calls.append(req.url.path)
        return httpx.Response(200, content=b"v" * 2300)

    constructor = httpx.Client
    monkeypatch.setattr(
        bilibili_media.httpx,
        "Client",
        lambda **kw: constructor(transport=httpx.MockTransport(handle), **kw),
    )
    monkeypatch.setattr(
        bilibili_media.subprocess,
        "run",
        lambda cmd, **kw: (
            Path(cmd[-1]).write_bytes(b"remux") or subprocess.CompletedProcess(cmd, 0)
        ),
    )
    assert bilibili_media.public_media_download("BV0000000002", 1, tmp_path).is_file()
    assert calls == ["/720", "/aac"]


def test_public_api_falls_back_when_high_quality_is_not_public(monkeypatch, tmp_path):
    requests = []

    def handle(req):
        if req.url.path == "/x/web-interface/view":
            return httpx.Response(
                200, json={"code": 0, "data": {"pages": [{"cid": 33}]}}
            )
        if req.url.path == "/x/player/playurl":
            qn = int(req.url.params["qn"])
            requests.append(qn)
            if qn == 64:
                return httpx.Response(200, json={"code": -403})
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "dash": {
                            "video": [
                                {
                                    "id": 32,
                                    "codecs": "avc1",
                                    "baseUrl": "https://cdn.bilivideo.com/480",
                                }
                            ],
                            "audio": [
                                {
                                    "codecs": "mp4a",
                                    "baseUrl": "https://cdn.bilivideo.com/aac",
                                }
                            ],
                        }
                    },
                },
            )
        return httpx.Response(200, content=b"v" * 2300)

    constructor = httpx.Client
    monkeypatch.setattr(
        bilibili_media.httpx,
        "Client",
        lambda **kw: constructor(transport=httpx.MockTransport(handle), **kw),
    )

    def ffmpeg(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"remuxed")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(bilibili_media.subprocess, "run", ffmpeg)
    assert (
        bilibili_media.public_media_download("BV0000000002", 1, tmp_path).read_bytes()
        == b"remuxed"
    )
    assert requests == [64, 32]


def test_public_api_falls_back_after_720_cdn_failure(monkeypatch, tmp_path):
    calls = []

    def handle(req):
        if req.url.path == "/x/web-interface/view":
            return httpx.Response(
                200, json={"code": 0, "data": {"pages": [{"cid": 33}]}}
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
                                    "id": 64,
                                    "codecs": "avc1",
                                    "baseUrl": "https://cdn.bilivideo.com/high",
                                },
                                {
                                    "id": 32,
                                    "codecs": "avc1",
                                    "baseUrl": "https://cdn.bilivideo.com/medium",
                                },
                            ],
                            "audio": [
                                {
                                    "codecs": "mp4a",
                                    "baseUrl": "https://cdn.bilivideo.com/aac",
                                }
                            ],
                        }
                    },
                },
            )
        calls.append(req.url.path)
        return httpx.Response(
            403 if req.url.path == "/high" else 200, content=b"v" * 2300
        )

    constructor = httpx.Client
    monkeypatch.setattr(
        bilibili_media.httpx,
        "Client",
        lambda **kw: constructor(transport=httpx.MockTransport(handle), **kw),
    )

    def ffmpeg(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"remuxed")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(bilibili_media.subprocess, "run", ffmpeg)
    assert (
        bilibili_media.public_media_download("BV0000000002", 1, tmp_path).read_bytes()
        == b"remuxed"
    )
    assert calls == ["/high", "/medium", "/aac"]


def test_480p_selection_is_a_hard_ceiling_and_no_720p_request(monkeypatch, tmp_path):
    selected = []

    def run(cmd, **kwargs):
        selected.append(cmd[cmd.index("-f") + 1])
        (tmp_path / "source.mp4").write_bytes(b"small-video")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(autoprep.subprocess, "run", run)
    result = autoprep.media_download("BV0000000002", 1, tmp_path, max_height=480)
    assert result.read_bytes() == b"small-video"
    assert len(selected) == 1
    assert "height<=480" in selected[0]
    assert "height<=720" not in selected[0]
    with pytest.raises(ValueError, match="清晰度"):
        autoprep.media_download("BV0000000002", 1, tmp_path, max_height=1080)


def test_public_api_480p_mode_only_requests_32(monkeypatch, tmp_path):
    queries = []

    def handle(req):
        if req.url.path == "/x/web-interface/view":
            return httpx.Response(
                200, json={"code": 0, "data": {"pages": [{"cid": 33}]}}
            )
        if req.url.path == "/x/player/playurl":
            queries.append(req.url.params["qn"])
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "dash": {
                            "video": [
                                {
                                    "id": 32,
                                    "codecs": "avc1",
                                    "baseUrl": "https://cdn.bilivideo.com/480",
                                }
                            ],
                            "audio": [
                                {
                                    "codecs": "mp4a",
                                    "baseUrl": "https://cdn.bilivideo.com/aac",
                                }
                            ],
                        }
                    },
                },
            )
        return httpx.Response(200, content=b"v" * 2300)

    constructor = httpx.Client
    monkeypatch.setattr(
        bilibili_media.httpx,
        "Client",
        lambda **kw: constructor(transport=httpx.MockTransport(handle), **kw),
    )

    def remux(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"remux-480")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(bilibili_media.subprocess, "run", remux)
    assert (
        bilibili_media.public_media_download(
            "BV0000000002", 1, tmp_path, max_height=480
        ).read_bytes()
        == b"remux-480"
    )
    assert queries == ["32"]
