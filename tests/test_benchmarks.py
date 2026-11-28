"""Portable benchmark package must remain easy to run and validate settings."""

import json
import math
import subprocess
import sys

import pytest

from benchmarks.party import Config, validate_config


@pytest.mark.parametrize(
    "config",
    [
        Config(users=0),
        Config(steps=-1),
        Config(check_every=0),
        Config(duration_min=120, duration_max=30),
        Config(p_order=-1),
        Config(
            p_cancel=0,
            p_order=0,
            p_join=0,
            p_lyrics=0,
            p_skip_lyrics=0,
            p_edit_lyrics=0,
            p_worker_complete=0,
            p_playback=0,
            p_pin=0,
            p_replay=0,
            p_switch_policy=0,
            p_toggle_orders=0,
        ),
        Config(p_bad_timing=2),
        Config(p_cancel=math.inf),
        Config(p_order=math.nan),
        Config(p_netease=math.nan),
        Config(p_burst=-0.1),
    ],
)
def test_invalid_probability_parameters_are_rejected(config):
    with pytest.raises(ValueError):
        validate_config(config)


def test_valid_benchmark_config():
    validate_config(Config(seed=1, users=2, steps=0, p_netease=0.5))


def test_probabilistic_module_cli_creates_reproducible_summary(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks.party",
            "--seed",
            "23",
            "--users",
            "6",
            "--steps",
            "25",
            "--max-orders",
            "30",
            "--p-toggle-orders",
            "0",
            "--output",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=35,
        check=True,
    )
    assert "SUMMARY" in proc.stdout
    summary = json.loads((tmp_path / "seed-23.json").read_text())
    assert summary["seed"] == 23
    assert summary["config"]["steps"] == 25
    assert summary["fair_reference_checks"] >= 2


