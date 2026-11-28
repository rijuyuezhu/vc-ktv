"""Probabilistic multi-user preparation benchmark with real claims and synthetic media.

No GPU, network downloads, FFmpeg or production service. The real scheduler,
publication, refresh and accompaniment cache logic run against tiny fixtures.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from unittest.mock import patch

from benchmarks.party import Config, Simulation, run_cli
from benchmarks.scheduler_reference import reference_queue
from vc_ktv.core.artifacts import source_record
from vc_ktv.processing import refresh
from vc_ktv.processing.backing_audio import ready_audio_file, source_identity
from vc_ktv.processing.publication import publish_original, publish_subtitles
from vc_ktv.processing.task_state import claim_next_locked


@dataclass
class PreparationConfig(Config):
    p_worker_complete: float = 0.45
    p_personal_order: float = 0.05
    p_backing: float = 0.20
    p_settings: float = 0.035
    p_refresh: float = 0.06
    p_backing_gate: float = 0.035
    web_readers: int = 4
    web_every: int = 2


class SimulatedBackingThread:
    """Advertise the manually stepped backing lane to the admin API."""

    def is_alive(self):
        return True


class PreparationSimulation(Simulation):
    def __init__(self, config, folder):
        if not 0 <= config.web_readers <= 64 or config.web_every < 1:
            raise ValueError("web_readers must be 0–64 and web_every positive")
        super().__init__(config, folder)
        self.worker = self.app.state.autoprep
        self.worker.backing.thread = SimulatedBackingThread()
        self.worker.backing.producer = self._produce_backing
        self.worker.enhance = self._render
        self._properties = {}
        self._sequence = 0
        self._steps = 0
        self._web_ms = []
        self._action_ms = []
        self._web_pool = (
            ThreadPoolExecutor(max_workers=config.web_readers)
            if config.web_readers
            else None
        )

    def _read_web(self, client, path):
        start = time.perf_counter()
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)
        state = response.json()
        assert isinstance(state["queue"], list)
        return (time.perf_counter() - start) * 1000

    def run_action(self, action):
        self._steps += 1
        futures = []
        if self._web_pool and self._steps % self.cfg.web_every == 0:
            for i in range(self.cfg.web_readers):
                client = self.users[i % len(self.users)].client if i % 2 else self.admin
                path = "/api/state" if i % 2 else "/api/admin/state"
                futures.append(self._web_pool.submit(self._read_web, client, path))
        start = time.perf_counter()
        try:
            action()
        finally:
            self._action_ms.append((time.perf_counter() - start) * 1000)
            self._web_ms.extend(f.result() for f in futures)

    @staticmethod
    def _latencies(values):
        ordered = sorted(values)
        if not ordered:
            return {}
        return {
            "p50": round(ordered[(len(ordered) - 1) // 2], 2),
            "p95": round(ordered[int((len(ordered) - 1) * 0.95)], 2),
            "max": round(ordered[-1], 2),
        }

    def extra_summary(self):
        return {
            "web_reads": len(self._web_ms),
            "web_latency_ms": self._latencies(self._web_ms),
            "action_latency_ms": self._latencies(self._action_ms),
        }

    def close(self):
        if self._web_pool:
            self._web_pool.shutdown()
        super().close()

    def actions(self):
        return {
            **super().actions(),
            "personal_order": self.personal_order,
            "backing": self.backing,
            "settings": self.change_settings,
            "refresh": self.request_refresh,
            "backing_gate": self.backing_gate,
        }

    def _new_name(self, folder, stem):
        self._sequence += 1
        return folder / f"{stem}-{self._sequence}.mp4"

    def _metadata(self, row, settings):
        mode = settings["original_codec_mode"]
        return {
            "codec": "h264" if mode == "default" else mode,
            "height": settings["video_quality"]
            if row["source_type"] == "bilibili"
            else 720,
            "duration": float(row["duration"]),
            "video_start": 0.0,
            "average_mbps": settings["max_bandwidth_mbps"] * 0.45,
            "peak_1s_mbps": settings["max_bandwidth_mbps"] * 1.2,
            "peak_5s_mbps": settings["max_bandwidth_mbps"] * 0.9,
        }

    def _produce_backing(self, source, target, staging, **kwargs):
        target.write_bytes(b"synthetic accompaniment" * 100)

    def _render(self, source, lyrics, target, archive, duration, **kwargs):
        target.write_bytes(b"synthetic subtitle render" * 100)
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "aligned.lrc").write_text("[00:00.10]测试歌词\n")
        limit = kwargs.get(
            "max_bandwidth_mbps", self.store.settings()["max_bandwidth_mbps"]
        )
        self._properties[target.name] = {
            **self.store.artifact(source.name, "source"),
            "codec": "h264",
            "average_mbps": limit * 0.45,
            "peak_1s_mbps": limit * 1.2,
            "peak_5s_mbps": limit * 0.9,
        }
        return {}

    def _render_provider(
        self, source, timed, offset, target, archive, duration, **kwargs
    ):
        result = self._render(source, timed, target, archive, duration, **kwargs)
        from vc_ktv.lyrics.timed_lyrics import offset_timeline

        (archive / "aligned.lrc").write_text(offset_timeline(timed, offset, duration))
        return result

    def _variant(self, owner, row, old, old_meta, settings, staging):
        target = self._new_name(staging, f"{row['id']}-variant")
        target.write_bytes(old.read_bytes())
        metadata = source_record(
            row,
            settings,
            self._metadata(row, settings),
            origin=row["source_type"],
            lineage=old_meta["lineage"],
        )
        return target, metadata, True

    def _tv_cache(self, source, archive, duration, limit, **kwargs):
        target = archive / "tv-source.mp4"
        target.write_bytes(b"synthetic TV cache" * 100)
        self._properties[target.name] = {
            **(kwargs.get("profile") or {}),
            "codec": "h264",
            "average_mbps": limit * 0.4,
            "peak_1s_mbps": limit,
            "peak_5s_mbps": limit * 0.8,
        }

    def _refresh(self, row):
        settings = row["refresh_settings"]
        before = self.store.replay_assets(row)
        before_backing = self.store.backing_asset(row)[0]

        # Exercise actual refresh publication, provenance, reuse and cleanup,
        # replacing only media computation with deterministic tiny files.
        def inspect(path):
            return self._properties.get(path.name, self._metadata(row, settings))

        with (
            patch.object(refresh, "_new_source", self._variant),
            patch.object(refresh, "same_soundtrack", return_value=True),
            patch.object(refresh, "render_provider_video", self._render_provider),
            patch.object(refresh, "prepare_tv_source", self._tv_cache),
            patch.object(refresh, "inspect_media", inspect),
            patch.object(
                refresh, "known_media", lambda path, meta: {**inspect(path), **meta}
            ),
        ):
            refresh.execute_refresh(self.worker, row)
        after = self.row(row["id"])
        assert after["refresh_state"] == "", after
        assert not self.store.refresh_reasons(after, settings), after
        if before_backing == "ready":
            assert self.store.backing_ready(after)
        self.log(
            "refresh_complete",
            id=row["id"],
            source_reused=before["source_file"] == after["source_file"],
        )
        if before_backing == "ready":
            self.counters["refresh_backing_reused"] += 1

    def _oracle(self, projected=False):
        settings = self.store.settings()
        users = self.store.rows("SELECT * FROM guests")
        previous = self.store.db.execute(
            "SELECT value FROM room_state WHERE name='last_requester'"
        ).fetchone()
        pool = self.store.songs()
        if projected:
            pool = [
                {**s, "status": "ready", "has_media": 1, "prep_state": "done"}
                for s in pool
                if s["status"] in ("ready", "waiting", "rendering")
            ]
        else:
            pool = [s for s in pool if self.store.can_play(s, settings)]
        return reference_queue(
            pool, settings, users, self.store.current(), previous[0] if previous else ""
        )

    def _eligible(self, row, lane, now):
        if lane == "fast" and row["lyric_timing_mode"] != "provider":
            return False
        if row["status"] == "waiting":
            return row["prep_state"] == "pending" or (
                row["prep_state"] == "unavailable"
                and row["prep_attempts"] < 3
                and row["prep_retry_at"] <= now
            )
        if row["status"] != "ready":
            return False
        if row["refresh_state"] == "queued":
            return (
                lane == "general"
                and row["prep_state"] not in ("aligning", "downloading")
                and (
                    bool(self.store.refresh_reasons(row))
                    or (
                        row["prep_state"] == "downloaded"
                        and row["lyric_choice"]
                        and not row["skip_subtitles"]
                    )
                )
            )
        return (
            row["prep_state"] == "downloaded"
            and bool(row["lyric_choice"])
            and not row["skip_subtitles"]
            and row["refresh_state"] != "working"
        )

    def worker_complete(self, lane=None):
        lane = lane or self.rng.choice(["general", "general", "fast"])
        with self.store.lock:
            now = int(time.time())
            order = self._oracle(projected=True)
            expected = next(
                (
                    s["id"]
                    for s in order
                    if self._eligible(self.row(s["id"]), lane, now)
                ),
                None,
            )
            counters = self.store.rows(
                "SELECT id,plays,fair_offset FROM guests ORDER BY id"
            )
            row = claim_next_locked(self.store, lane, True)
            assert (row["id"] if row else None) == expected, (lane, expected, row)
            assert (
                self.store.rows("SELECT id,plays,fair_offset FROM guests ORDER BY id")
                == counters
            )
            self.counters["preparation_claim_checks"] += 1
        if row is None:
            return False
        sid = row["id"]
        if row.get("refresh_task"):
            self._refresh(row)
        elif row["status"] == "waiting":
            settings = self.store.settings()
            fixture = self.store.media / "fixture.mp4"
            meta = source_record(
                row, settings, self._metadata(row, settings), origin=row["source_type"]
            )
            assert publish_original(
                self.store,
                sid,
                fixture,
                row["duration"],
                time.monotonic(),
                lambda: None,
                meta,
            )
            self.log("download_complete", id=sid, lane=lane)
        else:
            archive = self.store.media / f"{sid}-synthetic"
            target = self._new_name(self.store.media, f"{sid}-render")
            if row["lyric_timing_mode"] == "provider":
                self._render_provider(
                    self.store.media / row["source_file"],
                    row["provider_lrc"],
                    row["lyric_offset_ms"],
                    target,
                    archive,
                    row["duration"],
                )
            else:
                self._render(
                    self.store.media / row["source_file"],
                    row["lyric_choice"],
                    target,
                    archive,
                    row["duration"],
                )
            metadata = {
                **self._properties[target.name],
                "subtitle_style": row["style_preset"],
                "bandwidth": row["max_bandwidth_mbps"],
            }
            assert publish_subtitles(
                self.store, row, target, archive / "aligned.lrc", metadata
            )
            self.log(
                "subtitle_complete", id=sid, lane=lane, timing=row["lyric_timing_mode"]
            )
        self.counters[f"{lane}_claims"] += 1
        return True

    def backing(self):
        worker = self.worker.backing
        with self.store.lock:
            order = [s["id"] for s in self._oracle(projected=True)]
            current = self.store.current()
            if current:
                order.append(current["id"])
            expected = None
            for sid in order:
                row = self.row(sid)
                if row["status"] not in (
                    "ready",
                    "playing",
                ) or self.store.refresh_blocked(row, self.store.settings()):
                    continue
                name = self.store.replay_assets(row).get("source_file")
                source = self.store.media / (name or "")
                if not source.is_file():
                    continue
                key = source_identity(source, worker.model_sha256)
                cached = self.store.db.execute(
                    "SELECT status FROM backing_tracks WHERE cache_key=?", (key,)
                ).fetchone()
                if cached and (
                    cached["status"] in ("failed", "processing")
                    or (
                        cached["status"] == "ready"
                        and ready_audio_file(self.store.root, key)
                    )
                ):
                    continue
                expected = sid
                break
            selected = worker._candidate()
            assert (selected[0] if selected else None) == expected
            self.counters["backing_claim_checks"] += 1
        if selected is None:
            return False
        sid, source, key = selected
        worker._process(sid, source, key)
        assert self.store.backing_ready(self.row(sid))
        self.log("backing_complete", id=sid)
        return True

    def personal_order(self):
        ids = {s["id"] for s in self.store.fair_queue()}
        chosen = self.choose(lambda x, r: x["id"] in ids)
        if not chosen:
            return False
        sid = chosen["id"]
        response = chosen["user"].client.post(f"/api/songs/{sid}/move-first")
        assert response.status_code == 200, response.text
        self.log("personal_order", id=sid)
        return True

    def change_settings(self):
        field, choices = self.rng.choice(
            [
                ("original_codec_mode", ["h264", "av1", "default"]),
                ("video_quality", [480, 720, 1080]),
                ("max_bandwidth_mbps", [2, 3, 5]),
                ("subtitle_style", ["classic", "compact", "large"]),
            ]
        )
        old = self.store.settings()[field]
        value = self.rng.choice([v for v in choices if v != old])
        response = self.admin.put("/api/admin/settings", json={field: value})
        assert response.status_code == 200, response.text
        self.log("settings", field=field, value=value)
        return True

    def request_refresh(self):
        response = self.admin.post("/api/admin/songs/refresh-outdated")
        assert response.status_code == 200, response.text
        count = response.json()["count"]
        self.log("refresh_requested", count=count)
        return True

    def backing_gate(self):
        value = not self.store.settings()["require_backing_ready"]
        response = self.admin.put(
            "/api/admin/settings", json={"require_backing_ready": value}
        )
        assert response.status_code == 200, response.text
        assert self.store.settings()["require_backing_ready"] == value
        self.log("backing_gate", required=value)
        return True

    def invariant(self):
        super().invariant()
        with self.store.lock:
            assert [s["id"] for s in self.store.projected_queue()] == [
                s["id"] for s in self._oracle(projected=True)
            ]
            self.counters["projected_reference_checks"] += 1


def main():
    # The API needs the feature enabled, but no real worker thread is started.
    with patch.dict(
        os.environ, {"VC_KTV_BACKING_TRACKS": "1", "CUDA_VISIBLE_DEVICES": ""}
    ):
        run_cli(
            PreparationConfig(),
            PreparationSimulation,
            prefix="-preparation",
            description=__doc__,
        )


if __name__ == "__main__":
    main()
