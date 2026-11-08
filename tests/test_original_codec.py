"""Original video codec policy: default HEVC cleanup and optional full AVC compatibility."""

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vc_ktv.application.factory import create_app
from vc_ktv.core.store import Store
from vc_ktv.processing.media_preparation import normalize_hevc_source
from vc_ktv.processing.worker import AutoPrep


def codec(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)["streams"][0]["codec_name"]


def test_real_hevc_is_normalized_before_original_publication(tmp_path):
    original = tmp_path / "source.mp4"
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
            "color=c=blue:s=128x72:r=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "0.7",
            "-c:v",
            "libx265",
            "-x265-params",
            "pools=1:frame-threads=1:log-level=error",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(original),
        ],
        check=True,
        capture_output=True,
    )
    assert codec(original) == "hevc"
    assert normalize_hevc_source(original) == original
    assert codec(original) == "h264"
    assert not list(tmp_path.glob("*-h264.mp4"))
    # Already compatible originals must not be modified by the optional policy.
    content = original.read_bytes()
    assert normalize_hevc_source(original, force_h264=True) == original
    assert original.read_bytes() == content


@pytest.mark.parametrize(
    ("codec_name", "force", "converted"),
    [
        ("av1", False, False),
        ("av1", True, True),
        ("hevc", False, True),
        ("h264", True, False),
    ],
)
def test_optional_policy_selects_correct_codecs(
    tmp_path, monkeypatch, codec_name, force, converted
):
    import vc_ktv.processing.media_preparation as module

    original = tmp_path / "source.mp4"
    original.write_bytes(b"original video bytes")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout=codec_name + "\n")
        Path(command[-1]).write_bytes(b"converted video" * 100)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    assert normalize_hevc_source(original, force_h264=force) == original
    assert len(commands) == (2 if converted else 1)
    assert original.read_bytes().startswith(b"converted" if converted else b"original")


def test_failed_conversion_does_not_replace_original(tmp_path, monkeypatch):
    import vc_ktv.processing.media_preparation as module

    original = tmp_path / "source.mp4"
    original.write_bytes(b"original")

    def run(command, **kwargs):
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout="hevc")
        Path(command[-1]).write_bytes(b"incomplete")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        normalize_hevc_source(original)
    assert original.read_bytes() == b"original"
    assert not list(tmp_path.glob("*-h264.mp4"))


def test_source_job_snapshots_policy_and_publishes_converted_original(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "room")
    store.save_settings(None, None, force_h264_originals=True)
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('a','A')")
        song_id = store.db.execute(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at) "
            "VALUES('BV0000000001',1,'Song','UP','',2,'a',1)"
        ).lastrowid
        store.db.commit()

    def download(bvid, page, directory):
        path = directory / "source.mp4"
        path.write_bytes(b"hevc sample")
        return path

    seen = []

    def normalize(path, *, force_h264):
        seen.append(force_h264)
        path.write_bytes(b"converted H264")
        return path

    monkeypatch.setattr("vc_ktv.processing.worker.normalize_hevc_source", normalize)
    worker = AutoPrep(store, lambda path: 2, download=download)
    claimed = worker.claim()
    assert claimed["force_h264_originals"] is True
    store.save_settings(None, None, force_h264_originals=False)
    worker.prepare_source(claimed)
    row = store.rows(
        "SELECT source_file,media_file,status FROM songs WHERE id=?", (song_id,)
    )[0]
    assert row["source_file"] == row["media_file"]
    assert row["status"] == "ready"
    assert (store.media / row["source_file"]).read_bytes() == b"converted H264"
    assert seen == [True]


def test_admin_upload_exposes_only_normalized_original(tmp_path):
    original = tmp_path / "upload.mp4"
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
            "color=c=blue:s=128x72:r=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "0.7",
            "-c:v",
            "libx265",
            "-x265-params",
            "pools=1:frame-threads=1:log-level=error",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(original),
        ],
        check=True,
        capture_output=True,
    )
    app = create_app(
        root=tmp_path / "ktv",
        password="test-admin",
        secret=b"original-codec-upload",
        auto_prepare=False,
    )
    app.state.events.create("upload")
    store = app.state.store
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('a','A')")
        song_id = store.db.execute(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at) "
            "VALUES('BV0000000001',1,'Song','UP','',2,'a',1)"
        ).lastrowid
        store.db.commit()
    with TestClient(app) as client:
        assert (
            client.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        response = client.post(
            f"/api/admin/songs/{song_id}/media",
            files={"file": ("upload.mp4", original.read_bytes(), "video/mp4")},
        )
        assert response.status_code == 200, response.text
        saved = store.rows(
            "SELECT source_file,media_file FROM songs WHERE id=?", (song_id,)
        )[0]
        assert saved["source_file"] == saved["media_file"]
        assert codec(store.media / saved["source_file"]) == "h264"
        downloaded = client.get(f"/api/songs/{song_id}/download/source")
        assert downloaded.status_code == 200
        assert downloaded.content == (store.media / saved["source_file"]).read_bytes()


def test_admin_option_defaults_off_persists_and_resets_between_events(tmp_path):
    app = create_app(
        root=tmp_path / "ktv",
        password="test-admin",
        secret=b"original-codec-settings",
        auto_prepare=False,
    )
    app.state.events.create("first")
    with TestClient(app) as client:
        assert (
            client.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        assert client.get("/api/admin/settings").json()["force_h264_originals"] is False
        assert (
            client.put(
                "/api/admin/settings", json={"force_h264_originals": True}
            ).json()["force_h264_originals"]
            is True
        )
        assert (
            client.put("/api/admin/settings", json={"video_quality": 480}).json()[
                "force_h264_originals"
            ]
            is True
        )
        assert client.post("/api/admin/events/close").status_code == 200
        assert (
            client.post("/api/admin/events", json={"name": "second"}).status_code == 200
        )
        assert client.get("/api/admin/settings").json()["force_h264_originals"] is False