def test_browser_benchmark_help_does_not_need_optional_playwright():
    proc = subprocess.run(
        [sys.executable, "-m", "benchmarks.lyric_ui", "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "--offsets" in proc.stdout
    assert "--output" in proc.stdout


def test_preparation_benchmark_small_web_load(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks.preparation",
            "--seed",
            "23",
            "--users",
            "4",
            "--steps",
            "35",
            "--max-orders",
            "15",
            "--web-readers",
            "2",
            "--output",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=35,
        check=True,
    )
    summary = json.loads((tmp_path / "seed-23-preparation.json").read_text())
    assert "SUMMARY" in proc.stdout
    assert summary["web_reads"] == 34
    assert summary["actions"]["preparation_claim_checks"] > 0
    assert summary["actions"]["backing_complete"] > 0
    assert summary["actions"]["projected_reference_checks"] > 0


def test_preparation_refresh_and_backing_gate_without_gpu(tmp_path, monkeypatch):
    from benchmarks.preparation import PreparationConfig, PreparationSimulation

    monkeypatch.setenv("VC_KTV_BACKING_TRACKS", "1")
    with PreparationSimulation(
        PreparationConfig(users=2, steps=0, web_readers=0), tmp_path
    ) as sim:
        assert sim.users_add()
        user = sim.users[0]
        reference = "BV0000000001"
        sim.durations[reference] = 120
        response = user.client.post("/api/songs", json={"bvid": reference})
        assert response.status_code == 200
        sid = response.json()["id"]
        sim.all_songs[sid] = {
            "id": sid,
            "user": user,
            "kind": "bilibili",
            "duration": 120,
        }
        candidate = sim.candidate(sim.all_songs[sid])[0]
        assert (
            user.client.post(
                f"/api/songs/{sid}/lyrics/select",
                json={
                    **{k: candidate[k] for k in ("text", "ticket", "source")},
                    "timing_mode": "align",
                },
            ).status_code
            == 200
        )
        assert sim.worker_complete("general")
        assert sim.row(sid)["prep_state"] == "downloaded"
        assert sim.backing()
        assert sim.worker_complete("general")
        assert sim.row(sid)["prep_state"] == "done"
        assert sim.backing_gate()
        assert sim.store.can_play(
            next(s for s in sim.store.songs() if s["id"] == sid), sim.store.settings()
        )
        before = sim.row(sid)
        codec = (
            "av1" if sim.store.settings()["original_codec_mode"] == "h264" else "h264"
        )
        assert (
            sim.admin.put(
                "/api/admin/settings",
                json={"original_codec_mode": codec, "subtitle_style": "large"},
            ).status_code
            == 200
        )
        assert sim.request_refresh()
        assert sim.row(sid)["refresh_state"] == "queued"
        assert sim.worker_complete("general")
        after = sim.row(sid)
        assert after["source_file"] != before["source_file"]
        assert after["refresh_state"] == ""
        assert sim.store.backing_ready(after)
        assert sim.counters["refresh_backing_reused"] == 1
        sim.invariant()


def test_real_benchmark_plan_does_not_start_server(tmp_path):
    manifest = tmp_path / "songs.json"
    manifest.write_text(
        json.dumps(
            [
                {"source": "bilibili", "ref": "BV0000000001", "timing": "align"},
                {"source": "netease", "ref": "12345", "timing": "provider"},
            ]
        )
    )
    output = tmp_path / "results"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks.real_party",
            "--manifest",
            str(manifest),
            "--pipeline-root",
            str(tmp_path / "missing-models"),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "PLAN ONLY" in proc.stdout
    assert not output.exists()


def test_real_metrics_read_and_distribution():
    import httpx

    from benchmarks.real_metrics import RealMetrics, distribution

    def respond(request):
        return httpx.Response(200, json={"queue": []})

    with httpx.Client(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        elapsed, error = RealMetrics.read(client, "/api/state")
        assert elapsed >= 0
        assert error is None
    assert distribution([1, 2, 3, 4]) == {"p50": 2, "p95": 3, "max": 4}


def test_real_workload_timing_ratio_and_timeout():
    from collections import Counter

    from benchmarks.real_party import full_timeout, plan_orders

    pool = [{"timing": "provider", "ref": str(i)} for i in range(8)]
    pool += [
        {"timing": "align", "ref": "A"},
        {"timing": "align", "ref": "B"},
        {"timing": "skip", "ref": "S"},
    ]
    plan = plan_orders(pool, 10, seed=23)
    assert Counter(song["timing"] for song in plan) == {"provider": 8, "align": 2}
    assert len({song["ref"] for song in plan}) == 10
    assert plan == plan_orders(pool, 10, seed=23)
    plan_with_skip = plan_orders(pool, 20, provider_ratio=0.75, skip_ratio=0.2)
    assert Counter(song["timing"] for song in plan_with_skip) == {
        "provider": 12,
        "align": 4,
        "skip": 4,
    }
    assert full_timeout(plan_with_skip) > full_timeout(plan) > 420


def test_browser_observer_follows_swapped_video_elements(tmp_path):
    """Exercise actual Chromium end events and the same roles as the TV UI."""
    import base64
    from pathlib import Path

    from benchmarks.real_party import PLAYBACK_EVENTS, PLAYBACK_SAMPLE

    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("optional Chromium is not installed")
        clip = tmp_path / "tiny.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:r=10:d=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(clip),
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        uri = "data:video/mp4;base64," + base64.b64encode(clip.read_bytes()).decode()
        browser = playwright.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        try:
            page = browser.new_page()
            page.set_content(
                '<video id="video" muted></video>'
                '<video id="preload" class="preloading" muted></video>'
                '<audio id="backing-audio"></audio>'
                "<script>let activeId = 1;</script>"
            )
            page.evaluate(PLAYBACK_EVENTS)
            page.evaluate(
                "(uri) => { document.querySelector('#video').src = uri; }", uri
            )
            page.evaluate("document.querySelector('#video').play()")
            page.wait_for_function("document.querySelector('#video').currentTime > 0.1")
            first = page.evaluate(PLAYBACK_SAMPLE)
            assert first["element"] == "video" and first["client_song_id"] == 1
            page.wait_for_function("window.__benchmarkEnded.length === 1")
            page.evaluate(
                """(uri) => {
                document.querySelector('#video').classList.add('preloading');
                document.querySelector('#preload').classList.remove('preloading');
                activeId = 2;
                document.querySelector('#preload').src = uri;
                document.querySelector('#video').removeAttribute('src');
                document.querySelector('#video').load();
            }""",
                uri,
            )
            page.evaluate("document.querySelector('#preload').play()")
            page.wait_for_function(
                "document.querySelector('#preload').currentTime > 0.1"
            )
            second = page.evaluate(PLAYBACK_SAMPLE)
            assert second["element"] == "preload" and second["client_song_id"] == 2
            assert second["video"] > 0.1 and not second["paused"]
            assert page.evaluate("document.querySelector('#video').currentTime") == 0
            page.wait_for_function("window.__benchmarkEnded.length === 2")
            ended = page.evaluate("window.__benchmarkEnded")
            assert [(e["song_id"], e["element"]) for e in ended] == [
                (1, "video"),
                (2, "preload"),
            ]
            assert all(abs(e["position"] - e["duration"]) < 0.1 for e in ended)
        finally:
            browser.close()
