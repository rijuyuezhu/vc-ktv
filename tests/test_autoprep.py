import shutil
import time

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app
from vc_ktv.autoprep import title_key


def fake_preview(bvid, page):
    async def result():
        return {
            "bvid": bvid,
            "page": page,
            "title": "【虚拟歌手】示例曲目",
            "uploader": "test",
            "cover": "",
            "duration": 8,
        }

    return result()


def test_title_key():
    assert title_key("【虚拟歌手】示例曲目") == "示例曲目"
    assert title_key("[Cover] 【虚拟歌手】示例曲目") == "示例曲目"
    assert (
        title_key("歌曲介绍里包含《测试歌曲》。【虚拟歌手原创】【bilibilionly】")
        == "测试歌曲"
    )
    # Ambiguous multiple song titles must not be guessed.
    assert title_key("《甲曲》改编自《乙曲》") == "《甲曲》改编自《乙曲》"


def test_auto_bv_only_then_karaoke(tmp_path):
    app = create_app(
        root=tmp_path / "room", password="test", secret=b"test-key", auto_prepare=True
    )
    app.state.events.create("测试场次", "123456")
    app.state.lookup = fake_preview

    def download(bvid, page, directory):
        target = directory / "source.mp4"
        target.write_bytes(b"test-video")
        return target

    async def lyrics(title):
        assert title == "【虚拟歌手】示例曲目"
        return "唱歌\n完成"

    def enhance(source, text, dest, archive, duration, *, preset):
        assert source.is_file() and "唱歌" in text
        assert duration == 8
        archive.mkdir(exist_ok=True)
        (archive / "aligned.lrc").write_text("[00:01.00]唱歌")
        shutil.copyfile(source, dest)
        return {"lines": 1}

    app.state.autoprep.download = download
    app.state.autoprep.enhance = enhance

    async def candidate_search(query):
        assert query == "示例曲目"
        return [
            {
                "source": "VCPedia",
                "title": query,
                "text": "唱歌唱歌唱歌\n完成完成完成",
                "url": "https://vcpedia.cn",
            }
        ]

    app.state.candidate_search = candidate_search
    app.state.probe = lambda path: 8.0
    app.state.autoprep.probe = lambda path: 8.0
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "访客")
        r = guest.post("/api/songs", json={"bvid": "BV0000000001"})
        assert r.status_code == 200, r.text
        song = r.json()["id"]
        options = guest.get(f"/api/songs/{song}/lyrics/candidates").json()["candidates"]
        assert len(options) == 1
        assert (
            guest.post(
                f"/api/songs/{song}/lyrics/select",
                json={
                    "text": options[0]["text"],
                    "ticket": options[0]["ticket"],
                    "source": "VCPedia",
                },
            ).status_code
            == 200
        )
        for _ in range(100):
            data = guest.get("/api/state").json()
            if data["queue"] and data["queue"][0]["prep_state"] == "done":
                break
            time.sleep(0.05)
        assert len(data["queue"]) == 1, data
        assert data["queue"][0]["prep_state"] == "done"
        assert data["queue"][0]["nickname"] == "访客"
        assert data["queue"][0]["has_lyrics"]
        assert (
            admin.post("/api/admin/login", json={"password": "test"}).status_code == 200
        )
        active = admin.post("/api/admin/next").json()["active"]
        assert active["id"] == song
        assert admin.get(active["media_url"]).content == b"test-video"


def test_autoprep_failed_subtitle_keeps_source(tmp_path):
    app = create_app(
        root=tmp_path / "room", password="pw", secret=b"test-key", auto_prepare=True
    )
    app.state.events.create("测试场次", "123456")
    app.state.store.save_settings(
        "fair", True
    )  # This test opts into original playback.
    app.state.lookup = fake_preview
    app.state.autoprep.download = lambda bv, page, dir: dir / "source.mp4"

    async def lyrics(title):
        return "歌词"

    def fail_enhance(*args, **kwargs):
        raise RuntimeError("model failed")

    app.state.autoprep.enhance = fail_enhance

    async def search(query):
        return [
            {
                "source": "VCPedia",
                "title": query,
                "text": "测试原唱歌词\n再来一段歌词",
                "url": "https://vcpedia.cn",
            }
        ]

    app.state.candidate_search = search

    # Return an actual file; no mocked URL required.
    def download(bv, page, dir):
        path = dir / "source.mp4"
        path.write_bytes(b"original")
        return path

    app.state.autoprep.download = download
    app.state.autoprep.probe = lambda path: 8
    with TestClient(app) as client:
        join_and_register(client, "客户端")
        song_id = client.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        c = client.get(f"/api/songs/{song_id}/lyrics/candidates").json()["candidates"][
            0
        ]
        client.post(
            f"/api/songs/{song_id}/lyrics/select",
            json={"text": c["text"], "ticket": c["ticket"], "source": c["source"]},
        )
        for _ in range(100):
            data = client.get("/api/state").json()
            if data["queue"] and data["queue"][0]["prep_state"] == "original":
                break
            time.sleep(0.05)
        assert len(data["queue"]) == 1
        assert data["queue"][0]["prep_state"] == "original"
        assert data["queue"][0]["has_media"]
        assert data["queue"][0]["prep_error"]


