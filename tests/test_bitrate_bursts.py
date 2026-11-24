"""Source selection must not hide sustained bitrate bursts behind a low mean."""

import json
from pathlib import Path
from types import SimpleNamespace

from tests.test_tv_html_source import room, setup_song
from vc_ktv.processing import bitrate, media_preparation


def test_packet_windows_include_audio_and_video_and_ignore_packet_order():
    # The first two packets are in decoding rather than presentation order.
    profile = bitrate.packet_peaks(
        "2.3,300000\n0.3,500000\n0.1,200000\n1.9,400000\n3,400000\n4,400000\n20,50"
    )
    assert profile == {"peak_1s_mbps": 5.6, "peak_5s_mbps": 3.52}
    assert bitrate.packet_peaks("N/A,123\ninvalid\nNaN,1") is None


def test_bursty_original_selects_cache_or_baked_without_probing_request(
    tmp_path, monkeypatch
):
    app = room(tmp_path)
    song_id, source, _, archive = setup_song(app, source_size=6_000_000, duration=30)
    song = dict(
        app.state.store.db.execute(
            "SELECT * FROM songs WHERE id=?", (song_id,)
        ).fetchone()
    )
    # 1.6 Mbps average, but sustained bursts beyond the 3 Mbps target.
    monkeypatch.setattr(
        bitrate, "measure", lambda _p: {"peak_1s_mbps": 6.74, "peak_5s_mbps": 4.07}
    )
    bitrate.record_source_profile(source, archive)
    monkeypatch.setattr(
        bitrate,
        "measure",
        lambda _p: (_ for _ in ()).throw(AssertionError("must not probe on request")),
    )
    assert app.state.media.overlay_source(song) is None
    (archive / "tv-source.mp4").write_bytes(b"cache" * 512)
    assert app.state.media.overlay_source(song) == archive / "tv-source.mp4"
    # Relaxing bandwidth should allow reuse of original without rewriting files.
    app.state.store.save_settings(None, None, max_bandwidth_mbps=5.0)
    assert app.state.media.overlay_source(song) == source


def test_worker_generates_cache_for_low_average_high_peak(tmp_path, monkeypatch):
    source = tmp_path / "original.mp4"
    source.write_bytes(b"x" * 3_000_000)
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(
        bitrate, "measure", lambda _p: {"peak_1s_mbps": 5.1, "peak_5s_mbps": 3.7}
    )
    bitrate.record_source_profile(source, archive)
    calls = []

    def fake_encode(cmd, **_kwargs):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"cache" * 512)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(media_preparation.subprocess, "run", fake_encode)
    media_preparation.prepare_tv_source(source, archive, 20, 3)
    assert len(calls) == 1 and "-maxrate" in calls[0]
    assert (archive / "tv-source.mp4").is_file()
    # A profile cannot be inherited after replacement of the source bytes.
    source.write_bytes(b"new")
    assert bitrate.read_source_profile(source, archive) is None


def test_profile_malformed_is_unknown(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")
    stat = source.stat()
    (tmp_path / "source-bitrate.json").write_text(
        json.dumps(
            {
                "source_file": source.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "peak_1s_mbps": "broken",
            }
        )
    )
    assert bitrate.read_source_profile(source, tmp_path) is None


def test_legacy_low_average_source_can_be_audited_by_explicit_retry(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from tests.test_tv_cache_retry import login, wait_for_retry
    from vc_ktv.processing import worker

    app = room(tmp_path)
    song_id, source, baked, archive = setup_song(
        app, source_size=6_000_000, duration=30
    )
    app.state.autoprep.probe = lambda _: 30.0
    monkeypatch.setattr(
        bitrate, "measure", lambda _: {"peak_1s_mbps": 6.74, "peak_5s_mbps": 4.07}
    )

    def cache(path, staging, duration, budget, **kwargs):
        assert not bitrate.fits_budget(
            path, duration, budget, bitrate.read_source_profile(path, staging)
        )
        (staging / "tv-source.mp4").write_bytes(b"c" * 2048)

    monkeypatch.setattr(worker, "prepare_tv_source", cache)
    with TestClient(app) as admin:
        login(admin)
        assert (
            admin.post(f"/api/admin/songs/{song_id}/tv-cache/retry").json()["status"]
            == "started"
        )
        wait_for_retry(app)
        assert bitrate.read_source_profile(source, archive)["peak_5s_mbps"] == 4.07
        assert baked.read_bytes() == b"BAKED"
        assert (
            admin.post(f"/api/admin/songs/{song_id}/tv-cache/retry").json()["status"]
            == "already_ready"
        )
