"""Two-client races between guest lyric/cancel actions and admin controls."""

import argparse
import concurrent.futures
import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmarks.party import Config, Simulation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=2408)
    parser.add_argument("--users", type=int, default=12)
    parser.add_argument("--rounds", type=int, default=35, help="Pairs per race type")
    parser.add_argument("--output", type=Path, default=Path("benchmarks/.results"))
    args = parser.parse_args()
    if args.users < 1 or args.rounds < 1 or 4 * args.rounds > 1000:
        parser.error("users must be positive; at most 1000 songs per event")

    with TemporaryDirectory(prefix="vcktv-race-party-") as tmp:
        cfg = Config(
            seed=args.seed,
            users=args.users,
            max_orders=max(650, 4 * args.rounds),
            steps=0,
        )
        sim = Simulation(cfg, Path(tmp))
        for _ in range(args.users):
            sim.users_add()
        stats = {}

        def order_for(user):
            index = len(sim.all_songs) + 1
            ref = f"BV{index:010d}"
            sim.durations[ref] = 150
            response = user.client.post("/api/songs", json={"bvid": ref})
            assert response.status_code == 200, (
                response.status_code,
                response.text[:180],
            )
            sid = response.json()["id"]
            sim.all_songs[sid] = {
                "id": sid,
                "user": user,
                "kind": "bilibili",
                "duration": 150,
            }
            return sid

        def ready_song(sid):
            with sim.store.lock:
                sim.store.db.execute(
                    "UPDATE songs SET status='ready',prep_state='original',skip_subtitles=1,source_file='fixture.mp4',media_file='fixture.mp4' WHERE id=?",
                    (sid,),
                )
                sim.store.db.commit()

        def parallel_pair(fn_a, fn_b):
            latch = threading.Barrier(3)

            def execute(fn):
                latch.wait(timeout=5)
                return fn()

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(execute, fn_a)
                second = pool.submit(execute, fn_b)
                latch.wait(timeout=5)
                return first.result(timeout=12), second.result(timeout=12)

        try:
            for mode in (
                "cancel_vs_select",
                "cancel_vs_pin",
                "cancel_vs_claim",
                "skip_vs_select",
            ):
                counts = {}
                steps = args.rounds
                for i in range(steps):
                    user = sim.users[(i * 7) % len(sim.users)]
                    sid = order_for(user)
                    if mode in ("cancel_vs_pin", "cancel_vs_claim"):
                        ready_song(sid)
                    if mode in ("cancel_vs_select", "skip_vs_select"):
                        ticket = sim.candidate(sim.all_songs[sid])[0]
                        payload = dict(
                            text=ticket["text"],
                            ticket=ticket["ticket"],
                            source=ticket["source"],
                            timing_mode="provider",
                            timed_lrc=ticket.get("timed_lrc") or "",
                            source_lrc_raw=ticket.get("source_lrc_raw") or "",
                            timing_ticket=ticket.get("timing_ticket") or "",
                            offset_ms=0,
                        )
                        assert ticket.get("timed_lrc"), ticket
                        chooser = lambda: user.client.post(
                            f"/api/songs/{sid}/lyrics/select", json=payload
                        )
                    if mode == "cancel_vs_select":
                        a, b = parallel_pair(
                            lambda: user.client.post(f"/api/songs/{sid}/cancel"),
                            chooser,
                        )
                        assert a.status_code == 200 and b.status_code in (200, 409), (
                            mode,
                            a.status_code,
                            b.status_code,
                            b.text[:150],
                        )
                        assert sim.row(sid)["status"] == "cancelled"
                    elif mode == "cancel_vs_pin":
                        a, b = parallel_pair(
                            lambda: user.client.post(f"/api/songs/{sid}/cancel"),
                            lambda: sim.admin.post(f"/api/admin/songs/{sid}/pin-next"),
                        )
                        assert a.status_code == 200 and b.status_code in (200, 409), (
                            mode,
                            a.status_code,
                            b.status_code,
                            b.text[:150],
                        )
                        assert sim.row(sid)["status"] == "cancelled"
                    elif mode == "cancel_vs_claim":
                        a, b = parallel_pair(
                            lambda: user.client.post(f"/api/songs/{sid}/cancel"),
                            lambda: sim.admin.post("/api/admin/next"),
                        )
                        assert a.status_code in (200, 409) and b.status_code == 200, (
                            mode,
                            a.status_code,
                            b.status_code,
                            a.text[:120],
                        )
                        row = sim.row(sid)
                        assert row["status"] in ("cancelled", "playing"), (
                            mode,
                            row["status"],
                        )
                        if row["status"] == "playing":
                            assert (
                                sim.admin.post(
                                    "/api/admin/finish",
                                    json={"song_id": sid, "reason": "skip"},
                                ).status_code
                                == 200
                            )
                        else:
                            assert a.status_code == 200
                    else:
                        a, b = parallel_pair(
                            lambda: user.client.post(f"/api/songs/{sid}/lyrics/skip"),
                            chooser,
                        )
                        assert a.status_code == b.status_code == 200, (
                            mode,
                            a.status_code,
                            b.status_code,
                            a.text[:100],
                            b.text[:100],
                        )
                        row = sim.row(sid)
                        if row["skip_subtitles"]:
                            assert (
                                row["lyric_choice"] == ""
                                and row["provider_lrc"] == ""
                                and row["lyric_offset_ms"] == 0
                            )
                        else:
                            assert (
                                row["lyric_choice"]
                                and row["lyric_timing_mode"] == "provider"
                                and row["provider_lrc"]
                            )
                            assert row["lyric_offset_ms"] == 0
                    counts[(a.status_code, b.status_code)] = (
                        counts.get((a.status_code, b.status_code), 0) + 1
                    )
                    if i % 8 == 0:
                        sim.invariant()
                stats[mode] = {
                    "attempts": steps,
                    "response_pairs": {str(k): v for k, v in counts.items()},
                }
                print("RACE_CASE", mode, json.dumps(stats[mode]), flush=True)
            sim.invariant()
            result = {
                "scenario": str(args.seed),
                "race_cases": stats,
                "scheduler_checks": sim.reference_checks,
                "bugs": [],
            }
            args.output.mkdir(parents=True, exist_ok=True)
            path = args.output / f"seed-{args.seed}-races.json"
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(
                "RACE_SUMMARY",
                sum(x["attempts"] for x in stats.values()),
                "concurrent pairs; all invariants held",
                flush=True,
            )
        finally:
            sim.close()


if __name__ == "__main__":
    main()