def test_auto_download_failure_does_not_block_other_songs(tmp_path):
    app = create_app(
        root=tmp_path / "room", password="pw", secret=b"test-key", auto_prepare=True
    )
    app.state.events.create("测试场次", "123456")
    app.state.store.save_settings(
        "fair", True
    )  # This test opts into original playback.
    app.state.lookup = fake_preview

    def download(bvid, page, dir):
        if page == 1:
            raise RuntimeError("network denied")
        f = dir / "source.mp4"
        f.write_bytes(b"ok")
        return f

    app.state.autoprep.download = download
    app.state.autoprep.probe = lambda path: 8.0
    with TestClient(app) as client:
        join_and_register(client, "客户端")
        client.post("/api/songs", json={"bvid": "BV0000000001", "page": 1})
        client.post("/api/songs", json={"bvid": "BV0000000001", "page": 2})
        for _ in range(100):
            state = client.get("/api/state").json()
            if state["queue"] and state["waiting"][0]["prep_state"] == "unavailable":
                break
            time.sleep(0.05)
        assert len(state["queue"]) == 1
        assert state["queue"][0]["prep_state"] in ("downloaded", "original", "aligning")
        assert state["waiting"][0]["prep_state"] == "unavailable"


def test_failed_download_retries_after_backoff(tmp_path):
    app = create_app(
        root=tmp_path / "room", password="pw", secret=b"test-key", auto_prepare=True
    )
    app.state.events.create("测试场次", "123456")
    app.state.store.save_settings(
        "fair", True
    )  # This test opts into original playback.
    app.state.lookup = fake_preview
    failures = []

    def download(bv, page, directory):
        failures.append(1)
        if len(failures) == 1:
            raise RuntimeError("temporary public API problem")
        target = directory / "source.mp4"
        target.write_bytes(b"working-video")
        return target

    app.state.autoprep.download = download
    app.state.autoprep.probe = lambda path: 8
    with TestClient(app) as client:
        join_and_register(client, "客户端")
        song = client.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        for _ in range(80):
            state = client.get("/api/state").json()
            if state["waiting"] and state["waiting"][0]["prep_state"] == "unavailable":
                break
            time.sleep(0.05)
        row = state["waiting"][0]
        assert row["prep_attempts"] == 1 and row["prep_retry_at"] > time.time()
        assert "自动重试" in row["prep_error"]
        with app.state.store.lock:
            app.state.store.db.execute(
                "UPDATE songs SET prep_retry_at=0 WHERE id=?", (song,)
            )
            app.state.store.db.commit()
        for _ in range(80):
            state = client.get("/api/state").json()
            if state["queue"] and state["queue"][0]["prep_state"] == "downloaded":
                break
            time.sleep(0.05)
        assert state["queue"] and state["queue"][0]["prep_attempts"] == 2
        assert len(failures) == 2


def test_default_auto_renderer_is_only_upstream_pipeline(tmp_path, monkeypatch):
    """A normal AutoPrep enhancement never calls our old direct TextGrid path."""
    import json
    from types import SimpleNamespace

    from vc_ktv import autoprep

    source = tmp_path / "original.mp4"
    source.write_bytes(b"original-soundtrack")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    produced = []

    def fake_pipeline(video, lyrics, work, settings, *, style):
        assert style.size == 48 and style.mode == "double"
        assert video == source
        assert lyrics == "DUDUDU\n你好"
        assert settings.kim_model.name == "MelBandRoformer.ckpt"
        assert work == attempt / "pipeline" and not work.exists()
        produced.append(work)
        subtitles = work / "subtitles"
        subtitles.mkdir(parents=True)
        (subtitles / "lyrics.ass").write_text("Original DUDUDU\n你好")
        (subtitles / "lyrics.lrc").write_text("[00:01.00]DUDUDU你好\n")
        report = subtitles / "export_report.json"
        report.write_text(json.dumps({"rendered_lines": 1}))
        video = subtitles / "karaoke.mp4"
        video.write_bytes(b"mp4-with-original-soundtrack")
        return SimpleNamespace(video=video, ass=subtitles / "lyrics.ass", report=report)

    monkeypatch.setattr(autoprep, "make_karaoke_video", fake_pipeline)
    target = tmp_path / "finished.mp4"
    report = autoprep.render_upstream_video(
        source, "DUDUDU\n你好", target, attempt, 4.73
    )
    assert report["rendered_lines"] == 1
    assert target.read_bytes() == b"mp4-with-original-soundtrack"
    assert source.read_bytes() == b"original-soundtrack"
    assert (attempt / "aligned.lrc").read_text() == "[00:01.00]DUDUDU你好\n"
    assert produced == [attempt / "pipeline"]
