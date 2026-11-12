"""Queue saturation with synthetic ready media and late-joining guests."""

import argparse
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmarks.party import Config, Simulation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=2407)
    parser.add_argument(
        "--users", type=int, default=60, help="Total potential guest count"
    )
    parser.add_argument("--initial-users", type=int, default=48)
    parser.add_argument("--songs-per-user", type=int, default=12)
    parser.add_argument("--steps", type=int, default=950)
    parser.add_argument("--max-orders", type=int, default=820)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/.results"))
    args = parser.parse_args()
    if not 1 <= args.initial_users <= args.users or not 1 <= args.songs_per_user <= 20:
        parser.error("Require 1 <= initial users <= users, and 1–20 songs per user")
    if args.max_orders < args.initial_users * args.songs_per_user:
        parser.error("max-orders must accommodate the initial queue")

    cfg = Config(
        seed=args.seed,
        users=args.users,
        steps=args.steps,
        max_orders=args.max_orders,
        p_join=0.06,
        p_order=0.22,
        p_lyrics=0.17,
        p_skip_lyrics=0.06,
        p_cancel=0.10,
        p_worker_complete=0.15,
        p_playback=0.65,
        p_pin=0.07,
        p_replay=0.06,
        p_switch_policy=0.06,
        p_toggle_orders=0,
        p_netease=0.45,
        p_bad_timing=0.12,
        p_line_review=0.43,
        p_burst=0.19,
        check_every=24,
    )
    with TemporaryDirectory(prefix="vcktv-mass-ready-party-") as dir:
        sim = Simulation(cfg, Path(dir))
        print("SETUP_START", flush=True)
        begin = time.monotonic()
        for _ in range(args.initial_users):
            sim.users_add()
        for u in sim.users:
            for _ in range(args.songs_per_user):
                index = len(sim.all_songs) + 1
                kind = "netease" if (index + len(u.name)) % 3 == 0 else "bilibili"
                ref = str(100000000 + index) if kind == "netease" else f"BV{index:010d}"
                duration = sim.rng.randint(90, 410)
                sim.durations[ref] = duration
                if kind == "netease":
                    response = u.client.post("/api/netease/songs", json={"ref": ref})
                else:
                    response = u.client.post("/api/songs", json={"bvid": ref})
                assert response.status_code == 200, (
                    u.name,
                    index,
                    response.status_code,
                    response.text[:180],
                )
                sid = response.json()["id"]
                sim.all_songs[sid] = {
                    "id": sid,
                    "user": u,
                    "kind": kind,
                    "duration": duration,
                }
                assert u.client.post(f"/api/songs/{sid}/lyrics/skip").status_code == 200
        with sim.store.lock:
            sim.store.db.execute(
                "UPDATE songs SET status='ready',prep_state='original',source_file='fixture.mp4',media_file='fixture.mp4',prep_stage='done'"
            )
            sim.store.db.commit()
        before = sim.store.fair_queue()
        assert len(before) == args.initial_users * args.songs_per_user
        first_turn = [s["requester_id"] for s in before[: args.initial_users]]
        assert len(set(first_turn)) == args.initial_users, (
            "unfair first ready rotation",
            len(set(first_turn)),
        )
        print(
            "SCALE_SETUP",
            len(sim.users),
            "users",
            len(before),
            "ready songs",
            round(time.monotonic() - begin, 2),
            "seconds; first_turn_unique",
            len(set(first_turn)),
            flush=True,
        )
        measure = []
        for _ in range(10):
            start = time.monotonic()
            sim.store.fair_queue()
            measure.append((time.monotonic() - start) * 1000)
        print(
            "FAIR_PREVIEW_TIMINGS_ms",
            {
                "min": round(min(measure), 2),
                "median": round(sorted(measure)[5], 2),
                "max": round(max(measure), 2),
            },
            flush=True,
        )
        out = sim.run()
        summary = {
            "setup_ready_songs": len(before),
            "initial_people": args.initial_users,
            "first_turn_unique": len(set(first_turn)),
            "queue_preview_ms": {
                "median": round(sorted(measure)[5], 2),
                "max": round(max(measure), 2),
            },
            "scenario": out,
        }
        output = args.output / f"seed-{args.seed}-mass.json"
        output.parent.mkdir(exist_ok=True, parents=True)
        output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print(
            "MASS_SUMMARY",
            json.dumps({k: v for k, v in summary.items() if k != "scenario"}),
            flush=True,
        )


if __name__ == "__main__":
    main()
