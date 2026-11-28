"""Opt-in real-media multi-user KTV benchmark on an isolated loopback server."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from benchmarks.real_metrics import RealMetrics
from vc_ktv.processing.backing_audio import KARAOKE_FILENAME


def read_manifest(path: Path):
    items = json.loads(path.read_text())
    if not isinstance(items, list) or not items:
        raise ValueError("manifest must be a nonempty JSON array")
    for item in items:
        if not isinstance(item, dict) or item.get("source") not in (
            "bilibili",
            "netease",
        ):
            raise ValueError("each song needs source=bilibili or netease")
        if not isinstance(item.get("ref"), str) or not item["ref"].strip():
            raise ValueError("each song needs a nonempty ref string")
        if item.get("timing") not in ("align", "provider", "skip"):
            raise ValueError("timing must be align, provider or skip")
        if item.get("lyrics_file"):
            if item["timing"] != "align":
                raise ValueError("uploaded LRC requires timing=align")
            item["lyrics_file"] = str((path.parent / item["lyrics_file"]).resolve())
            if not Path(item["lyrics_file"]).is_file():
                raise ValueError("lyrics_file does not exist")
    return items


def plan_orders(manifest, count, provider_ratio=0.8, skip_ratio=0.0, seed=42):
    """Build a seeded, stratified workload using eligible songs from the manifest."""
    if count < 1 or not 0 <= provider_ratio <= 1 or not 0 <= skip_ratio <= 1:
        raise ValueError("invalid song count or timing ratio")
    # Ratios are conditional on having lyrics, with separate no-lyrics share.
    skips = int(count * skip_ratio + 0.5)
    providers = int((count - skips) * provider_ratio + 0.5)
    counts = {"provider": providers, "align": count - skips - providers, "skip": skips}
    rng = random.Random(seed)
    orders = []
    for timing, n in counts.items():
        pool = [item for item in manifest if item["timing"] == timing]
        if n and not pool:
            raise ValueError(f"manifest needs at least one {timing} song")
        while n:
            cycle = rng.sample(pool, len(pool))
            selected = cycle[:n]
            orders.extend(selected)
            n -= len(selected)
    rng.shuffle(orders)
    return orders


def full_timeout(orders):
    # Conservative test budget, not a production latency objective. Alignment
    # and backing may compete, and the backing lane is serial.
    counts = Counter(item["timing"] for item in orders)
    return max(1200, 300 + 420 * counts["align"] + 150 * len(orders))


def get_json(response):
    if not response.is_success:
        raise RuntimeError(
            f"HTTP {response.status_code} for {response.request.method} {response.request.url.path}"
        )
    return response.json()


def port_number():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(predicate, deadline, description):
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(1)
    raise TimeoutError(description)


def submit(client, item):
    if item["source"] == "bilibili":
        result = client.post(
            "/api/songs", json={"bvid": item["ref"], "page": item.get("page", 1)}
        )
    else:
        result = client.post("/api/netease/songs", json={"ref": item["ref"]})
    sid = get_json(result)["id"]
    timing = item["timing"]
    if timing == "skip":
        get_json(client.post(f"/api/songs/{sid}/lyrics/skip"))
    elif item.get("lyrics_file"):
        with Path(item["lyrics_file"]).open("rb") as stream:
            get_json(
                client.post(
                    f"/api/songs/{sid}/lyrics/upload",
                    files={"file": ("lyrics.lrc", stream)},
                )
            )
    else:
        candidates = get_json(
            client.get(
                f"/api/songs/{sid}/lyrics/candidates",
                params={"query": item.get("query", "")},
            )
        )["candidates"]
        candidates = [
            c
            for c in candidates
            if len(c.get("text", "")) >= 10
            and (timing != "provider" or c.get("timing_ticket"))
        ]
        if not candidates:
            raise RuntimeError(
                f"no suitable lyrics for song {sid}; supply query or lyrics_file"
            )
        candidate = candidates[0]
        payload = {key: candidate[key] for key in ("text", "ticket", "source")}
        payload["timing_mode"] = timing
        if timing == "provider":
            payload.update(
                {
                    key: candidate[key]
                    for key in ("timed_lrc", "source_lrc_raw", "timing_ticket")
                }
            )
        get_json(client.post(f"/api/songs/{sid}/lyrics/select", json=payload))
    return sid


def state(admin):
    return get_json(admin.get("/api/admin/state"))


def wait_ready(admin, ids, submitted, deadline, ready_times):
    def check():
        snapshot = state(admin)
        pending = {s["id"]: s for s in snapshot["waiting"]}
        ready = {s["id"] for s in snapshot["queue"]}
        for sid in ids:
            if sid in ready and sid not in ready_times:
                ready_times[sid] = round(time.monotonic() - submitted[sid], 2)
            song = pending.get(sid)
            if song and (
                song.get("prep_state") == "failed"
                or (
                    song.get("prep_state") == "unavailable"
                    and song.get("prep_attempts", 0) >= 3
                )
                or song.get("refresh_state") == "failed"
                or (
                    song.get("prep_state") == "original"
                    and not song.get("skip_subtitles")
                    and song.get("prep_error")
                )
            ):
                raise RuntimeError(
                    f"preparation failed for song {sid}: {song.get('prep_state')}"
                )
        return ids <= ready

    wait_until(
        check, deadline, "songs did not become subtitle/backing ready before timeout"
    )


def check_range(client, url):
    # Request a small range, never download the whole media in the API test.
    with client.stream("GET", url, headers={"Range": "bytes=0-4095"}) as response:
        assert response.status_code == 206, response.status_code
        assert response.headers.get("content-range", "").startswith("bytes 0-")
        assert next(response.iter_bytes(), b"")
    return True


def api_playback(admin, base, count):
    """Check actual signed media ranges and TV state, not browser decoding."""
    pair_url = get_json(admin.get("/api/admin/tv-link"))["url"]
    from urllib.parse import parse_qs

    pair = parse_qs(urlsplit(pair_url).query)["pair"][0]
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as tv:
        token = get_json(tv.post("/api/tv/activate", json={"pair": pair}))["token"]
        headers = {"Authorization": f"Bearer {token}"}
        played = []
        first_key = None
        for _ in range(count):
            active = get_json(tv.post("/api/tv/next", headers=headers))["active"]
            assert active and active["backing"]["status"] == "ready"
            check_range(tv, active["media_url"])
            check_range(tv, active["backing"]["url"])
            if active.get("html_overlay"):
                check_range(tv, active["html_overlay"]["source_url"])
                get_json(tv.get(active["html_overlay"]["timeline_url"]))
            played.append(active["id"])
            if first_key is None:
                first_key = active["backing"]["cache_key"]
            get_json(
                tv.post(
                    "/api/tv/finish",
                    headers=headers,
                    json={"song_id": active["id"], "reason": "completed"},
                )
            )
        replay = get_json(admin.post(f"/api/admin/songs/{played[0]}/replay"))["id"]
        active = get_json(tv.post("/api/tv/next", headers=headers))["active"]
        assert active and active["id"] == replay
        assert active["backing"]["status"] == "ready"
        assert active["backing"]["cache_key"] == first_key
        check_range(tv, active["media_url"])
        check_range(tv, active["backing"]["url"])
        get_json(
            tv.post(
                "/api/tv/finish",
                headers=headers,
                json={"song_id": replay, "reason": "completed"},
            )
        )
        return {
            "mode": "api",
            "range_checked_songs": played,
            "replay_cache_reused": True,
        }


def browser_playback(admin, base, seconds):
    """Exercise the shipped TV UI with a real Chromium media decoder."""
    from playwright.sync_api import sync_playwright

    pair_url = get_json(admin.get("/api/admin/tv-link"))["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        try:
            page = browser.new_page()
            page.goto(base + pair_url, wait_until="domcontentloaded")
            page.locator("#start").click(timeout=30000)
            page.wait_for_function(
                "document.querySelector('#video').currentTime > 0.5 && !document.querySelector('#video').paused",
                timeout=60000,
            )
            page.locator("#panel-toggle").click()
            page.locator("#audio-mode").click(timeout=30000)
            page.wait_for_function(
                "document.querySelector('#audio-mode').getAttribute('aria-pressed') === 'true'",
                timeout=15000,
            )
            samples = []
            for _ in range(seconds):
                page.wait_for_timeout(1000)
                sample = page.evaluate("""() => {
                    const v=document.querySelector('#video'), a=document.querySelector('#backing-audio');
                    return {video:v.currentTime, audio:a.currentTime, muted:v.muted,
                            videoPaused:v.paused, audioPaused:a.paused,
                            videoError:v.error?.code ?? null, audioError:a.error?.code ?? null};
                }""")
                assert (
                    sample["muted"]
                    and not sample["videoPaused"]
                    and not sample["audioPaused"]
                )
                assert sample["videoError"] is None and sample["audioError"] is None
                samples.append(sample)
            assert samples[-1]["video"] > samples[0]["video"]
            page.locator("#audio-mode").click()
            page.wait_for_function(
                "document.querySelector('#audio-mode').getAttribute('aria-pressed') === 'false' && !document.querySelector('#video').muted",
                timeout=15000,
            )
            return {
                "mode": "browser",
                "samples": samples,
                "max_media_clock_delta_seconds": max(
                    abs(s["audio"] - s["video"]) for s in samples
                ),
            }
        finally:
            browser.close()


def preparation_snapshot(root):
    """Read only the isolated test room's stage counts, including backing."""
    data = root / "data"
    event_id = (data / "active-session").read_text().strip()
    event = data / "sessions" / event_id
    database = next(
        p for p in event.glob("*.sqlite*") if p.suffix in (".sqlite", ".sqlite3")
    )

    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        return {
            "preparation_states": dict(
                db.execute("SELECT prep_state,count(*) FROM songs GROUP BY prep_state")
            ),
            "preparation_stages": dict(
                db.execute("SELECT prep_stage,count(*) FROM songs GROUP BY prep_stage")
            ),
            "backing_states": dict(
                db.execute("SELECT status,count(*) FROM backing_tracks GROUP BY status")
            ),
            "preparation_errors": db.execute(
                "SELECT count(*) FROM songs WHERE prep_error != '' OR refresh_error != ''"
            ).fetchone()[0],
        }


