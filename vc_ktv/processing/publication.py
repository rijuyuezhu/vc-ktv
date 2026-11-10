"""Publish prepared videos only when their song's SQLite state still matches."""

import json
import logging
import secrets
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


def publish_original(
    store,
    song_id: int,
    source: Path,
    duration: float,
    started: float,
    check_budget: Callable[[], None],
) -> bool:
    """Copy a downloaded original and commit its pointer, or discard the copy."""
    target = store.media / f"{song_id}-original-{secrets.token_hex(6)}{source.suffix}"
    published = False
    try:
        with source.open("rb") as src, target.open("wb") as out:
            while chunk := src.read(4 * 1024 * 1024):
                out.write(chunk)
                check_budget()
        with store.lock:
            latest = store.db.execute(
                "SELECT status,prep_state,skip_subtitles FROM songs WHERE id=?",
                (song_id,),
            ).fetchone()
            if (
                not latest
                or latest["status"] != "waiting"
                or latest["prep_state"] != "downloading"
            ):
                return False
            try:
                store.db.execute(
                    """UPDATE songs SET media_file=?,source_file=?,duration=?,status='ready',
                       prep_state=?,prep_stage=?,prep_stage_at=?,prep_timing=?,
                       prep_error='',prep_retry_at=0 WHERE id=?""",
                    (
                        target.name,
                        target.name,
                        round(duration),
                        "original" if latest["skip_subtitles"] else "downloaded",
                        "skipped" if latest["skip_subtitles"] else "downloaded",
                        int(time.time()),
                        json.dumps({"download": round(time.monotonic() - started, 2)}),
                        song_id,
                    ),
                )
                store.db.commit()
            except BaseException:
                store.db.rollback()
                raise
            published = True
            return True
    finally:
        # A failed copy, cancelled claim, or failed commit must leave no source.
        if not published:
            target.unlink(missing_ok=True)


def publish_subtitles(store, claimed: dict, target: Path, aligned_lrc: Path) -> bool:
    """Commit a render against its original claim; cleanup follows commit."""
    song_id = claimed["id"]
    with store.lock:
        latest = store.db.execute(
            "SELECT status,source_file,media_file,prep_state,lyric_choice,"
            "lyric_timing_mode,provider_lrc,lyric_offset_ms FROM songs WHERE id=?",
            (song_id,),
        ).fetchone()
        if not (
            latest
            and latest["status"] == "ready"
            and latest["source_file"] == claimed["source_file"]
            and latest["prep_state"] == "aligning"
            and latest["lyric_choice"] == claimed["lyric_choice"]
            and latest["lyric_timing_mode"] == claimed.get("lyric_timing_mode", "align")
            and latest["provider_lrc"] == claimed.get("provider_lrc", "")
            and latest["lyric_offset_ms"] == claimed.get("lyric_offset_ms", 0)
        ):
            target.unlink(missing_ok=True)
            return False
        try:
            store.db.execute(
                "UPDATE songs SET media_file=?,lyrics=?,prep_state='done',"
                "prep_stage='done',prep_stage_at=?,prep_error='' WHERE id=?",
                (target.name, aligned_lrc.read_text(), int(time.time()), song_id),
            )
            store.db.commit()
        except BaseException:
            # The AutoPrep caller removes the failed render; never commit its path.
            store.db.rollback()
            raise
        # Cleanup is best-effort: it must not invalidate the committed render.
        old_media = latest["media_file"]
        if old_media and old_media != claimed["source_file"]:
            try:
                (store.media / old_media).unlink(missing_ok=True)
            except OSError as error:
                log.warning(
                    "Song #%s obsolete media cleanup failed: %s", song_id, error
                )
        return True
