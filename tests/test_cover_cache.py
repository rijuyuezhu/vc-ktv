"""Cover normalization, bounded fetching and nonblocking song submission."""

import asyncio
import subprocess
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from tests.test_events import admin_login
from vc_ktv.application.factory import create_app
from vc_ktv.infra.cover_cache import (
    MAX_DOWNLOAD,
    MAX_JPEG,
    _cover_host,
    _image_size,
    download_cover,
    normalize_existing_cover,
    normalize_image,
)


def create_image(path: Path, width: int, height: int) -> bytes:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={width}x{height}:rate=1",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    return path.read_bytes()


def test_convert_large_art_and_existing_history_to_720(tmp_path):
    original = create_image(tmp_path / "source.png", 2400, 1200)
    thumbnail = normalize_image(original)
    assert thumbnail is not None
    assert len(thumbnail) <= MAX_JPEG
    assert _image_size(thumbnail) == (720, 360)

    media = tmp_path / "media"
    media.mkdir()
    covers = media / "covers"
    covers.mkdir()
    old = covers / "1.png"
    old.write_bytes(original)
    assert normalize_existing_cover(old)
    optimized = covers / "1.jpg"
    assert optimized.is_file() and not old.exists()
    assert optimized.stat().st_size <= MAX_JPEG
    assert _image_size(optimized.read_bytes()) == (720, 360)


def test_invalid_and_oversized_art_are_rejected(tmp_path):
    assert normalize_image(b"invalid") is None
    assert normalize_image(b"\xff\xd8\xff" + b"x" * MAX_DOWNLOAD) is None
    assert not _cover_host("https://evil.example.com/a.jpg")
    assert not _cover_host("http://i0.hdslb.com/a.jpg")
    assert not _cover_host("https://i0.hdslb.com.evil.com/a.jpg")
    assert not _cover_host("https://i0.hdslb.com:8080/a.jpg")

    media = tmp_path / "media"
    media.mkdir()
    covers = media / "covers"

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={
                        "Content-Type": "image/png",
                        "Content-Length": str(MAX_DOWNLOAD + 1),
                    },
                    content=b"not fetched",
                )
            )
        ) as client:
            assert not await download_cover(
                client, "https://i0.hdslb.com/image.png", covers, 1
            )
        assert not covers.exists()

    asyncio.run(check())


