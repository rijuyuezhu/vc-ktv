"""Optional, low-priority backing audio generator and event-scoped asset cache."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from vc_ktv.infra.process_cancel import terminate_thread_children

log = logging.getLogger(__name__)

KARAOKE_SHA256 = "1de20d459332fe8869aeb01327a31df0032262706e1365114e852dc271779813"
KARAOKE_FILENAME = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"
BACKING_ALGORITHM = "direct-melband-v1"


def source_identity(source: Path, model_sha256: str) -> str:
    """Same physical source and model share one asset across virtual replays."""
    stat = source.stat()
    identity = f"{BACKING_ALGORITHM}\0{source.name}\0{stat.st_size}\0{stat.st_mtime_ns}\0{model_sha256}"
    return hashlib.sha256(identity.encode()).hexdigest()


def run_direct(
    source: Path,
    target: Path,
    staging: Path,
    *,
    checkpoint: Path,
    model_source: Path,
    python: Path,
    cancelled: threading.Event | None = None,
) -> None:
    """Generate one AAC file, keeping all large intermediate audio in staging."""
    mixture = staging / "mixture.wav"
    residual = staging / "residual.wav"
    if cancelled and cancelled.is_set():
        return
    subprocess.run(
        [
            os.getenv("KTV_FFMPEG", "ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            "asetpts=PTS-STARTPTS",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_f32le",
            str(mixture),
        ],
        check=True,
    )
    if cancelled and cancelled.is_set():
        return
    request = staging / "request.json"
    request.write_text(
        json.dumps(
            {
                "mixture": str(mixture),
                "output": str(residual),
                "checkpoint": str(checkpoint),
                "model_source": str(model_source),
            }
        ),
        encoding="utf-8",
    )
    with (staging / "inference.log").open("wb") as log_file:
        subprocess.run(
            [
                str(python),
                str(Path(__file__).with_name("backing_audio_worker.py")),
                str(request),
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            check=True,
        )
    if cancelled and cancelled.is_set():
        return
    subprocess.run(
        [
            os.getenv("KTV_FFMPEG", "ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(residual),
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(target),
        ],
        check=True,
    )
    if target.stat().st_size < 1024:
        raise ValueError("Empty backing audio result")


class BackingTracks:
    """Single-flight, event-scoped, optional task below normal AutoPrep claims."""

    def __init__(
        self,
        owner,
        *,
        enabled: bool | None = None,
        checkpoint: Path | None = None,
        model_sha256: str = KARAOKE_SHA256,
        producer: Callable[..., None] = run_direct,
    ):
        self.owner = owner
        self.enabled = (
            os.getenv("VC_KTV_BACKING_TRACKS", "0") == "1"
            if enabled is None
            else enabled
        )
        root = Path(
            os.getenv("VC_KTV_PIPELINE_ROOT", "~/.local/share/vc-ktv/pipeline")
        ).expanduser()
        self.checkpoint = (
            checkpoint
            or Path(
                os.getenv(
                    "VC_KTV_BACKING_CHECKPOINT",
                    str(root / "models/separation/Karaoke" / KARAOKE_FILENAME),
                )
            ).expanduser()
        )
        self.model_source = root / "models/separation/sources/Kim"
        self.python = root / "runtime/kim/bin/python"
        self.model_sha256 = model_sha256
        self.producer = producer
        self.unavailable = False
        self.thread: threading.Thread | None = None
        self.cancelled = threading.Event()
        self.active_song_id: int | None = None
        self.active_tid: int | None = None
        self.lock = threading.RLock()

    @property
    def store(self):
        return self.owner.store

    def start(self) -> None:
        if not self.enabled or (self.thread and self.thread.is_alive()):
            return
        if not all(
            path.is_file()
            for path in (
                self.checkpoint,
                self.model_source / "utils.py",
                self.python,
            )
        ):
            self.unavailable = True
            log.warning(
                "Backing audio disabled: optional model/runtime is not installed"
            )
            return
        with self.checkpoint.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != self.model_sha256:
                self.unavailable = True
                log.error("Backing audio disabled: unverified model checkpoint")
                return
        self.unavailable = False
        with self.store.lock:
            self.store.db.execute(
                "UPDATE backing_tracks SET status='pending' WHERE status='processing'"
            )
            self.store.db.commit()
        self.thread = threading.Thread(
            target=self.loop, daemon=True, name="vc-ktv-backing"
        )
        self.thread.start()

    def cancel_current(self, song_id: int | None = None) -> None:
        with self.lock:
            if self.active_tid is None or (
                song_id is not None and song_id != self.active_song_id
            ):
                return
            self.cancelled.set()
            terminate_thread_children(self.active_tid)

    def stop(self) -> bool:
        self.cancel_current()
        if self.thread:
            self.thread.join(timeout=2)
        return self.thread is None or not self.thread.is_alive()

    def _normal_work_pending(self) -> bool:
        row = self.store.db.execute(
            """SELECT 1 FROM songs WHERE
               (status='waiting' AND (prep_state='pending' OR
                (prep_state='unavailable' AND prep_attempts < 3 AND prep_retry_at<=?)))
               OR (status='ready' AND prep_state='downloaded'
                   AND lyric_choice != '' AND skip_subtitles=0)
               LIMIT 1""",
            (int(time.time()),),
        ).fetchone()
        return bool(row)

    def _candidate(self) -> tuple[int, Path, str] | None:
        with self.store.lock:
            if self.owner.stop_signal.is_set() or self.owner.active_claims:
                return None
            if self._normal_work_pending():
                return None
            for song in self.store.rows(
                "SELECT * FROM songs WHERE status IN ('ready','playing') ORDER BY requested_at,id"
            ):
                resolved = self.store.replay_assets(song)
                name = resolved.get("source_file")
                if not name or Path(name).name != name:
                    continue
                source = self.store.media / name
                if source.is_symlink() or not source.is_file():
                    continue
                key = source_identity(source, self.model_sha256)
                item = self.store.db.execute(
                    "SELECT status FROM backing_tracks WHERE cache_key=?", (key,)
                ).fetchone()
                if item and item["status"] == "ready":
                    if (self.store.root / "backing" / f"{key}.m4a").is_file():
                        continue
                    self.store.db.execute(
                        "DELETE FROM backing_tracks WHERE cache_key=?", (key,)
                    )
                elif item and item["status"] in ("failed", "processing"):
                    continue
                self.store.db.execute(
                    """INSERT INTO backing_tracks
                       (cache_key,source_file,model_sha256,status,updated_at)
                       VALUES(?,?,?,'processing',?)
                       ON CONFLICT(cache_key) DO UPDATE SET
                         status='processing',updated_at=excluded.updated_at""",
                    (key, name, self.model_sha256, int(time.time())),
                )
                self.store.db.commit()
                return int(song["id"]), source, key
        return None

    def state(self, song: dict) -> str:
        resolved = self.store.replay_assets(song)
        source_name = resolved.get("source_file")
        if not source_name or Path(source_name).name != source_name:
            return "unavailable"
        source = self.store.media / source_name
        if not source.is_file():
            return "unavailable"
        key = source_identity(source, self.model_sha256)
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT status FROM backing_tracks WHERE cache_key=?", (key,)
            ).fetchone()
            if (
                row
                and row["status"] == "ready"
                and not (self.store.root / "backing" / f"{key}.m4a").is_file()
            ):
                return "pending"
        return (
            row["status"]
            if row
            else "pending"
            if self.enabled and not self.unavailable
            else "unavailable"
        )

    def retry(self, song: dict) -> bool:
        resolved = self.store.replay_assets(song)
        name = resolved.get("source_file")
        if not name or Path(name).name != name:
            return False
        source = self.store.media / name
        if not source.is_file():
            return False
        key = source_identity(source, self.model_sha256)
        with self.store.lock:
            cursor = self.store.db.execute(
                "UPDATE backing_tracks SET status='pending',error='' "
                "WHERE cache_key=? AND status='failed'",
                (key,),
            )
            self.store.db.commit()
            return bool(cursor.rowcount)

    def _publish(self, key: str, source: Path, staged: Path) -> None:
        with self.store.lock:
            if self.cancelled.is_set() or self.owner.stop_signal.is_set():
                return
            if key != source_identity(source, self.model_sha256):
                return
            # A now-cancelled song may still share the source with an active replay.
            if not any(
                self.store.replay_assets(song).get("source_file") == source.name
                for song in self.store.rows(
                    "SELECT * FROM songs WHERE status != 'cancelled'"
                )
            ):
                return
            folder = self.store.root / "backing"
            if folder.is_symlink():
                raise ValueError("Backing storage must not be a symlink")
            folder.mkdir(exist_ok=True)
            target = folder / f"{key}.m4a"
            staged.replace(target)
            self.store.db.execute(
                "UPDATE backing_tracks SET status='ready',error='',updated_at=? "
                "WHERE cache_key=?",
                (int(time.time()), key),
            )
            self.store.db.commit()

    def _process(self, song_id: int, source: Path, key: str) -> None:
        try:
            if self.cancelled.is_set() or self.owner.stop_signal.is_set():
                return
            self.store.disk_budget.check()
            with tempfile.TemporaryDirectory(
                prefix=f"vc-ktv-{song_id}-backing-", dir=self.store.root
            ) as folder:
                staging = Path(folder)
                candidate = staging / "backing.m4a"
                self.producer(
                    source,
                    candidate,
                    staging,
                    checkpoint=self.checkpoint,
                    model_source=self.model_source,
                    python=self.python,
                    cancelled=self.cancelled,
                )
                if self.cancelled.is_set() or self.owner.stop_signal.is_set():
                    return
                self.store.disk_budget.check()
                if candidate.stat().st_size < 1024:
                    raise ValueError("Backing audio output is empty")
                self._publish(key, source, candidate)
        except Exception as error:
            if not self.cancelled.is_set() and not self.owner.stop_signal.is_set():
                log.warning(
                    "Optional backing preparation failed for song #%s: %s",
                    song_id,
                    error,
                )
                with self.store.lock:
                    self.store.db.execute(
                        "UPDATE backing_tracks SET status='failed',error=?,updated_at=? "
                        "WHERE cache_key=?",
                        (type(error).__name__, int(time.time()), key),
                    )
                    self.store.db.commit()
        finally:
            with self.store.lock:
                self.store.db.execute(
                    "UPDATE backing_tracks SET status='pending' "
                    "WHERE cache_key=? AND status='processing'",
                    (key,),
                )
                self.store.db.commit()

    def loop(self) -> None:
        while not self.owner.stop_signal.wait(1):
            try:
                candidate = self._candidate()
                if candidate is None:
                    continue
                song_id, source, key = candidate
                with self.store.lock:
                    # A main worker might have claimed work since candidate selection.
                    if self.owner.active_claims or self._normal_work_pending():
                        self.store.db.execute(
                            "UPDATE backing_tracks SET status='pending' "
                            "WHERE cache_key=? AND status='processing'",
                            (key,),
                        )
                        self.store.db.commit()
                        continue
                    with self.lock:
                        self.cancelled.clear()
                        self.active_song_id = song_id
                        self.active_tid = threading.get_native_id()
                try:
                    if (
                        not self.owner.stop_signal.is_set()
                        and not self.cancelled.is_set()
                    ):
                        self._process(song_id, source, key)
                finally:
                    with self.lock:
                        self.active_song_id = None
                        self.active_tid = None
            except (OSError, sqlite3.Error, ValueError):
                log.exception("Backing track worker recovered from error")
