"""SQLite transitions for AutoPrep task claims and failure writeback."""

import json
import sqlite3
import time

from vc_ktv.infra.disk_budget import DiskBudgetExceeded
from vc_ktv.sources.netease_media import MusicUnavailable


def claim_next_locked(store, lane: str, gpu_alignment: bool):
    """Select and commit the next task while the caller holds store.lock."""
    try:
        # Provider subtitles are cheap CPU work: never let downloads or
        # GPU alignment prevent an already-downloaded song from finishing.
        row = store.db.execute(
            """SELECT * FROM songs WHERE status='ready'
               AND prep_state='downloaded' AND lyric_choice != ''
               AND skip_subtitles=0 AND lyric_timing_mode='provider'
               ORDER BY requested_at,id LIMIT 1"""
        ).fetchone()
        if not row:
            # Fast workers download only songs with provider timing.
            download_filter = (
                " AND lyric_timing_mode='provider'" if lane == "fast" else ""
            )
            row = store.db.execute(
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
            # General workers may also run GPU alignment on downloaded sources.
            row = store.db.execute(
                """SELECT * FROM songs WHERE status='ready'
                   AND prep_state='downloaded' AND lyric_choice != ''
                   AND skip_subtitles=0
                   ORDER BY requested_at,id LIMIT 1"""
            ).fetchone()
        if row and row["status"] == "waiting":
            store.db.execute(
                "UPDATE songs SET prep_state='downloading',prep_attempts=prep_attempts+1,"
                "prep_error='',prep_stage='download',prep_stage_at=?,"
                "prep_timing='{}' WHERE id=?",
                (int(time.time()), row["id"]),
            )
        elif row:
            if not gpu_alignment and row["lyric_timing_mode"] != "provider":
                store.db.execute(
                    "UPDATE songs SET prep_state='original',"
                    "prep_error='GPU 对齐未启用，请选择来源时间轴或使用原片' "
                    "WHERE id=?",
                    (row["id"],),
                )
                store.db.commit()
                return None
            store.db.execute(
                "UPDATE songs SET prep_state='aligning',"
                "prep_stage='queued',prep_stage_at=? WHERE id=?",
                (int(time.time()), row["id"]),
            )
        if not row:
            return None  # Idle polling has no write to commit.
        claimed = dict(row)
        # Snapshot current settings before marking the claim committed.
        settings = store.settings()
        if row["status"] == "waiting":
            claimed["video_quality"] = settings["video_quality"]
            claimed["original_codec_mode"] = settings["original_codec_mode"]
            claimed["max_bandwidth_mbps"] = settings["max_bandwidth_mbps"]
        else:
            claimed["style_preset"] = settings["subtitle_style"]
            claimed["max_bandwidth_mbps"] = settings["max_bandwidth_mbps"]
            claimed["tv_html_overlay"] = settings["tv_html_overlay"]
        store.db.commit()
        return claimed
    except sqlite3.Error:
        try:
            store.db.rollback()
        except sqlite3.Error as error:
            raise RuntimeError("AutoPrep claim rollback failed") from error
        raise


def record_failure(store, row: dict, error: Exception, attempt_started: float):
    """Write retry or fallback state for a failed claim under store.lock."""
    with store.lock:
        try:
            current = store.db.execute(
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
                    store.db.execute(
                        "UPDATE songs SET prep_state='unavailable',prep_attempts=3,"
                        "prep_retry_at=0,prep_error=? WHERE id=? AND status='waiting'",
                        (str(error), row["id"]),
                    )
                else:
                    store.db.execute(
                        "UPDATE songs SET prep_state='original',prep_error=? "
                        "WHERE id=? AND status='ready'",
                        (str(error), row["id"]),
                    )
                store.db.commit()
                return
            if row["status"] == "waiting":
                attempts = row["prep_attempts"] + 1
                retry = 0
                if attempts < 3:
                    retry = int(time.time()) + (60 if attempts == 1 else 300)
                # Only show trusted, short diagnostic text; never expose signed media URLs.
                source_type = row.get("source_type")
                reason = "公开媒体下载失败"
                if source_type == "netease":
                    reason = (
                        str(error)[:90]
                        if isinstance(error, MusicUnavailable)
                        else "网易云音源不可用"
                    )
                elif source_type == "bilibili":
                    detail = str(error)
                    if "Unable to extract initial state" in detail:
                        reason = "B站页面结构变化"
                    elif "接口" in detail:
                        reason = "B站公开播放接口暂不可用"
                    elif "CDN" in detail:
                        reason = "B站媒体 CDN 暂不可用"
                message = f"{reason}（已尝试 {attempts}/3 次）"
                if retry:
                    message += "，稍后自动重试"
                store.db.execute(
                    "UPDATE songs SET prep_state='unavailable',prep_error=?,"
                    "prep_retry_at=?,prep_timing=? WHERE id=? AND status='waiting'",
                    (
                        message,
                        retry,
                        json.dumps(
                            {"download": round(time.monotonic() - attempt_started, 2)}
                        ),
                        row["id"],
                    ),
                )
            else:
                store.db.execute(
                    "UPDATE songs SET prep_state='original',"
                    "prep_error='字幕制作失败' WHERE id=? AND status='ready'",
                    (row["id"],),
                )
            store.db.commit()
        except sqlite3.Error:
            try:
                store.db.rollback()
            except sqlite3.Error as failure:
                raise RuntimeError(
                    "AutoPrep failure writeback rollback failed"
                ) from failure
            raise
