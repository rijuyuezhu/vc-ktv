"""AutoPrep worker lifecycle, concurrency, and media preparation."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from vc_ktv.infra.disk_budget import (
    DiskBudgetExceeded,
    budget_scope,
    check_current_budget,
)
from vc_ktv.infra.process_cancel import terminate_thread_children
from vc_ktv.processing.media_preparation import (
    media_download,
    normalize_original_codec,
    prepare_tv_source,
    render_provider_video,
    render_upstream_video,
    stream_video_kbps,
)
from vc_ktv.processing.publication import publish_original, publish_subtitles
from vc_ktv.processing.subtitle_progress import SubtitleProgress
from vc_ktv.processing.task_state import claim_next_locked, record_failure
from vc_ktv.rendering.subtitle_styles import DEFAULT_PRESET
from vc_ktv.sources import DownloadTools, get_source
from vc_ktv.sources.netease_media import download_netease_video

log = logging.getLogger(__name__)


def _native_av1_variant(row: dict) -> tuple[str, int] | None:
    # Uploaded replacements may retain a Bilibili ID, but are not its video.
    if (
        row.get("original_codec_mode") != "av1"
        or row.get("source_type") not in (None, "bilibili")
        or not Path(row.get("source_file") or "").name.startswith(
            f"{row['id']}-original-"
        )
    ):
        return None
    return row["bvid"], int(row["page"])


def _write_attempt_report(archive: Path, metadata: dict) -> None:
    """Replace subtitle metadata atomically for concurrent media readers."""
    temporary = archive / "attempt.tmp.json"
    try:
        temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(archive / "attempt.json")
    finally:
        temporary.unlink(missing_ok=True)


class SubtitleError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class AutoPrep:
    def __init__(
        self,
        store,
        probe,
        download=media_download,
        enhance=render_upstream_video,
        max_workers: int | None = None,
        gpu_alignment: bool = True,
        fast_workers: int | None = None,
    ):
        # Explicit opt-in. Keep production serial until a concurrent
        # quality-pipeline benchmark demonstrates a throughput improvement.
        max_workers = (
            int(os.getenv("VC_KTV_PREP_WORKERS", "1"))
            if max_workers is None
            else max_workers
        )
        if max_workers not in (1, 2, 3, 4):
            raise ValueError("VC_KTV_PREP_WORKERS must be 1, 2, 3 or 4")
        self.max_workers = max_workers
        fast_workers = (
            int(os.getenv("VC_KTV_FAST_WORKERS", "0"))
            if fast_workers is None
            else fast_workers
        )
        if fast_workers not in (0, 1, 2):
            raise ValueError("VC_KTV_FAST_WORKERS must be 0, 1 or 2")
        self.fast_workers = fast_workers
        self.gpu_alignment = gpu_alignment
        self.store = store
        self.probe = probe
        self.download = download
        self.netease_download = download_netease_video
        self.netease_cookies = lambda: None
        self.enhance = enhance
        self.stop_signal = threading.Event()
        self.worker: threading.Thread | None = None
        self.workers: list[threading.Thread] = []
        self.busy = threading.Event()
        self.active_claims = 0
        self.active_tasks: dict[int, tuple[int, threading.Event]] = {}
        self.active_tasks_lock = threading.RLock()
        self.budget_watcher: threading.Thread | None = None
        self.cancel_external_tasks = lambda: None
        # Manual, optional and single-flight: no new persistent queue/worker.
        self.cache_retry_slot = threading.Lock()
        self.cache_retry_thread: threading.Thread | None = None
        self.cache_retry_cancel: threading.Event | None = None
        self.cache_retry_song_id: int | None = None

    def start(self):
        with self.store.lock:
            try:
                # Recover unfinished tasks after a process restart.
                self.store.db.execute(
                    "UPDATE songs SET prep_state='pending',prep_stage='' WHERE prep_state='downloading' AND status='waiting'"
                )
                self.store.db.execute(
                    "UPDATE songs SET prep_state='downloaded',prep_stage='queued' WHERE prep_state='aligning' AND status='ready'"
                )
                self.store.db.commit()
            except sqlite3.Error:
                # Do not leave a partial recovery transaction for later requests.
                self.store.db.rollback()
                log.exception("Unable to recover AutoPrep tasks at startup")
                return
        self.resume_after_failed_stop()

    def stop(self):
        self.stop_signal.set()
        self.cancel_tv_cache_retry()
        for thread in self.workers:
            thread.join(timeout=2)
        if self.cache_retry_thread:
            self.cache_retry_thread.join(timeout=2)
        if self.budget_watcher:
            self.budget_watcher.join(timeout=2)
        return all(not thread.is_alive() for thread in self.workers) and (
            self.cache_retry_thread is None or not self.cache_retry_thread.is_alive()
        )

    def cancel_song(self, song_id: int) -> list[int]:
        """Stop active child processes of a cancelled song, not other workers."""
        self.cancel_tv_cache_retry(song_id)
        with self.active_tasks_lock:
            task = self.active_tasks.get(song_id)
            if task is None:
                return []
            tid, cancelled = task
            cancelled.set()
            return terminate_thread_children(tid)

    def cancel_tv_cache_retry(self, song_id: int | None = None) -> None:
        """Cancellation shares the existing per-thread subprocess termination."""
        if self.cache_retry_song_id is None or (
            song_id is not None and song_id != self.cache_retry_song_id
        ):
            return
        if self.cache_retry_cancel is not None:
            self.cache_retry_cancel.set()
        thread = self.cache_retry_thread
        if thread and thread.native_id:
            terminate_thread_children(thread.native_id)

    def retry_tv_cache(
        self,
        song_id: int,
        source: Path,
        archive: Path,
        media_file: str,
        bandwidth_mbps: float,
        native_av1: tuple[str, int] | None,
    ) -> None:
        """Run at most one manual cache repair; the song stays playable."""
        if not self.cache_retry_slot.acquire(blocking=False):
            raise RuntimeError("已有电视缓存正在补做")
        cancelled = threading.Event()
        self.cache_retry_song_id = song_id
        self.cache_retry_cancel = cancelled
        try:
            if self.stop_signal.is_set():
                raise RuntimeError("后台服务正在停止")
            with self.active_tasks_lock:
                if song_id in self.active_tasks:
                    raise RuntimeError("歌曲正在制作，请稍后再补做缓存")
            thread = threading.Thread(
                target=self._repair_tv_cache,
                args=(
                    song_id,
                    source,
                    archive,
                    media_file,
                    bandwidth_mbps,
                    native_av1,
                    cancelled,
                ),
                name="vc-ktv-tv-cache-retry",
                daemon=True,
            )
            self.cache_retry_thread = thread
            thread.start()
        except BaseException:
            self.cache_retry_song_id = None
            self.cache_retry_cancel = None
            self.cache_retry_slot.release()
            raise

    def _repair_tv_cache(
        self,
        song_id: int,
        source: Path,
        archive: Path,
        media_file: str,
        bandwidth_mbps: float,
        native_av1: tuple[str, int] | None,
        cancelled: threading.Event,
    ) -> None:
        started = time.monotonic()
        budget = getattr(self.store, "disk_budget", None)
        try:
            with budget_scope(budget, cancelled):
                check_current_budget()
                with tempfile.TemporaryDirectory(
                    prefix="tv-cache-retry-", dir=archive
                ) as tmp:
                    staging = Path(tmp)
                    duration = self.probe(source)
                    prepare_tv_source(
                        source, staging, duration, bandwidth_mbps, native_av1=native_av1
                    )
                    check_current_budget()
                    candidate = staging / "tv-source.mp4"
                    with self.store.lock:
                        row = self.store.db.execute(
                            "SELECT * FROM songs WHERE id=?", (song_id,)
                        ).fetchone()
                        resolved = self.store.replay_assets(row) if row else {}
                        settings = self.store.settings()
                        if (
                            not row
                            or row["status"] != "ready"
                            or row["prep_state"] != "done"
                            or row["media_file"] != media_file
                            or resolved.get("source_file") != source.name
                            or settings["max_bandwidth_mbps"] != bandwidth_mbps
                            or not settings["tv_html_overlay"]
                        ):
                            return  # A new render, cancellation or event close won.
                        if self.stop_signal.is_set():
                            return
                        # The attempt remains active only while its media is published.
                        if not (archive / "attempt.json").is_file():
                            return
                        metadata = json.loads((archive / "attempt.json").read_text())
                        if (
                            metadata.get("outcome") != "rendered"
                            or metadata.get("source_file") != source.name
                        ):
                            return
                        if candidate.is_file():
                            if candidate.stat().st_size < 1024:
                                raise ValueError("生成的电视缓存文件为空或不完整")
                            candidate.replace(archive / "tv-source.mp4")
                            metadata["max_bandwidth_mbps"] = bandwidth_mbps
                            metadata["tv_source_ready"] = True
                        else:
                            metadata["tv_source_ready"] = False
                        metadata["tv_cache_seconds"] = round(
                            time.monotonic() - started, 2
                        )
                        metadata.pop("tv_cache_error", None)
                        _write_attempt_report(archive, metadata)
        except Exception as error:
            if not cancelled.is_set():
                log.warning("TV cache retry failed for song #%s: %s", song_id, error)
        finally:
            self.cache_retry_song_id = None
            self.cache_retry_cancel = None
            self.cache_retry_slot.release()

    def watch_disk(self):
        budget = getattr(self.store, "disk_budget", None)
        if budget is None:
            return
        while not self.stop_signal.wait(2):
            try:
                budget.check()
            except DiskBudgetExceeded:
                with self.active_tasks_lock:
                    song_ids = list(self.active_tasks)
                with self.store.lock:
                    try:
                        self.store.db.execute(
                            "UPDATE songs SET prep_state='unavailable',prep_attempts=3,"
                            "prep_retry_at=0,prep_error=? WHERE status='waiting' "
                            "AND prep_state='downloading'",
                            ("本场 200 GiB 磁盘预算已用尽",),
                        )
                        self.store.db.execute(
                            "UPDATE songs SET prep_state='original',prep_error=? "
                            "WHERE status='ready' AND prep_state='aligning'",
                            ("本场 200 GiB 磁盘预算已用尽",),
                        )
                        self.store.db.execute(
                            "UPDATE songs SET status='ready',render_error=? "
                            "WHERE status='rendering'",
                            ("本场 200 GiB 磁盘预算已用尽",),
                        )
                        self.store.db.commit()
                    except sqlite3.Error:
                        self.store.db.rollback()
                        log.exception(
                            "Failed to mark disk-full songs; watcher will retry"
                        )
                for song_id in song_ids:
                    self.cancel_song(song_id)
                self.cancel_external_tasks()
                self.cancel_tv_cache_retry()
            except OSError:
                log.exception("Failed to inspect room disk usage")

    def resume_after_failed_stop(self):
        """Retain surviving workers and replace those that already exited."""
        self.stop_signal.clear()
        self.workers = [thread for thread in self.workers if thread.is_alive()]
        names = {thread.name for thread in self.workers}
        for index in range(1, self.max_workers + 1):
            name = f"vc-ktv-auto-prep-{index}"
            if name in names:
                continue
            thread = None
            try:
                thread = threading.Thread(target=self.loop, name=name, daemon=True)
                thread.start()
            except Exception:
                log.exception("Failed to resume AutoPrep worker %s", name)
            # Keep a worker even if Thread.start() raised after it started.
            if thread is not None and thread.is_alive():
                self.workers.append(thread)
        for index in range(1, self.fast_workers + 1):
            name = f"vc-ktv-auto-prep-fast-{index}"
            if name in names:
                continue
            try:
                thread = threading.Thread(
                    target=self.loop, args=("fast",), name=name, daemon=True
                )
                thread.start()
                if thread.is_alive():
                    self.workers.append(thread)
            except Exception:
                log.exception("Failed to resume Fast worker %s", name)
        self.worker = self.workers[0] if self.workers else None
        if self.budget_watcher is None or not self.budget_watcher.is_alive():
            self.budget_watcher = threading.Thread(
                target=self.watch_disk, name="vc-ktv-disk-budget", daemon=True
            )
            self.budget_watcher.start()

    def claim(self, lane="general"):
        with self.store.lock:
            if self.stop_signal.is_set():
                return None
            budget = getattr(self.store, "disk_budget", None)
            if budget is not None:
                budget.check()
            claimed = claim_next_locked(self.store, lane, self.gpu_alignment)
            if claimed is None:
                return None
            # Keep claim and in-memory accounting under the same Store lock.
            self.active_claims += 1
            self.busy.set()
            return claimed

    def _record_failure(self, row, error, attempt_started):
        record_failure(self.store, row, error, attempt_started)

    def loop(self, lane="general"):
        while not self.stop_signal.is_set():
            try:
                row = self.claim(lane)
            except sqlite3.Error:
                log.exception("Failed to claim AutoPrep task; retrying")
                self.stop_signal.wait(1)
                continue
            except DiskBudgetExceeded:
                self.stop_signal.wait(3)
                continue
            if not row:
                self.stop_signal.wait(1)
                continue
            attempt_started = time.monotonic()
            cancelled = threading.Event()
            with self.active_tasks_lock:
                self.active_tasks[row["id"]] = (threading.get_native_id(), cancelled)
            try:
                with budget_scope(getattr(self.store, "disk_budget", None), cancelled):
                    check_current_budget()
                    with self.store.lock:
                        current = self.store.db.execute(
                            "SELECT status,prep_state FROM songs WHERE id=?",
                            (row["id"],),
                        ).fetchone()
                        valid = bool(
                            current
                            and current["status"] == row["status"]
                            and current["prep_state"]
                            == (
                                "downloading"
                                if row["status"] == "waiting"
                                else "aligning"
                            )
                        )
                    if valid:
                        check_current_budget()
                        if row["status"] == "waiting":
                            self.prepare_source(row)
                        else:
                            self.prepare_subtitles(row)
            except Exception as error:
                log.exception("Unexpected preparation error for song #%s", row["id"])
                while not self.stop_signal.is_set():
                    try:
                        self._record_failure(row, error, attempt_started)
                        break
                    except sqlite3.Error:
                        log.exception(
                            "Failed to record AutoPrep error for song #%s; retrying",
                            row["id"],
                        )
                        self.stop_signal.wait(1)
            finally:
                with self.active_tasks_lock:
                    self.active_tasks.pop(row["id"], None)
                # Other workers may still be processing a different song.
                # Account for every claim under the same lock as claim().
                with self.store.lock:
                    self.active_claims -= 1
                    if self.active_claims == 0:
                        self.busy.clear()

    def prepare_source(self, row):
        check_current_budget()
        song_id = row["id"]
        started = time.monotonic()
        with tempfile.TemporaryDirectory(
            prefix=f"vc-ktv-{song_id}-", dir=self.store.root
        ) as tmp:
            source = get_source(row.get("source_type") or "bilibili")
            path = source.download(
                row,
                Path(tmp),
                DownloadTools(
                    video=self.download,
                    music=self.netease_download,
                    cookies=self.netease_cookies,
                    video_quality_keyword=self.download is media_download,
                    prefer_av1=row.get("original_codec_mode") == "av1",
                    prefer_h264=row.get("original_codec_mode") == "h264",
                ),
            )
            check_current_budget()
            path = normalize_original_codec(
                path,
                mode=row.get("original_codec_mode", "default"),
                max_video_kbps=stream_video_kbps(row.get("max_bandwidth_mbps", 3.0)),
            )
            check_current_budget()
            actual_duration = self.probe(path)
            budget = getattr(self.store, "disk_budget", None)
            if budget is not None:
                budget.check()
            if not publish_original(
                self.store,
                song_id,
                path,
                actual_duration,
                started,
                check_current_budget,
            ):
                return
        log.info("Song #%s original available, subtitle enhancement follows", song_id)

    def prepare_subtitles(self, row):
        check_current_budget()
        song_id = row["id"]
        stages = json.loads(row["prep_timing"] or "{}")
        started = time.monotonic()
        progress = SubtitleProgress(self.store, song_id, stages, started)
        source = self.store.media / Path(row["source_file"] or "").name
        if not source.is_file():
            raise RuntimeError("source file vanished")
        lyrics = row["lyric_choice"]
        if not lyrics:
            return
        if not self.gpu_alignment and row.get("lyric_timing_mode") != "provider":
            raise SubtitleError("gpu_disabled", "GPU 对齐未启用，请选择来源时间轴")
        # A retry must never reuse old alignment output or overwrite diagnostics.
        # Keep all artifacts scoped to this song/event.
        archive = (
            self.store.media
            / f"{song_id}-auto-alignment"
            / (f"attempt-{time.time_ns()}-{secrets.token_hex(3)}")
        )
        archive.mkdir(parents=True)
        target = self.store.media / f"{song_id}-auto-{secrets.token_hex(6)}.mp4"
        preset = row.get("style_preset", DEFAULT_PRESET)
        max_bandwidth_mbps = row.get("max_bandwidth_mbps", 3.0)
        metadata = {
            "lyric_timing_mode": row.get("lyric_timing_mode", "align"),
            "subtitle_style": preset,
            "max_bandwidth_mbps": max_bandwidth_mbps,
            "song_id": song_id,
            "source_file": row["source_file"],
            "lyric_source": row["lyric_source"],
            "outcome": "running",
        }
        published = False
        try:
            options = {"preset": preset}
            if self.enhance is render_upstream_video:
                options["max_bandwidth_mbps"] = max_bandwidth_mbps
                options["progress"] = progress
            if row.get("lyric_timing_mode") == "provider":
                (archive / "source-provider.lrc").write_text(
                    row["source_lrc_raw"], encoding="utf-8"
                )
                (archive / "confirmed_lyrics.txt").write_text(lyrics, encoding="utf-8")
                progress.start_provider_render()
                with self.store.lock:
                    self.store.db.execute(
                        "UPDATE songs SET prep_stage='render',prep_stage_at=? "
                        "WHERE id=? AND status='ready' AND prep_state='aligning'",
                        (int(time.time()), song_id),
                    )
                    self.store.db.commit()
                report = render_provider_video(
                    source,
                    row["provider_lrc"],
                    row["lyric_offset_ms"],
                    target,
                    archive,
                    float(row["duration"]),
                    max_bandwidth_mbps=max_bandwidth_mbps,
                    preset=preset,
                )
            else:
                report = self.enhance(
                    source, lyrics, target, archive, float(row["duration"]), **options
                )
            # Stop rendering time accounting before optional TV cache work.
            progress.finish()
            metadata["outcome"] = "rendered"
            metadata["report"] = report
            budget = getattr(self.store, "disk_budget", None)
            if budget is not None:
                budget.check()
            # Publish song state and its HTML subtitle manifest under the
            # same lock, so readers cannot observe one without the other.
            with self.store.lock:
                if not publish_subtitles(
                    self.store, row, target, archive / "aligned.lrc"
                ):
                    metadata["outcome"] = "stale"
                else:
                    published = True
                    metadata["ready_seconds"] = round(time.monotonic() - started, 2)
                    try:
                        _write_attempt_report(archive, metadata)
                    except OSError:
                        # The baked video is already committed and remains playable.
                        log.warning(
                            "Unable to publish subtitle manifest for song #%s", song_id
                        )
            if metadata["outcome"] == "rendered" and row.get("tv_html_overlay"):
                cache_started = time.monotonic()
                try:
                    prepare_tv_source(
                        source,
                        archive,
                        float(row["duration"]),
                        max_bandwidth_mbps,
                        native_av1=_native_av1_variant(row),
                    )
                    if budget is not None:
                        budget.check()
                except Exception as error:
                    # Cache work is optional; failure cannot invalidate a published video.
                    try:
                        (archive / "tv-source.mp4").unlink(missing_ok=True)
                    except OSError:
                        log.warning("Unable to clean TV cache for song #%s", song_id)
                    log.warning(
                        "TV source compression failed for song #%s: %s",
                        song_id,
                        error,
                    )
                finally:
                    metadata["tv_source_ready"] = (archive / "tv-source.mp4").is_file()
                    metadata["tv_cache_seconds"] = round(
                        time.monotonic() - cache_started, 2
                    )
        except Exception as error:
            if published:
                # SQLite already points to target. Nothing after publication may
                # turn a successful render into a broken media reference.
                log.exception("Post-publication work failed for song #%s", song_id)
            else:
                metadata["outcome"] = "failed"
                metadata["error_code"] = getattr(error, "code", type(error).__name__)
                metadata["error_message"] = str(error)[:400]
                log.warning(
                    "Song #%s subtitle enhancement failed, original preserved: %s",
                    song_id,
                    error,
                )
                target.unlink(missing_ok=True)
                with self.store.lock:
                    message = (
                        str(error)
                        if isinstance(error, SubtitleError)
                        else "字幕制作失败，请查看日志"
                    )
                    # Explicit style re-render may have a prior finished video.
                    # On failure preserve both its path and completed status.
                    old_video = row["media_file"]
                    keep_done = (
                        old_video
                        and old_video != row["source_file"]
                        and (self.store.media / Path(old_video).name).is_file()
                    )
                    self.store.db.execute(
                        "UPDATE songs SET prep_state=?,prep_error=? "
                        "WHERE id=? AND status='ready' AND prep_state='aligning' "
                        "AND source_file=?",
                        (
                            "done" if keep_done else "original",
                            message,
                            song_id,
                            row["source_file"],
                        ),
                    )
                    self.store.db.commit()
        finally:
            metadata["stage_seconds"] = progress.finish()
            metadata["elapsed_seconds"] = round(time.monotonic() - started, 2)
            # Diagnostics are best-effort: a committed render must not be
            # downgraded because its timing or report could not be written.
            with self.store.lock:
                try:
                    self.store.db.execute(
                        "UPDATE songs SET prep_timing=? WHERE id=? AND status='ready'",
                        (json.dumps(stages), song_id),
                    )
                    self.store.db.commit()
                except sqlite3.Error:
                    self.store.db.rollback()
                    log.exception(
                        "Failed to save subtitle timing for song #%s", song_id
                    )
            try:
                _write_attempt_report(archive, metadata)
            except OSError:
                log.exception(
                    "Failed to write subtitle attempt report for song #%s", song_id
                )
