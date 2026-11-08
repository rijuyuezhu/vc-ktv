"""AutoPrep worker scheduling, cancellation, and transactional publication."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import subprocess
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
    normalize_hevc_source,
    prepare_tv_source,
    render_provider_video,
    render_upstream_video,
)
from vc_ktv.rendering.subtitle_styles import DEFAULT_PRESET
from vc_ktv.sources import DownloadTools, get_source
from vc_ktv.sources.netease_media import MusicUnavailable, download_netease_video

log = logging.getLogger(__name__)


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
        for thread in self.workers:
            thread.join(timeout=2)
        if self.budget_watcher:
            self.budget_watcher.join(timeout=2)
        return all(not thread.is_alive() for thread in self.workers)

    def cancel_song(self, song_id: int) -> list[int]:
        """Stop active child processes of a cancelled song, not other workers."""
        with self.active_tasks_lock:
            task = self.active_tasks.get(song_id)
            if task is None:
                return []
            tid, cancelled = task
            cancelled.set()
            return terminate_thread_children(tid)

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
                for song_id in song_ids:
                    self.cancel_song(song_id)
                self.cancel_external_tasks()
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
            try:
                # Provider subtitles are cheap CPU work: never let downloads or
                # GPU alignment prevent an already-downloaded song from finishing.
                row = self.store.db.execute(
                    """SELECT * FROM songs WHERE status='ready'
                       AND prep_state='downloaded' AND lyric_choice != ''
                       AND skip_subtitles=0 AND lyric_timing_mode='provider'
                       ORDER BY requested_at,id LIMIT 1"""
                ).fetchone()
                if not row:
                    download_filter = (
                        " AND lyric_timing_mode='provider'" if lane == "fast" else ""
                    )
                    row = self.store.db.execute(
                        """SELECT * FROM songs WHERE status='waiting' AND
                           (prep_state='pending' OR
                            (prep_state='unavailable' AND prep_attempts < 3
                             AND prep_retry_at <= ?))"""
                        + download_filter
                        + """ ORDER BY CASE WHEN prep_state='pending' THEN 0 ELSE 1 END,
                                     requested_at,id LIMIT 1""",
                        (int(time.time()),),
                    ).fetchone()
                if not row and lane == "general":
                    row = self.store.db.execute(
                        """SELECT * FROM songs WHERE status='ready'
                           AND prep_state='downloaded' AND lyric_choice != ''
                           AND skip_subtitles=0
                           ORDER BY requested_at,id LIMIT 1"""
                    ).fetchone()
                if row and row["status"] == "waiting":
                    self.store.db.execute(
                        "UPDATE songs SET prep_state='downloading',prep_attempts=prep_attempts+1,"
                        "prep_error='',prep_stage='download',prep_stage_at=?,"
                        "prep_timing='{}' WHERE id=?",
                        (int(time.time()), row["id"]),
                    )
                elif row:
                    if (
                        not self.gpu_alignment
                        and row["lyric_timing_mode"] != "provider"
                    ):
                        self.store.db.execute(
                            "UPDATE songs SET prep_state='original',"
                            "prep_error='GPU 对齐未启用，请选择来源时间轴或使用原片' "
                            "WHERE id=?",
                            (row["id"],),
                        )
                        self.store.db.commit()
                        return None
                    self.store.db.execute(
                        "UPDATE songs SET prep_state='aligning',"
                        "prep_stage='queued',prep_stage_at=? WHERE id=?",
                        (int(time.time()), row["id"]),
                    )
                if not row:
                    return None  # Idle polling has no write to commit.
                claimed = dict(row)
                # Snapshot settings before commit; failure must not strand a
                # claimed task or leave active_claims/busy stuck.
                settings = self.store.settings()
                if row["status"] == "waiting":
                    claimed["video_quality"] = settings["video_quality"]
                    claimed["force_h264_originals"] = settings["force_h264_originals"]
                else:
                    claimed["style_preset"] = settings["subtitle_style"]
                    claimed["max_bandwidth_mbps"] = settings["max_bandwidth_mbps"]
                    claimed["tv_html_overlay"] = settings["tv_html_overlay"]
                self.store.db.commit()
            except sqlite3.Error:
                try:
                    self.store.db.rollback()
                except sqlite3.Error as error:
                    raise RuntimeError("AutoPrep claim rollback failed") from error
                raise
            self.active_claims += 1
            self.busy.set()
            return claimed

    def _record_failure(self, row, error, attempt_started):
        with self.store.lock:
            try:
                current = self.store.db.execute(
                    "SELECT status,prep_state,prep_error FROM songs WHERE id=?",
                    (row["id"],),
                ).fetchone()
                if (
                    not current
                    or current["status"] == "cancelled"
                    or current["prep_error"].startswith("本场 200 GiB")
                ):
                    return
                if isinstance(error, DiskBudgetExceeded):
                    if row["status"] == "waiting":
                        self.store.db.execute(
                            "UPDATE songs SET prep_state='unavailable',prep_attempts=3,"
                            "prep_retry_at=0,prep_error=? WHERE id=? AND status='waiting'",
                            (str(error), row["id"]),
                        )
                    else:
                        self.store.db.execute(
                            "UPDATE songs SET prep_state='original',prep_error=? "
                            "WHERE id=? AND status='ready'",
                            (str(error), row["id"]),
                        )
                    self.store.db.commit()
                    return
                if row["status"] == "waiting":
                    attempts = row["prep_attempts"] + 1
                    retry = (
                        int(time.time()) + (60 if attempts == 1 else 300)
                        if attempts < 3
                        else 0
                    )
                    # Only show trusted, short diagnostic text; never expose signed media URLs.
                    reason = (
                        str(error)[:90]
                        if row.get("source_type") == "netease"
                        and isinstance(error, MusicUnavailable)
                        else "网易云音源不可用"
                        if row.get("source_type") == "netease"
                        else "公开媒体下载失败"
                    )
                    if row.get(
                        "source_type"
                    ) == "bilibili" and "Unable to extract initial state" in str(error):
                        reason = "B站页面结构变化"
                    elif row.get("source_type") == "bilibili" and "接口" in str(error):
                        reason = "B站公开播放接口暂不可用"
                    elif row.get("source_type") == "bilibili" and "CDN" in str(error):
                        reason = "B站媒体 CDN 暂不可用"
                    message = f"{reason}（已尝试 {attempts}/3 次）"
                    if retry:
                        message += "，稍后自动重试"
                    self.store.db.execute(
                        "UPDATE songs SET prep_state='unavailable',prep_error=?,"
                        "prep_retry_at=?,prep_timing=? WHERE id=? AND status='waiting'",
                        (
                            message,
                            retry,
                            json.dumps(
                                {
                                    "download": round(
                                        time.monotonic() - attempt_started, 2
                                    )
                                }
                            ),
                            row["id"],
                        ),
                    )
                else:
                    self.store.db.execute(
                        "UPDATE songs SET prep_state='original',"
                        "prep_error='字幕制作失败' WHERE id=? AND status='ready'",
                        (row["id"],),
                    )
                self.store.db.commit()
            except sqlite3.Error:
                try:
                    self.store.db.rollback()
                except sqlite3.Error as failure:
                    raise RuntimeError(
                        "AutoPrep failure writeback rollback failed"
                    ) from failure
                raise

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
                ),
            )
            check_current_budget()
            path = normalize_hevc_source(
                path, force_h264=row.get("force_h264_originals", False)
            )
            check_current_budget()
            actual_duration = self.probe(path)
            budget = getattr(self.store, "disk_budget", None)
            if budget is not None:
                budget.check()
            target = (
                self.store.media
                / f"{song_id}-original-{secrets.token_hex(6)}{path.suffix}"
            )
            published = False
            try:
                with path.open("rb") as src, target.open("wb") as out:
                    while chunk := src.read(4 * 1024 * 1024):
                        out.write(chunk)
                        check_current_budget()
                with self.store.lock:
                    latest = self.store.db.execute(
                        "SELECT status,prep_state,skip_subtitles FROM songs WHERE id=?",
                        (song_id,),
                    ).fetchone()
                    if (
                        not latest
                        or latest["status"] != "waiting"
                        or latest["prep_state"] != "downloading"
                    ):
                        return
                    try:
                        self.store.db.execute(
                            """UPDATE songs SET media_file=?,source_file=?,duration=?,status='ready',
                               prep_state=?,prep_stage=?,prep_stage_at=?,prep_timing=?,
                               prep_error='',prep_retry_at=0 WHERE id=?""",
                            (
                                target.name,
                                target.name,
                                round(actual_duration),
                                "original"
                                if latest["skip_subtitles"]
                                else "downloaded",
                                "skipped" if latest["skip_subtitles"] else "downloaded",
                                int(time.time()),
                                json.dumps(
                                    {"download": round(time.monotonic() - started, 2)}
                                ),
                                song_id,
                            ),
                        )
                        self.store.db.commit()
                    except BaseException:
                        self.store.db.rollback()
                        raise
                    published = True
            finally:
                # Only a committed media path may retain the published file.
                # A failed copy or SQLite write must not leak partial videos.
                if not published:
                    target.unlink(missing_ok=True)
        log.info("Song #%s original available, subtitle enhancement follows", song_id)

    def prepare_subtitles(self, row):
        check_current_budget()
        song_id = row["id"]
        stages = json.loads(row["prep_timing"] or "{}")
        started = time.monotonic()
        current_stage = None
        stage_started = started
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
        try:
            options = {"preset": preset}
            if self.enhance is render_upstream_video:
                options["max_bandwidth_mbps"] = max_bandwidth_mbps
                stage_names = {
                    "读取原视频并提取音频": "extract",
                    "Kim 分离用于推理的人声": "kim",
                    "双遍 Qwen3-ASR 定位与 TIFA 安全分块对齐": "align",
                    "整首 TIFA 对齐完整歌词": "align",
                    "整首 TIFA 对齐完整歌词（不分块、不估算）": "align",
                    "生成 ASS、LRC 与时间轴记录": "subtitles",
                    "将字幕烧录到保留原始音轨的 MP4": "render",
                }

                def progress(message):
                    nonlocal current_stage, stage_started
                    stage = stage_names.get(message)
                    if stage is None or stage == current_stage:
                        return
                    now = time.monotonic()
                    if current_stage is not None:
                        stages[current_stage] = round(now - stage_started, 2)
                    current_stage, stage_started = stage, now
                    # At most one DB write per stage transition; the source
                    # audio and finished video are never affected by telemetry.
                    with self.store.lock:
                        try:
                            cur = self.store.db.execute(
                                "UPDATE songs SET prep_stage=?,prep_stage_at=?,prep_timing=? "
                                "WHERE id=? AND status='ready' AND prep_state='aligning' "
                                "AND prep_stage!=?",
                                (
                                    stage,
                                    int(time.time()),
                                    json.dumps(stages),
                                    song_id,
                                    stage,
                                ),
                            )
                            if cur.rowcount:
                                self.store.db.commit()
                        except sqlite3.Error:
                            self.store.db.rollback()
                            log.exception(
                                "Failed to save subtitle progress for song #%s", song_id
                            )

                options["progress"] = progress
            if row.get("lyric_timing_mode") == "provider":
                (archive / "source-provider.lrc").write_text(
                    row["source_lrc_raw"], encoding="utf-8"
                )
                (archive / "confirmed_lyrics.txt").write_text(lyrics, encoding="utf-8")
                current_stage, stage_started = "render", time.monotonic()
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
            if row.get("tv_html_overlay"):
                try:
                    prepare_tv_source(
                        source, archive, float(row["duration"]), max_bandwidth_mbps
                    )
                    metadata["tv_source_ready"] = True
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    log.warning(
                        "TV source compression failed for song #%s: %s", song_id, error
                    )
                    metadata["tv_source_ready"] = False
            metadata["outcome"] = "rendered"
            metadata["report"] = report
            budget = getattr(self.store, "disk_budget", None)
            if budget is not None:
                budget.check()
            with self.store.lock:
                latest = self.store.db.execute(
                    "SELECT status,source_file,media_file,prep_state,lyric_choice,"
                    "lyric_timing_mode,provider_lrc,lyric_offset_ms FROM songs WHERE id=?",
                    (song_id,),
                ).fetchone()
                if (
                    latest
                    and latest["status"] == "ready"
                    and latest["source_file"] == row["source_file"]
                    and latest["prep_state"] == "aligning"
                    and latest["lyric_choice"] == lyrics
                    and latest["lyric_timing_mode"]
                    == row.get("lyric_timing_mode", "align")
                    and latest["provider_lrc"] == row.get("provider_lrc", "")
                    and latest["lyric_offset_ms"] == row.get("lyric_offset_ms", 0)
                ):
                    try:
                        self.store.db.execute(
                            "UPDATE songs SET media_file=?,lyrics=?,prep_state='done',"
                            "prep_stage='done',prep_stage_at=?,prep_error='' WHERE id=?",
                            (
                                target.name,
                                (archive / "aligned.lrc").read_text(),
                                int(time.time()),
                                song_id,
                            ),
                        )
                        self.store.db.commit()
                    except BaseException:
                        # The failure handler deletes target. Never commit a
                        # media_file pointing at that deleted render later.
                        self.store.db.rollback()
                        raise
                    # The new video is committed. A failed cleanup of the old
                    # artifact must not turn this success into a render failure
                    # and delete the new file referenced by SQLite.
                    old_media = latest["media_file"]
                    if old_media and old_media != row["source_file"]:
                        try:
                            (self.store.media / old_media).unlink(missing_ok=True)
                        except OSError as error:
                            log.warning(
                                "Song #%s obsolete media cleanup failed: %s",
                                song_id,
                                error,
                            )
                else:
                    target.unlink(missing_ok=True)  # Playback already started.
                    metadata["outcome"] = "stale"
        except Exception as error:
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
            if current_stage is not None:
                stages[current_stage] = round(time.monotonic() - stage_started, 2)
            metadata["stage_seconds"] = stages
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
                (archive / "attempt.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            except OSError:
                log.exception(
                    "Failed to write subtitle attempt report for song #%s", song_id
                )