def test_live_bilibili_netease_covers_cache_without_blocking_order(tmp_path):
    source = create_image(tmp_path / "source.png", 1600, 1000)
    root = tmp_path / "room"
    app = create_app(
        root=root,
        password="test-admin",
        secret=b"cover-cache-tests",
        auto_prepare=False,
    )
    seen = []

    async def deliver(request):
        seen.append(request.url.host)
        await asyncio.sleep(0.35)
        return httpx.Response(
            200, headers={"Content-Type": "image/png"}, content=source
        )

    app.state.covers.transport = httpx.MockTransport(deliver)

    async def bilibili(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "Bilibili song",
            "uploader": "UP",
            "cover": "https://i0.hdslb.com/bfs/archive/x.png",
            "duration": 120,
        }

    async def resolve(ref):
        return 12345

    async def netease(song_id):
        return {
            "source": "netease",
            "source_id": str(song_id),
            "title": "NetEase song",
            "uploader": "Singer",
            "cover": "https://p1.music.126.net/art.png",
            "duration": 120,
        }

    app.state.lookup = bilibili
    app.state.netease_resolve = resolve
    app.state.netease_lookup = netease

    with TestClient(app) as admin, TestClient(app) as guest:
        admin_login(admin)
        event = admin.post(
            "/api/admin/events", json={"name": "Cover test", "code": "654321"}
        ).json()
        join_and_register(guest, "Guest", "654321")
        directory = root / "sessions" / event["id"] / "media" / "covers"
        start = time.monotonic()
        one = guest.post("/api/songs", json={"bvid": "BV0000000001"})
        elapsed = time.monotonic() - start
        assert one.status_code == 200, one.text
        assert elapsed < 0.32, "cover fetch must not block song request"
        first = one.json()["id"]
        two = guest.post("/api/netease/songs", json={"ref": "12345"})
        assert two.status_code == 200, two.text
        second = two.json()["id"]
        until = time.monotonic() + 8
        while time.monotonic() < until and not all(
            (directory / f"{i}.jpg").is_file() for i in (first, second)
        ):
            time.sleep(0.05)
        assert all((directory / f"{i}.jpg").is_file() for i in (first, second))
        assert seen == ["i0.hdslb.com", "p1.music.126.net"] or set(seen) == {
            "i0.hdslb.com",
            "p1.music.126.net",
        }
        for i in (first, second):
            file = directory / f"{i}.jpg"
            assert file.stat().st_size <= MAX_JPEG
            w, h = _image_size(file.read_bytes())
            assert max(w, h) <= 720

        # Both current-song detail and archived detail use the same local route.
        for song_id in (first, second):
            detail = guest.get(f"/api/songs/{song_id}/detail")
            assert detail.status_code == 200
            local = f"/api/events/{event['id']}/covers/{song_id}"
            assert detail.json()["cover"] == local
            image = guest.get(local)
            assert image.status_code == 200
            assert image.headers["content-type"] == "image/jpeg"
        with TestClient(app) as outsider:
            assert (
                outsider.get(f"/api/events/{event['id']}/covers/{first}").status_code
                == 401
            )

        state = guest.get("/api/state")
        assert state.status_code == 200
        live = [*state.json()["queue"], *state.json()["waiting"]]
        for song_id in (first, second):
            listed = next(song for song in live if song["id"] == song_id)
            assert listed["cover"] == f"/api/events/{event['id']}/covers/{song_id}"

        # A repeated identical source should reuse the same on-disk data.
        three = guest.post("/api/songs", json={"bvid": "BV0000000001"})
        assert three.status_code == 200
        third = three.json()["id"]
        until = time.monotonic() + 6
        while time.monotonic() < until and not (directory / f"{third}.jpg").is_file():
            time.sleep(0.05)
        assert (directory / f"{third}.jpg").is_file()
        assert (directory / f"{third}.jpg").stat().st_ino == (
            directory / f"{first}.jpg"
        ).stat().st_ino

        assert admin.post("/api/admin/events/close").status_code == 200
        for i in (first, second, third):
            detail = admin.get(f"/api/events/{event['id']}/songs/{i}")
            assert detail.status_code == 200
            cover = detail.json()["song"]["cover"]
            assert cover == f"/api/events/{event['id']}/covers/{i}"
            response = admin.get(cover)
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/jpeg"


def test_bad_provider_image_is_only_an_optional_cache(tmp_path):
    root = tmp_path / "room"
    app = create_app(
        root=root,
        password="test-admin",
        secret=b"cover-cache-fallback",
        auto_prepare=False,
    )
    app.state.covers.transport = httpx.MockTransport(lambda _: httpx.Response(503))

    async def bilibili(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "Song",
            "uploader": "UP",
            "cover": "https://i0.hdslb.com/bfs/archive/missing.jpg",
            "duration": 120,
        }

    app.state.lookup = bilibili
    with TestClient(app) as admin, TestClient(app) as guest:
        admin_login(admin)
        event = admin.post(
            "/api/admin/events", json={"name": "Fallback", "code": "654321"}
        ).json()
        join_and_register(guest, "Guest", "654321")
        song = guest.post("/api/songs", json={"bvid": "BV0000000001"})
        assert song.status_code == 200
        assert admin.post("/api/admin/events/close").status_code == 200
        detail = admin.get(
            f"/api/events/{event['id']}/songs/{song.json()['id']}"
        ).json()["song"]
        assert detail["cover"] == "https://i0.hdslb.com/bfs/archive/missing.jpg"
