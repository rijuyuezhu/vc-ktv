"""Original source codec choices, including settings and publication."""

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vc_ktv.application.factory import create_app
from vc_ktv.core.store import Store
from vc_ktv.processing.media_preparation import normalize_original_codec
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
    assert normalize_original_codec(original) == original
    assert codec(original) == "h264"
    assert not list(tmp_path.glob("*-h264.mp4"))
    # Already compatible originals must not be modified by the optional policy.
    content = original.read_bytes()
    assert normalize_original_codec(original, mode="h264") == original
    assert original.read_bytes() == content


@pytest.mark.parametrize(
    ("codec_name", "mode", "converted"),
    [
        ("av1", "default", False),
        ("av1", "h264", True),
        ("hevc", "default", True),
        ("h264", "h264", False),
        ("h264", "av1", True),
        ("av1", "av1", False),
        ("hevc", "av1", True),
    ],
)
def test_optional_policy_selects_correct_codecs(
    tmp_path, monkeypatch, codec_name, mode, converted
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
    assert normalize_original_codec(original, mode=mode) == original
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
        normalize_original_codec(original)
    assert original.read_bytes() == b"original"
    assert not list(tmp_path.glob("*-h264.mp4"))


def test_source_job_snapshots_policy_and_publishes_converted_original(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "room")
    store.save_settings(None, None, original_codec_mode="av1")
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

    def normalize(path, *, mode, max_video_kbps):
        seen.append((mode, max_video_kbps))
        path.write_bytes(b"converted AV1")
        return path

    monkeypatch.setattr("vc_ktv.processing.worker.normalize_original_codec", normalize)
    worker = AutoPrep(store, lambda path: 2, download=download)
    claimed = worker.claim()
    assert claimed["original_codec_mode"] == "av1"
    store.save_settings(None, None, original_codec_mode="default")
    worker.prepare_source(claimed)
    row = store.rows(
        "SELECT source_file,media_file,status FROM songs WHERE id=?", (song_id,)
    )[0]
    assert row["source_file"] == row["media_file"]
    assert row["status"] == "ready"
    assert (store.media / row["source_file"]).read_bytes() == b"converted AV1"
    assert seen == [("av1", 2616)]


@pytest.mark.parametrize(
    ("mode", "expected_codec"),
    [("default", "h264"), ("av1", "av1")],
)
def test_admin_upload_exposes_only_normalized_original(tmp_path, mode, expected_codec):
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
    store.save_settings(None, None, original_codec_mode=mode)
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
        assert codec(store.media / saved["source_file"]) == expected_codec
        audio_codec = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(store.media / saved["source_file"]),
            ],
            text=True,
        ).strip()
        assert audio_codec == "aac"
        downloaded = client.get(f"/api/songs/{song_id}/download/source")
        assert downloaded.status_code == 200
        assert downloaded.content == (store.media / saved["source_file"]).read_bytes()


def test_admin_codec_mode_defaults_persists_and_resets_between_events(tmp_path):
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
        assert client.get("/api/admin/settings").json()["original_codec_mode"] == "av1"
        assert (
            client.put(
                "/api/admin/settings", json={"original_codec_mode": "av1"}
            ).json()["original_codec_mode"]
            == "av1"
        )
        assert (
            client.put("/api/admin/settings", json={"video_quality": 480}).json()[
                "original_codec_mode"
            ]
            == "av1"
        )
        assert (
            client.put(
                "/api/admin/settings", json={"original_codec_mode": "h264"}
            ).json()["original_codec_mode"]
            == "h264"
        )
        assert (
            client.put(
                "/api/admin/settings", json={"original_codec_mode": "unknown"}
            ).status_code
            == 422
        )
        assert client.get("/api/admin/settings").json()["original_codec_mode"] == "h264"
        assert client.post("/api/admin/events/close").status_code == 200
        assert (
            client.post("/api/admin/events", json={"name": "second"}).status_code == 200
        )
        assert client.get("/api/admin/settings").json()["original_codec_mode"] == "av1"


def test_legacy_h264_setting_is_respected_without_migration(tmp_path):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute(
            "INSERT INTO room_state(name,value) VALUES('force_h264_originals','1')"
        )
        store.db.commit()
    assert store.settings()["original_codec_mode"] == "h264"
    store.save_settings(None, None, original_codec_mode="av1")
    assert store.settings()["original_codec_mode"] == "av1"


def test_av1_encoder_falls_back_to_cpu_without_publishing_failed_output(
    tmp_path, monkeypatch
):
    import vc_ktv.processing.media_preparation as module

    original = tmp_path / "source.mp4"
    original.write_bytes(b"original video bytes")
    attempts = []

    def run(command, **kwargs):
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout="h264")
        attempts.append(command)
        if "av1_nvenc" in command:
            Path(command[-1]).write_bytes(b"bad")
            raise subprocess.CalledProcessError(1, command)
        Path(command[-1]).write_bytes(b"cpu converted" * 200)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    assert normalize_original_codec(original, mode="av1") == original
    assert len(attempts) == 2
    assert "libsvtav1" in attempts[1]
    assert original.read_bytes().startswith(b"cpu converted")


def test_non_mp4_conversion_preserves_new_mp4(tmp_path, monkeypatch):
    import vc_ktv.processing.media_preparation as module

    original = tmp_path / "source.webm"
    original.write_bytes(b"source-media")

    def run(command, **kwargs):
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout="vp9")
        Path(command[-1]).write_bytes(b"encoded av1" * 200)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    converted = normalize_original_codec(original, mode="av1")
    assert converted == tmp_path / "source-av1.mp4"
    assert converted.is_file()
    assert not original.exists()


def test_av1_failed_encoders_leave_original_unchanged(tmp_path, monkeypatch):
    import vc_ktv.processing.media_preparation as module

    original = tmp_path / "source.mp4"
    original.write_bytes(b"original video bytes")

    def run(command, **kwargs):
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout="h264")
        Path(command[-1]).write_bytes(b"incomplete")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        normalize_original_codec(original, mode="av1")
    assert original.read_bytes() == b"original video bytes"
    assert not list(tmp_path.glob("*-av1.mp4"))


def test_normalization_uses_custom_ffprobe(monkeypatch, tmp_path):
    original = tmp_path / "source.mp4"
    original.write_bytes(b"fake-media")
    commands = []

    def probe(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="h264\n")

    monkeypatch.setenv("KTV_FFPROBE", "/custom/ffprobe")
    monkeypatch.setattr("vc_ktv.processing.media_preparation.subprocess.run", probe)
    assert normalize_original_codec(original) == original
    assert commands[0][0] == "/custom/ffprobe"
