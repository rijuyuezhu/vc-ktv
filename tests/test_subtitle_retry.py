import json
import shutil
import time

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app
from vc_ktv.autoprep import SubtitleError


def test_retry_only_subtitles_preserves_download_and_previous_logs(tmp_path):
    app = create_app(
        root=tmp_path / "room", password="pw", secret=b"retry-test", auto_prepare=True
    )
    app.state.events.create("测试场次", "123456")
    downloads = []
    attempts = []

    async def preview(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "测试歌曲",
            "uploader": "测试",
            "cover": "",
            "duration": 8,
        }

    async def lyrics(query):
        return [
            {
                "source": "VCPedia",
                "title": query,
                "text": "测试歌词第一行\n测试歌词第二行",
                "url": "https://vcpedia.cn",
            }
        ]

    def download(bvid, page, directory):
        downloads.append(bvid)
        path = directory / "source.mp4"
        path.write_bytes(b"preserved-source")
        return path

    def enhance(source, lyrics, target, archive, duration):
        attempts.append(archive)
        assert source.read_bytes() == b"preserved-source"
        if len(attempts) == 1:
            (archive / "tifa.log").write_text("Unknown phonemes")
            raise SubtitleError("alignment_oov", "歌词包含未知发音")
        (archive / "aligned.lrc").write_text("[00:01.00]测试歌词")
        shutil.copyfile(source, target)
        return {"lines": 1}

    app.state.lookup = preview
    app.state.candidate_search = lyrics
    app.state.autoprep.download = download
    app.state.autoprep.enhance = enhance
    app.state.autoprep.probe = lambda path: 8

    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "访客", "123456")
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        song = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        candidate = guest.get(f"/api/songs/{song}/lyrics/candidates").json()[
            "candidates"
        ][0]
        assert (
            guest.post(
                f"/api/songs/{song}/lyrics/select",
                json={
                    "text": candidate["text"],
                    "ticket": candidate["ticket"],
                    "source": candidate["source"],
                },
            ).status_code
            == 200
        )

        for _ in range(120):
            state = admin.get("/api/admin/state").json()
            if state["waiting"] and state["waiting"][0]["prep_state"] == "original":
                break
            time.sleep(0.025)
        assert state["queue"] == []
        assert state["waiting"][0]["prep_error"] == "歌词包含未知发音"
        assert guest.post(f"/api/admin/songs/{song}/subtitles/retry").status_code == 401
        assert admin.post(f"/api/admin/songs/{song}/subtitles/retry").status_code == 200
        assert admin.post(f"/api/admin/songs/{song}/subtitles/retry").status_code == 409
        for _ in range(120):
            state = admin.get("/api/admin/state").json()
            if state["queue"] and state["queue"][0]["prep_state"] == "done":
                break
            time.sleep(0.025)
        assert state["queue"][0]["prep_state"] == "done", state
        assert len(downloads) == 1
        assert len(attempts) == 2
        assert attempts[0] != attempts[1]
        assert (
            json.loads((attempts[0] / "attempt.json").read_text())["error_code"]
            == "alignment_oov"
        )
        assert (
            json.loads((attempts[1] / "attempt.json").read_text())["outcome"]
            == "rendered"
        )
        assert (attempts[0] / "tifa.log").read_text() == "Unknown phonemes"
        original = next(app.state.store.media.glob(f"{song}-original-*.mp4"))
        assert original.read_bytes() == b"preserved-source"
