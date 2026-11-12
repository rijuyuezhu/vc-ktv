"""Configurable probabilistic KTV guests, lyric timing, queue/pin/cancel/replay.

Runs only against a fresh ephemeral local server and database. Fixed seeds reproduce
failure sequences. Real API signing/auth and SQL transitions, worker completion mocked.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import random
import socket
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

import httpx
import uvicorn

from benchmarks.scheduler_reference import reference_queue
from vc_ktv.application.factory import create_app
from vc_ktv.lyrics.lyric_selection import _candidate
from vc_ktv.lyrics.timed_lyrics import offset_timeline


@dataclass
class Config:
    seed: int = 303
    users: int = 25
    steps: int = 1200
    max_orders: int = 300
    p_join: float = 0.05
    p_order: float = 0.30
    p_lyrics: float = 0.14
    p_skip_lyrics: float = 0.08
    p_edit_lyrics: float = 0.05
    p_cancel: float = 0.12
    p_worker_complete: float = 0.13
    p_playback: float = 0.10
    p_pin: float = 0.04
    p_replay: float = 0.035
    p_switch_policy: float = 0.02
    p_toggle_orders: float = 0.025
    p_bad_timing: float = 0.15
    p_line_review: float = 0.24
    p_burst: float = 0.12
    p_netease: float = 0.38
    p_skip_playing: float = 0.28
    duration_min: int = 12
    duration_max: int = 480
    check_every: int = 15


@dataclass
class Guest:
    name: str
    client: httpx.Client
    style: str
    created_at: int


def validate_config(config: Config) -> None:
    if config.users < 1 or config.steps < 0 or config.max_orders < 0:
        raise ValueError("users must be positive; steps and max_orders nonnegative")
    if (
        config.check_every < 1
        or config.duration_min < 1
        or config.duration_max < config.duration_min
    ):
        raise ValueError("invalid check interval or song duration range")
    probabilities = vars(config)
    action_weights = [
        value
        for key, value in probabilities.items()
        if key.startswith("p_")
        and key
        in {
            "p_join",
            "p_order",
            "p_lyrics",
            "p_skip_lyrics",
            "p_edit_lyrics",
            "p_cancel",
            "p_worker_complete",
            "p_playback",
            "p_pin",
            "p_replay",
            "p_switch_policy",
            "p_toggle_orders",
        }
    ]
    if any(
        not math.isfinite(weight) or weight < 0 for weight in action_weights
    ) or not any(action_weights):
        raise ValueError(
            "action weights must be nonnegative, with at least one positive"
        )
    for key, value in probabilities.items():
        if (
            key.startswith("p_")
            and key
            not in {
                "p_join",
                "p_order",
                "p_lyrics",
                "p_skip_lyrics",
                "p_edit_lyrics",
                "p_cancel",
                "p_worker_complete",
                "p_playback",
                "p_pin",
                "p_replay",
                "p_switch_policy",
                "p_toggle_orders",
            }
            and (not math.isfinite(value) or not 0 <= value <= 1)
        ):
            raise ValueError(f"{key} must be between 0 and 1")


class Simulation:
    def __init__(self, config: Config, folder: Path):
        validate_config(config)
        folder.mkdir(parents=True, exist_ok=True)
        self.cfg = config
        self.rng = random.Random(config.seed)
        self.folder = folder
        self.start = time.monotonic()
        self.trace = []
        self.counters = collections.Counter()
        self.durations = {}
        self.users = []
        self.all_songs = {}
        self.reference_checks = 0
        self.peak_queue = 0
        self.played_sequence = []
        self.max_wait_turns = 0
        self.max_wait_active_users = 0
        self.app = create_app(
            root=folder / "data",
            password="only-test",
            secret=b"isolated-stochastic-party-secret",
            auto_prepare=False,
            gpu_alignment=True,
        )
        self.app.state.events.create("Probabilistic Party", "765432")
        self.store = self.app.state.events.active()
        # A tiny synthetic media placeholder; no real encoder is used here.
        fixture = self.store.media / "fixture.mp4"
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_bytes(b"ftyp" + b"0" * 10240)

        async def b_lookup(bvid, page):
            duration = self.durations[bvid]
            return {
                "bvid": bvid,
                "page": page,
                "title": f"Karaoke {bvid}",
                "uploader": "Complex Songs",
                "duration": duration,
                "cover": "",
                "pages": [{"page": 1, "part": "Main"}],
            }

        async def n_resolve(ref):
            return int(ref)

        async def n_lookup(ref):
            duration = self.durations[str(ref)]
            return {
                "source": "netease",
                "source_id": str(ref),
                "title": f"Karaoke {ref}",
                "uploader": "Complex Songs",
                "duration": duration,
                "cover": "",
            }

        async def candidates(term):
            key = term.removeprefix("Karaoke ")
            duration = self.durations.get(key, 180)
            raws = self._make_raw(duration)
            return [
                c
                for raw in raws
                if (
                    c := _candidate(
                        term,
                        "网易云音乐",
                        raw,
                        "https://music.163.com/",
                        artist="Various",
                    )
                )
            ]

        async def n_exact(_id, *, title):
            return []

        self.app.state.lookup = b_lookup
        self.app.state.netease_resolve = n_resolve
        self.app.state.netease_lookup = n_lookup
        self.app.state.candidate_search = candidates
        self.app.state.netease_lyrics = n_exact
        with self.store.lock:
            self.store.db.execute(
                "INSERT INTO room_state(name,value) VALUES('playback_mode','browser')"
            )
            self.store.db.commit()
        self.port = self._port()
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=self.port,
                access_log=False,
                log_level="error",
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        for _ in range(120):
            try:
                if httpx.get(self.base + "/api/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.04)
        else:
            raise RuntimeError("isolated server not ready")
        self._closed = False
        self.admin = httpx.Client(base_url=self.base, timeout=15)
        assert (
            self.admin.post(
                "/api/admin/login", json={"password": "only-test"}
            ).status_code
            == 200
        )

    def _port(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def _make_raw(self, duration):
        count = self.rng.choice([2, 4, 8, 15, 25, 45])
        start = 0.8
        end = max(1.4, duration - 1)
        step = (end - start) / (count + 1)
        lines = [
            f"[{round((start + i * step) * 100) // 6000:02d}:{round((start + i * step) * 100) % 6000 // 100:02d}.{round((start + i * step) * 100) % 100:02d}]第{i + 1}句 歌词测试 {i + 100}"
            for i in range(count)
        ]
        raw = "\n".join(lines)
        # Include leading credits and global offset that must not silently retime words.
        if self.rng.random() < 0.34:
            raw = "[00:00.01]作词：一位作者\n" + raw
        if self.rng.random() < 0.17:
            raw = "[offset:+500]\n" + raw
        # Second deliberately malformed version: untimed/missing line or reverse time.
        bad = "\n".join(
            lines[: max(1, count // 2)]
            + ["这一行没有时间标签"]
            + lines[max(1, count // 2) :]
        )
        return [raw, bad]

    def log(self, op, **kw):
        self.trace.append({"step": len(self.trace), "op": op, **kw})
        if len(self.trace) > 2000:
            self.trace = self.trace[-2000:]
        self.counters[op] += 1

    def users_add(self):
        if len(self.users) >= self.cfg.users:
            return False
        number = len(self.users)
        style = self.rng.choices(
            ["standard", "burst", "careful", "late", "skip-heavy"],
            weights=[35, 25, 15, 10, 15],
        )[0]
        guest = Guest(
            f"Agent-{number:03d}",
            httpx.Client(base_url=self.base, timeout=15),
            style,
            len(self.trace),
        )
        assert (
            guest.client.post("/api/join", json={"code": "765432"}).status_code == 200
        )
        res = guest.client.post(
            "/api/users/register",
            json={
                "username": guest.name,
                "password": "test-password",
                "confirm_password": "test-password",
            },
        )
        assert res.status_code == 200, (res.status_code, res.text[:200])
        self.users.append(guest)
        self.log("join", user=guest.name, style=style)
        return True

    def row(self, id):
        with self.store.lock:
            r = self.store.db.execute(
                "SELECT * FROM songs WHERE id=?", (id,)
            ).fetchone()
            return dict(r) if r else None

    def rows(self):
        with self.store.lock:
            return [
                dict(x)
                for x in self.store.db.execute("SELECT * FROM songs ORDER BY id")
            ]

    def choose(self, filter_fn, bias=None):
        candidates = [
            x for x in self.all_songs.values() if filter_fn(x, self.row(x["id"]))
        ]
        if not candidates:
            return None
        weights = [bias.get(x["user"].style, 1) if bias else 1 for x in candidates]
        return self.rng.choices(candidates, weights=weights)[0]

    def order(self):
        if not self.users or len(self.all_songs) >= self.cfg.max_orders:
            return False
        u = self.rng.choices(
            self.users,
            weights=[
                {
                    "burst": 3,
                    "careful": 0.5,
                    "skip-heavy": 1.1,
                    "late": 1.2,
                    "standard": 1,
                }.get(x.style, 1)
                for x in self.users
            ],
        )[0]
        quantity = min(
            self.rng.choices(
                [1, 2, 3, 5],
                weights=[25, 20, 25, 30]
                if (u.style == "burst" or self.rng.random() < self.cfg.p_burst)
                else [68, 16, 9, 7],
            )[0],
            self.cfg.max_orders - len(self.all_songs),
        )
        for _ in range(quantity):
            number = len(self.all_songs) + 1
            kind = "netease" if self.rng.random() < self.cfg.p_netease else "bilibili"
            reference = (
                str(100000000 + number) if kind == "netease" else f"BV{number:010d}"
            )
            duration = self.rng.randint(self.cfg.duration_min, self.cfg.duration_max)
            self.durations[reference] = duration
            page = self.rng.randint(1, 3)
            if kind == "netease":
                res = u.client.post("/api/netease/songs", json={"ref": reference})
            else:
                res = u.client.post(
                    "/api/songs", json={"bvid": reference, "page": page}
                )
            if res.status_code == 409 and (
                "暂停" in res.text or "二十首" in res.text or "上限" in res.text
            ):
                self.log(
                    "blocked_order",
                    user=u.name,
                    reason="paused" if "暂停" in res.text else "user_limit",
                )
                break
            assert res.status_code == 200, (res.status_code, res.text[:200], u.name)
            song_id = res.json()["id"]
            self.all_songs[song_id] = {
                "id": song_id,
                "user": u,
                "kind": kind,
                "duration": duration,
            }
            self.log(
                "order",
                user=u.name,
                id=song_id,
                source=kind,
                duration=duration,
                page=page,
            )
        return True

    def candidate(self, chosen):
        u = chosen["user"]
        id = chosen["id"]
        result = u.client.get(
            f"/api/songs/{id}/lyrics/candidates", params={"phase": "all"}
        )
        assert result.status_code == 200, (result.status_code, result.text[:150])
        return result.json()["candidates"]

    def choose_lyrics(self, edit=False):
        chosen = self.choose(
            lambda x, r: (
                r
                and r["status"] in ("waiting", "ready")
                and r["prep_state"] not in ("aligning",)
                and not r["replay_of"]
            ),
            bias={
                "careful": 3,
                "burst": 0.6,
                "standard": 1,
                "late": 1.0,
                "skip-heavy": 0.6,
            },
        )
        if not chosen:
            return False
        id = chosen["id"]
        u = chosen["user"]
        r = self.row(id)
        cands = self.candidate(chosen)
        assert len(cands) >= 2
        good = cands[0]
        bad = cands[1]
        if edit and self.rng.random() < 0.35:
            res = u.client.post(
                f"/api/songs/{id}/lyrics/select",
                json={
                    "text": good["text"],
                    "ticket": "invalid-test-ticket",
                    "source": good["source"],
                },
            )
            assert res.status_code == 403, (res.status_code, res.text[:150])
            assert self.row(id)["lyric_choice"] == r["lyric_choice"]
            self.log("rejected_forged_lyrics", id=id)
            return True
        action = self.rng.random()
        # Deliberately invalid source: it contains an untimed sung line.
        if action < 0.07:
            assert "timed_lrc" not in bad, bad
            self.log("untimed_candidate_rejected_as_provider", id=id)
            return True
        provider = action < 0.77
        offset = int(
            self.rng.choice(
                [-12000, -5000, -2500, -1000, -300, 0, 300, 1000, 2500, 5000, 12000]
            )
        )
        invalid = provider and self.rng.random() < self.cfg.p_bad_timing
        if invalid:
            offset = (
                -(r["duration"] * 1000 + 1500)
                if self.rng.random() < 0.5
                else r["duration"] * 1000 + 1500
            )
        payload = {
            "text": good["text"],
            "ticket": good["ticket"],
            "source": good["source"],
            "timing_mode": "provider" if provider else "align",
        }
        line_review = (
            provider
            and bool(good.get("unfiltered_ticket"))
            and self.rng.random() < self.cfg.p_line_review
        )
        if provider:
            assert good.get("timed_lrc") and good.get("timing_ticket")
            payload.update(
                timed_lrc=good["timed_lrc"],
                source_lrc_raw=good["source_lrc_raw"],
                timing_ticket=good["timing_ticket"],
                offset_ms=offset,
            )
            if line_review:
                all_lines = good["unfiltered_text"].splitlines()
                # Existing selection flow only allows whole source line indexes.
                chosen_indices = (
                    list(range(1, len(all_lines)))
                    if all_lines[0].startswith("作词")
                    else list(range(len(all_lines)))
                )
                if len(chosen_indices) >= 2:
                    payload.update(
                        selection_text=good["unfiltered_text"],
                        selection_ticket=good["unfiltered_ticket"],
                        selected_lines=chosen_indices,
                    )
                else:
                    line_review = False
        res = u.client.post(f"/api/songs/{id}/lyrics/select", json=payload)
        if provider:
            try:
                expected = offset_timeline(good["timed_lrc"], offset, r["duration"])
                if line_review:
                    from vc_ktv.lyrics.timed_lyrics import project_provider_timeline

                    expected = offset_timeline(
                        project_provider_timeline(
                            good["source_lrc_raw"],
                            good["unfiltered_text"],
                            chosen_indices,
                        ),
                        offset,
                        r["duration"],
                    )
                is_valid = True
            except ValueError:
                is_valid = False
            assert res.status_code == (200 if is_valid else 400), (
                id,
                offset,
                is_valid,
                res.status_code,
                res.text[:155],
            )
            after = self.row(id)
            if is_valid:
                assert (after["lyric_timing_mode"], after["lyric_offset_ms"]) == (
                    "provider",
                    offset,
                )
                assert (
                    offset_timeline(
                        after["provider_lrc"],
                        after["lyric_offset_ms"],
                        after["duration"],
                    )
                    == expected
                )
                self.log(
                    "selected_provider_timing",
                    id=id,
                    offset=offset,
                    line_review=line_review,
                )
            else:
                assert (
                    after["lyric_choice"] == r["lyric_choice"]
                    and after["lyric_offset_ms"] == r["lyric_offset_ms"]
                )
                self.log("rejected_timing_outside_media", id=id, offset=offset)
        else:
            assert res.status_code == 200, (id, res.status_code, res.text[:155])
            after = self.row(id)
            assert after["lyric_timing_mode"] == "align" and not after["provider_lrc"]
            self.log("selected_gpu_alignment", id=id)
        return True

    def skip_lyrics(self):
        chosen = self.choose(
            lambda x, r: (
                r
                and r["status"] in ("waiting", "ready")
                and r["prep_state"] != "aligning"
                and not r["replay_of"]
            ),
            bias={
                "skip-heavy": 3,
                "careful": 0.35,
                "standard": 1,
                "burst": 1,
                "late": 1,
            },
        )
        if not chosen:
            return False
        id = chosen["id"]
        r = self.row(id)
        res = chosen["user"].client.post(f"/api/songs/{id}/lyrics/skip")
        assert res.status_code == 200, (id, res.status_code, res.text[:155])
        after = self.row(id)
        assert after["skip_subtitles"] == 1 and after["lyric_offset_ms"] == 0
        assert after["provider_lrc"] == "" and after["lyric_choice"] == ""
        self.log(
            "skip_lyrics", id=id, was_provider=(r["lyric_timing_mode"] == "provider")
        )
        return True

    def cancel(self):
        chosen = self.choose(
            lambda x, r: r and r["status"] in ("waiting", "ready", "rendering"),
            bias={
                "burst": 2.5,
                "careful": 0.6,
                "standard": 1,
                "late": 1,
                "skip-heavy": 1.4,
            },
        )
        if not chosen:
            return False
        id = chosen["id"]
        r = self.row(id)
        before = self.store.rows(
            "SELECT name,plays,fair_offset FROM guests ORDER BY name"
        )
        # Cross-user cancellation must always be rejected without state corruption.
        if self.rng.random() < 0.17 and len(self.users) > 1:
            other = self.rng.choice([u for u in self.users if u != chosen["user"]])
            bad = other.client.post(f"/api/songs/{id}/cancel")
            assert bad.status_code in (403, 404), (id, bad.status_code, bad.text[:200])
            assert self.row(id)["status"] == r["status"]
            self.log("cross_user_cancel_denied", id=id)
        res = chosen["user"].client.post(f"/api/songs/{id}/cancel")
        assert res.status_code == 200, (res.status_code, res.text[:120])
        assert self.row(id)["status"] == "cancelled"
        assert before == self.store.rows(
            "SELECT name,plays,fair_offset FROM guests ORDER BY name"
        )
        self.log(
            "cancel",
            id=id,
            was_pinned=bool(r["priority_next"]),
            was_ready=r["status"] == "ready",
        )
        return True

    def worker_complete(self):
        chosen = self.choose(
            lambda x, r: (
                r
                and r["status"] in ("waiting", "ready")
                and r["prep_state"] in ("pending", "downloaded", "original")
            )
        )
        if not chosen:
            return False
        id = chosen["id"]
        r = self.row(id)
        # Simulate a worker publishing a complete file; the actual download/FFmpeg
        # path has separate end-to-end coverage and is intentionally not run here.
        with self.store.lock:
            if r["skip_subtitles"]:
                state = "original"
            else:
                state = "done" if r["lyric_choice"] else "original"
            self.store.db.execute(
                "UPDATE songs SET status='ready',prep_state=?,prep_stage='done',"
                "source_file='fixture.mp4',media_file='fixture.mp4' WHERE id=?",
                (state, id),
            )
            self.store.db.commit()
        assert self.row(id)["status"] == "ready"
        self.log("worker_complete", id=id, state=state)
        return True

    def pin(self):
        chosen = self.choose(
            lambda x, r: (
                r
                and r["status"] == "ready"
                and r["prep_state"] in ("original", "done", "manual")
                and not r["priority_next"]
            )
        )
        if not chosen:
            return False
        res = self.admin.post(f"/api/admin/songs/{chosen['id']}/pin-next")
        if res.status_code != 200:
            assert res.status_code == 409, (res.status_code, res.text[:120])
            self.log("pin_conflict", id=chosen["id"])
            return True
        with self.store.lock:
            pinned = [
                dict(x)
                for x in self.store.db.execute(
                    "SELECT * FROM songs WHERE priority_next=1"
                )
            ]
        assert len(pinned) == 1 and pinned[0]["id"] == chosen["id"], pinned
        self.log("pin", id=chosen["id"])
        return True

    def playback(self):
        active = self.store.current()
        if active:
            sid = active["id"]
            skip = self.rng.random() < self.cfg.p_skip_playing
            before = self.store.rows(
                "SELECT id,plays,fair_offset FROM guests ORDER BY id"
            )
            res = self.admin.post(
                "/api/admin/finish",
                json={"song_id": sid, "reason": "skip" if skip else "completed"},
            )
            assert res.status_code == 200, (sid, res.status_code, res.text[:200])
            settled = self.row(sid)
            assert settled["status"] == ("skipped" if skip else "played")
            after = self.store.rows(
                "SELECT id,plays,fair_offset FROM guests ORDER BY id"
            )
            b = {x["id"]: x for x in before}
            a = {x["id"]: x for x in after}
            req = settled["requester"]
            expected_plays = int(not skip and not settled["fair_exempt"])
            expected_offset = int(skip and not settled["fair_exempt"])
            assert a[req]["plays"] - b[req]["plays"] == expected_plays, (
                sid,
                "play-count",
                skip,
                b[req],
                a[req],
            )
            assert a[req]["fair_offset"] - b[req]["fair_offset"] == expected_offset, (
                sid,
                "offset",
                skip,
                b[req],
                a[req],
            )
            for user in b:
                if user != req:
                    assert (a[user]["plays"], a[user]["fair_offset"]) == (
                        b[user]["plays"],
                        b[user]["fair_offset"],
                    )
            self.played_sequence.append((req, sid, skip, bool(settled["fair_exempt"])))
            self.log(
                "finish", id=sid, skip=skip, pin_exempt=bool(settled["fair_exempt"])
            )
            return True
        preview = self.store.fair_queue()
        if not preview:
            return False
        expected = preview[0]["id"]
        original_rows = self.store.rows(
            "SELECT id,plays,fair_offset FROM guests ORDER BY id"
        )
        res = self.admin.post("/api/admin/next")
        assert res.status_code == 200, (res.status_code, res.text[:200])
        assert res.json().get("active") is not None, (expected, res.text[:200])
        actual = res.json()["active"]["id"]
        assert actual == expected, ("wrong claim", expected, actual)
        current = self.store.current()
        assert current and current["id"] == expected
        assert current["fair_exempt"] == int(bool(preview[0]["priority_next"]))
        after_rows = self.store.rows(
            "SELECT id,plays,fair_offset FROM guests ORDER BY id"
        )
        assert [x["plays"] for x in original_rows] == [x["plays"] for x in after_rows]
        if current["fair_exempt"]:
            assert [x["fair_offset"] for x in original_rows] == [
                x["fair_offset"] for x in after_rows
            ], ("pin changed fair offset on claim", expected)
        self.log(
            "claim",
            id=expected,
            user=current["requester"],
            pin_exempt=bool(current["fair_exempt"]),
        )
        return True

    def replay(self):
        candidates = [
            x
            for x in self.rows()
            if x["status"] == "played" and self.store.replay_assets(x).get("media_file")
        ]
        if not candidates or len(self.all_songs) >= self.cfg.max_orders + 100:
            return False
        old = self.rng.choice(candidates)
        res = self.admin.post(f"/api/admin/songs/{old['id']}/replay")
        assert res.status_code == 200, (old["id"], res.status_code, res.text[:150])
        # Replay copies source metadata and must carry an identity separate from the old song.
        new_id = res.json().get("id")
        assert new_id and new_id != old["id"], res.json()
        after = self.row(new_id)
        assert (
            after
            and after["replay_of"] == old["id"]
            and after["requester"] == old["requester"]
        )
        self.all_songs[new_id] = {
            "id": new_id,
            "user": next(
                u
                for u in self.users
                if u.name
                == self.store.db.execute(
                    "SELECT name FROM guests WHERE id=?", (old["requester"],)
                ).fetchone()[0]
            ),
            "kind": after["source_type"],
            "duration": after["duration"],
        }
        self.log("replay", old=old["id"], id=new_id)
        return True

    def switch_policy(self):
        old = self.store.settings()["queue_policy"]
        mode = "fcfs" if old == "fair" else "fair"
        res = self.admin.put("/api/admin/settings", json={"queue_policy": mode})
        assert res.status_code == 200, (res.status_code, res.text[:140])
        assert self.store.settings()["queue_policy"] == mode
        self.log("switch_policy", value=mode)
        return True

    def toggle_orders(self):
        old = self.store.settings()["orders_paused"]
        res = self.admin.put("/api/admin/settings", json={"orders_paused": not old})
        assert res.status_code == 200, (res.status_code, res.text[:130])
        self.log("toggle_orders", paused=not old)
        return True

    def invariant(self):
        with self.store.lock:
            rows = [
                dict(x)
                for x in self.store.db.execute(
                    "SELECT * FROM songs ORDER BY requested_at,id"
                )
            ]
            users = self.store.rows("SELECT * FROM guests")
            preview = self.store.fair_queue()
            settings = self.store.settings()
            current = self.store.current()
            history = self.store.db.execute(
                "SELECT value FROM room_state WHERE name='last_requester'"
            ).fetchone()
            previous = history[0] if history else ""
            expected = reference_queue(
                self.store.songs(), settings, users, current, previous
            )
            ids = [x["id"] for x in preview]
            assert ids == [x["id"] for x in expected], (
                "fair oracle mismatch",
                ids[:30],
                [x["id"] for x in expected][:30],
            )
            self.reference_checks += 1
            assert len(ids) == len(set(ids))
            assert len([x for x in rows if x["status"] == "playing"]) <= 1
            assert set(ids).isdisjoint(
                x["id"]
                for x in rows
                if x["status"] in ("played", "cancelled", "skipped")
            )
            assert all(x["plays"] >= 0 and x["fair_offset"] >= 0 for x in users)
            real_plays = collections.Counter(
                x["requester"]
                for x in rows
                if x["status"] == "played" and not x["fair_exempt"]
            )
            for user in users:
                assert user["plays"] == real_plays[user["id"]], (
                    "real plays mismatch",
                    user,
                    real_plays[user["id"]],
                )
            for x in rows:
                if x["lyric_timing_mode"] == "provider" and x["provider_lrc"]:
                    assert offset_timeline(
                        x["provider_lrc"], x["lyric_offset_ms"], x["duration"]
                    )
                if x["skip_subtitles"]:
                    assert (
                        x["lyric_choice"] == ""
                        and x["provider_lrc"] == ""
                        and x["lyric_offset_ms"] == 0
                    )
            if settings["queue_policy"] == "fair" and ids:
                # With the two-turn catch-up bound, every singer with an ordinary ready
                # song must appear promptly in the hypothetical fully prepared queue.
                ordinary = [x for x in expected if not x["priority_next"]]
                distinct = {x["requester_id"] for x in ordinary}
                for user in distinct:
                    first = next(
                        i for i, x in enumerate(ordinary) if x["requester_id"] == user
                    )
                    assert first <= 3 * len(distinct) + 3, (
                        "queue starvation under bounded catch-up",
                        user,
                        first,
                        len(distinct),
                    )
                    if first > self.max_wait_turns:
                        self.max_wait_turns = first
                        self.max_wait_active_users = len(distinct)
            self.peak_queue = max(self.peak_queue, len(ids))
        if self.rng.random() < 0.13:
            state = self.admin.get("/api/admin/state")
            assert state.status_code == 200, (state.status_code, state.text[:130])
            # No concurrent mutator in this deterministic scenario.
            state_ids = [row["id"] for row in state.json()["queue"]]
            assert ids == state_ids, (
                "admin queue differs from scheduler",
                ids[:20],
                state_ids[:20],
            )

    def run(self):
        try:
            for _ in range(min(4, self.cfg.users)):
                self.users_add()
            actions = {
                "join": self.users_add,
                "order": self.order,
                "lyrics": self.choose_lyrics,
                "skip_lyrics": self.skip_lyrics,
                "edit_lyrics": lambda: self.choose_lyrics(edit=True),
                "cancel": self.cancel,
                "worker_complete": self.worker_complete,
                "playback": self.playback,
                "pin": self.pin,
                "replay": self.replay,
                "switch_policy": self.switch_policy,
                "toggle_orders": self.toggle_orders,
            }
            names = list(actions)
            probs = [getattr(self.cfg, "p_" + name) for name in names]
            for step in range(self.cfg.steps):
                action = self.rng.choices(names, weights=probs)[0]
                actions[action]()
                if step % self.cfg.check_every == 0:
                    self.invariant()
                if step % 200 == 0:
                    print(
                        "PROGRESS",
                        self.cfg.seed,
                        step,
                        "users",
                        len(self.users),
                        "songs",
                        len(self.all_songs),
                        "queue",
                        len(self.store.fair_queue()),
                        "plays",
                        len(self.played_sequence),
                        flush=True,
                    )
            self.invariant()
            summary = {
                "seed": self.cfg.seed,
                "config": vars(self.cfg),
                "users": len(self.users),
                "orders": len(self.all_songs),
                "processed_actions": sum(self.counters.values()),
                "actions": dict(self.counters),
                "fair_reference_checks": self.reference_checks,
                "peak_queue": self.peak_queue,
                "worst_first_turn_wait": self.max_wait_turns,
                "active_users_at_worst_wait": self.max_wait_active_users,
                "played_or_skipped_turns": len(self.played_sequence),
                "wall_seconds": round(time.monotonic() - self.start, 2),
            }
            (self.folder / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
            )
            print("SUMMARY", json.dumps(summary, ensure_ascii=False), flush=True)
            return summary
        except Exception:
            fail = {
                "seed": self.cfg.seed,
                "error": traceback.format_exc(),
                "recent_trace": self.trace[-70:],
                "actions": dict(self.counters),
                "song_count": len(self.all_songs),
            }
            (self.folder / "failure.json").write_text(
                json.dumps(fail, ensure_ascii=False, indent=2) + "\n"
            )
            print("FAILURE", json.dumps(fail, ensure_ascii=False), flush=True)
            raise
        finally:
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.server.should_exit = True
        self.thread.join(timeout=8)
        self.admin.close()
        for user in self.users:
            user.client.close()
        self.store.db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.close()


def main():
    parser = argparse.ArgumentParser(
        description="Isolated probabilistic multi-user queue and lyric benchmark"
    )
    for key, val in vars(Config()).items():
        parser.add_argument("--" + key.replace("_", "-"), type=type(val), default=val)
    parser.add_argument("--output", default="benchmarks/.results")
    arguments = vars(parser.parse_args())
    output = Path(arguments.pop("output"))
    output.mkdir(parents=True, exist_ok=True)
    config = Config(**{name: arguments[name] for name in vars(Config())})
    try:
        validate_config(config)
    except ValueError as exc:
        parser.error(str(exc))
    with tempfile.TemporaryDirectory(prefix="vcktv-prob-party-") as tmp:
        sim = Simulation(config, Path(tmp))
        try:
            result = sim.run()
            (output / f"seed-{config.seed}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            )
        except Exception:
            if (Path(tmp) / "failure.json").exists():
                (output / f"failed-seed-{config.seed}.json").write_text(
                    (Path(tmp) / "failure.json").read_text()
                )
            raise


if __name__ == "__main__":
    main()