def run(args, items, root):
    pipeline = args.pipeline_root.expanduser().resolve()
    required = [
        pipeline / "runtime/kim/bin/python",
        pipeline / "runtime/tifa/bin/python",
        pipeline / "runtime/qwen/bin/python",
        pipeline / "models/separation/Karaoke" / KARAOKE_FILENAME,
    ]
    if any(not path.is_file() for path in required):
        raise RuntimeError(
            "GPU runtime or accompaniment checkpoint missing; install separately"
        )
    port = port_number()
    base = f"http://127.0.0.1:{port}"
    password = secrets.token_urlsafe(24)
    config = root / "config"
    config.mkdir()
    (config / "admin-password").write_text(password)
    (config / "token-secret").write_text(secrets.token_hex(32))
    env = {
        **os.environ,
        "VC_KTV_CONFIG": str(config),
        "VC_KTV_DATA": str(root / "data"),
        "VC_KTV_PIPELINE_ROOT": str(pipeline),
        "VC_KTV_HOST": "127.0.0.1",
        "VC_KTV_PORT": str(port),
        "VC_KTV_AUTOPREPARE": "1",
        "VC_KTV_GPU_ALIGNMENT": "1",
        "VC_KTV_BACKING_TRACKS": "1",
        "VC_KTV_PIPELINE_MODE": args.pipeline_mode,
        "VC_KTV_KIM_SINGLE_FLIGHT": str(args.kim_single_flight),
        "VC_KTV_PREP_WORKERS": str(args.general_workers),
        "VC_KTV_FAST_WORKERS": str(args.fast_workers),
        "VC_KTV_BACKING_CHECKPOINT": str(required[-1]),
    }
    with (root / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "vc_ktv"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            with ExitStack() as stack:
                admin = stack.enter_context(
                    httpx.Client(base_url=base, timeout=60, trust_env=False)
                )

                def health():
                    if process.poll() is not None:
                        raise RuntimeError("isolated server exited")
                    try:
                        return get_json(admin.get("/api/health"))
                    except httpx.HTTPError:
                        return None

                wait_until(health, time.monotonic() + 60, "server startup timeout")
                get_json(admin.post("/api/admin/login", json={"password": password}))
                code = "654321"
                get_json(
                    admin.post(
                        "/api/admin/events",
                        json={"name": "Real benchmark", "code": code},
                    )
                )
                wait_until(
                    lambda: (
                        (h := health())
                        and h["backing_ready"]
                        and h["autoprep_ready"]
                        and h["gpu_alignment"]
                    ),
                    time.monotonic() + 120,
                    "GPU/backing worker startup timeout",
                )
                get_json(
                    admin.put(
                        "/api/admin/settings",
                        json={
                            "require_backing_ready": not args.pressure_seconds,
                            "max_bandwidth_mbps": args.bandwidth,
                            "allow_unsubtitled": False,
                            "video_quality": args.quality,
                            "original_codec_mode": args.codec,
                        },
                    )
                )
                get_json(admin.put("/api/admin/playback-mode", json={"mode": "tv"}))
                clients = []
                submitted = {}
                ready_times = {}
                order_times = {}
                read_clients = [(admin, "/api/admin/state")]
                # All requested users are actual registered clients, even if
                # some only watch the queue. New joiners can be added later.
                for user in range(args.users + args.additional_readers):
                    client = stack.enter_context(
                        httpx.Client(base_url=base, timeout=90, trust_env=False)
                    )
                    get_json(client.post("/api/join", json={"code": code}))
                    get_json(
                        client.post(
                            "/api/users/register",
                            json={
                                "username": f"Bench-{user}",
                                "password": password,
                                "confirm_password": password,
                            },
                        )
                    )
                    clients.append(client)
                    if user < args.users:
                        read_clients.append((client, "/api/state"))
                metrics = stack.enter_context(
                    RealMetrics(read_clients, args.poll_interval)
                )
                time.sleep(args.poll_interval * 2)
                metrics.phase = (
                    f"preparation_{args.users}_users"
                    if args.pressure_seconds
                    else "preparation"
                )

                def order(i, item):
                    user = i % args.users
                    started = time.monotonic()
                    sid = submit(clients[user], item)
                    print(
                        f"ORDER {sid} user={user} timing={item['timing']}", flush=True
                    )
                    return sid, started, round(time.monotonic() - started, 2)

                with ThreadPoolExecutor(max_workers=args.submit_workers) as pool:
                    # Bounded submission concurrency: mimic independent users
                    # without flooding the public media/lyrics providers.
                    for start in range(0, len(items), args.submit_workers):
                        futures = [
                            pool.submit(order, i, items[i])
                            for i in range(
                                start, min(start + args.submit_workers, len(items))
                            )
                        ]
                        for future in futures:
                            sid, started, elapsed = future.result()
                            submitted[sid] = started
                            order_times[sid] = elapsed
                        if args.order_interval:
                            time.sleep(args.order_interval)
                ids = set(submitted)
                if args.pressure_seconds:
                    pressure_start = time.monotonic()
                    deadline = pressure_start + args.pressure_seconds
                    ramped = False
                    while time.monotonic() < deadline:
                        if (
                            args.additional_readers
                            and not ramped
                            and time.monotonic()
                            >= pressure_start + args.pressure_seconds / 2
                        ):
                            metrics.phase = f"preparation_{args.users + args.additional_readers}_users"
                            read_clients.extend(
                                (client, "/api/state")
                                for client in clients[args.users :]
                            )
                            ramped = True
                        snapshot = state(admin)
                        for song in snapshot["queue"]:
                            sid = song["id"]
                            if sid in submitted:
                                ready_times.setdefault(
                                    sid, round(time.monotonic() - submitted[sid], 2)
                                )
                        time.sleep(
                            min(args.poll_interval, max(0, deadline - time.monotonic()))
                        )
                    metrics.stop_signal.set()
                    metrics.thread.join()
                    return {
                        "mode": "bounded_pressure",
                        "songs": len(ids),
                        "users": len(clients),
                        "ordering_users": args.users,
                        "additional_readers": args.additional_readers,
                        "pressure_seconds_after_submission": args.pressure_seconds,
                        "worker_config": {
                            "general": args.general_workers,
                            "fast": args.fast_workers,
                            "backing": 1,
                            "kim_single_flight": args.kim_single_flight,
                            "pipeline_mode": args.pipeline_mode,
                        },
                        "media_config": {
                            "quality": args.quality,
                            "codec": args.codec,
                            "bandwidth": args.bandwidth,
                            "require_backing_ready": False,
                        },
                        "observed_ready_seconds": ready_times,
                        "order_and_lyrics_seconds": order_times,
                        "snapshot": preparation_snapshot(root),
                        "metrics": metrics.summary(),
                        "playback": {
                            "mode": "not_run",
                            "reason": "bounded pressure observation",
                        },
                    }
                wait_ready(
                    admin, ids, submitted, time.monotonic() + args.timeout, ready_times
                )
                refresh_count = 0
                if args.refresh:
                    metrics.phase = "refresh"
                    other = "av1" if args.codec == "h264" else "h264"
                    get_json(
                        admin.put(
                            "/api/admin/settings",
                            json={
                                "original_codec_mode": other,
                                "subtitle_style": "large",
                            },
                        )
                    )
                    refresh_count = get_json(
                        admin.post("/api/admin/songs/refresh-outdated")
                    )["count"]
                    wait_ready(
                        admin, ids, submitted, time.monotonic() + args.timeout, {}
                    )
                metrics.phase = "playback"
                if args.playback == "browser":
                    playback = browser_playback(admin, base, args.switch_seconds)
                elif args.playback == "manual":
                    url = get_json(admin.get("/api/admin/tv-link"))["url"]
                    print(
                        "Open the temporary TV page locally (or use an SSH tunnel):",
                        base + url,
                        flush=True,
                    )
                    input("Press Enter when manual playback verification is finished. ")
                    playback = {"mode": "manual", "verified_by_script": False}
                else:
                    playback = api_playback(admin, base, len(ids))
                metrics.stop_signal.set()
                metrics.thread.join()
                return {
                    "songs": len(ids),
                    "worker_config": {
                        "general": args.general_workers,
                        "fast": args.fast_workers,
                        "backing": 1,
                        "kim_single_flight": args.kim_single_flight,
                        "pipeline_mode": args.pipeline_mode,
                    },
                    "metrics": metrics.summary(),
                    "users": len(clients),
                    "observed_ready_seconds": ready_times,
                    "refreshed_songs": refresh_count,
                    "playback": playback,
                }
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--execute", action="store_true", help="Explicitly enable network/GPU work"
    )
    parser.add_argument(
        "--pipeline-root",
        type=Path,
        default=Path(
            os.getenv("VC_KTV_PIPELINE_ROOT", "~/.local/share/vc-ktv/pipeline")
        ),
    )
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument(
        "--additional-readers",
        type=int,
        default=0,
        help="add queue-only readers halfway through bounded pressure",
    )
    parser.add_argument(
        "--repeat", type=int, default=1, help="repeat the song manifest"
    )
    parser.add_argument(
        "--orders",
        type=int,
        help="sample this many orders with configurable timing ratios",
    )
    parser.add_argument(
        "--provider-ratio",
        type=float,
        default=0.8,
        help="source timeline share among songs with lyrics",
    )
    parser.add_argument(
        "--skip-ratio", type=float, default=0, help="share of songs without lyrics"
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--bandwidth", type=float, default=3)
    parser.add_argument(
        "--pipeline-mode",
        choices=("quality", "fast", "global", "global-kim"),
        default="quality",
    )
    parser.add_argument("--kim-single-flight", type=int, choices=(0, 1), default=1)
    parser.add_argument("--general-workers", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--fast-workers", type=int, choices=range(3), default=2)
    parser.add_argument("--quality", type=int, choices=(480, 720, 1080), default=1080)
    parser.add_argument("--codec", choices=("default", "h264", "av1"), default="h264")
    parser.add_argument(
        "--timeout",
        type=int,
        help="full completion deadline in seconds; default scales with song count",
    )
    parser.add_argument("--order-interval", type=float, default=1)
    parser.add_argument("--submit-workers", type=int, default=1, choices=range(1, 5))
    parser.add_argument(
        "--pressure-seconds",
        type=int,
        default=0,
        help="observe after submission, then stop without waiting for every song; 0 runs full playback",
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--playback", choices=("api", "browser", "manual"), default="api"
    )
    parser.add_argument("--switch-seconds", type=int, default=5)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/.results"))
    args = parser.parse_args()
    try:
        if (
            not 1 <= args.repeat <= 1000
            or not math.isfinite(args.poll_interval)
            or args.poll_interval < 1
            or not 1 <= args.bandwidth <= 100
        ):
            raise ValueError("invalid repeat, poll interval or bandwidth")
        manifest = read_manifest(args.manifest)
        if args.orders is not None:
            if not 1 <= args.orders <= 1000 or args.repeat != 1:
                raise ValueError(
                    "--orders must be 1–1000 and cannot be combined with --repeat"
                )
            items = plan_orders(
                manifest, args.orders, args.provider_ratio, args.skip_ratio, args.seed
            )
        else:
            items = manifest * args.repeat
        if args.timeout is None:
            args.timeout = full_timeout(items)
        if not 1 <= args.users <= 50 or len(items) > min(1000, args.users * 20):
            raise ValueError(
                "users must be 1–50 and orders must fit the per-user limit"
            )
        if (
            args.timeout < 1
            or args.pressure_seconds < 0
            or args.additional_readers < 0
            or args.users + args.additional_readers > 50
            or (args.additional_readers and not args.pressure_seconds)
            or (args.pressure_seconds and args.refresh)
            or args.submit_workers > args.users
            or not math.isfinite(args.order_interval)
            or args.order_interval < 0
            or args.switch_seconds < 2
        ):
            raise ValueError("invalid timing arguments")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if not args.execute:
        print(
            f"PLAN ONLY: {len(items)} songs; timing={dict(Counter(item['timing'] for item in items))}; full timeout={args.timeout}s; playback={args.playback}. Use --execute to run."
        )
        return
    if args.playback == "browser":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            parser.error("browser mode requires Playwright")
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                parser.error(
                    "install Playwright Chromium before starting real preparation"
                )
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="vcktv-real-party-") as folder:
            try:
                result = run(args, items, Path(folder))
            except Exception:
                log = Path(folder) / "server.log"
                if log.exists():
                    destination = (
                        args.output / f"real-party-local-debug-{time.time_ns()}.log"
                    )
                    destination.write_bytes(log.read_bytes())
                    destination.chmod(0o600)
                raise
    except Exception as error:
        (args.output / f"real-party-failed-{time.time_ns()}.json").write_text(
            json.dumps({"error_type": type(error).__name__}, indent=2) + "\n"
        )
        raise
    result["timing_counts"] = dict(Counter(item["timing"] for item in items))
    result["distinct_sources"] = len(
        {(item["source"], item["ref"], item.get("page", 1)) for item in items}
    )
    result["seed"] = args.seed
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / f"real-party-{time.time_ns()}.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    summary = {**result}
    if "metrics" in summary:
        summary["metrics"] = {"phases": summary["metrics"]["phases"]}
    print("SUMMARY", json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
