"""Administrator media upload, subtitle retry, TextGrid import and manual render."""

import logging
import secrets
import sqlite3
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Cookie, File, UploadFile
from starlette.concurrency import run_in_threadpool

from vc_ktv.application.errors import bail
from vc_ktv.application.media import MEDIA_MIMES
from vc_ktv.application.schemas import Lyrics, RenderRequest
from vc_ktv.core.store import time_now
from vc_ktv.infra.disk_budget import DiskBudgetExceeded
from vc_ktv.processing.media_preparation import normalize_hevc_source
from vc_ktv.rendering.karaoke import write_ass
from vc_ktv.rendering.textgrid_import import convert_textgrid


def create_preparation_admin_router(
    *,
    app,
    store,
    events,
    require_admin,
    check_room_capacity,
    check_guest_capacity,
    manual,
):
    router = APIRouter()

    @router.post("/api/admin/songs/{song_id}/subtitles/retry")
    def retry_subtitles(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if row["status"] != "ready" or row["prep_state"] not in (
                "original",
                "done",
            ):
                bail(409, "仅可重制尚未播放且字幕已完成或失败的歌曲")
            source_file = store.replay_assets(row).get("source_file")
            if not row["lyric_choice"] or not source_file:
                bail(409, "歌曲缺少已选歌词或原始视频")
            if not (store.media / Path(source_file).name).is_file():
                bail(409, "原视频已丢失，无法仅重试字幕")
            try:
                store.db.execute(
                    "UPDATE songs SET prep_state='downloaded',prep_error='',"
                    "source_file=?,skip_subtitles=0,prep_stage='queued',prep_stage_at=? WHERE id=?",
                    (source_file, time_now(), song_id),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True, "id": song_id, "status": "downloaded"}

    @router.post("/api/admin/songs/{song_id}/download/retry")
    def retry_media_download(song_id: int, vc_admin: str | None = Cookie(default=None)):
        """Requeue one failed download without requiring the guest to re-order."""
        require_admin(vc_admin)
        with store.lock:
            row = store.db.execute(
                "SELECT status,prep_state,prep_attempts,requester "
                "FROM songs WHERE id=?",
                (song_id,),
            ).fetchone()
            if row is None:
                bail(404, "歌曲不存在")
            if row["status"] != "waiting" or row["prep_state"] != "unavailable":
                bail(409, "只有准备失败的待处理歌曲可以重试下载")
            # A terminal failure frees its capacity; manually restarting it
            # must claim capacity again, without being subject to order pause.
            if row["prep_attempts"] >= 3:
                check_room_capacity()
                check_guest_capacity(row["requester"])
            try:
                store.db.execute(
                    "UPDATE songs SET prep_state='pending',prep_attempts=0,"
                    "prep_retry_at=0,prep_error='',prep_stage='download' WHERE id=?",
                    (song_id,),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True, "id": song_id}

    @router.post("/api/admin/songs/{song_id}/subtitles/skip")
    def skip_subtitles(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            row = store.db.execute(
                "SELECT status,prep_state,source_file,skip_subtitles "
                "FROM songs WHERE id=?",
                (song_id,),
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if (
                row["skip_subtitles"]
                or row["status"] != "ready"
                or row["prep_state"]
                not in (
                    "downloaded",
                    "original",
                    "aligning",
                )
            ):
                bail(409, "仅可对已下载且尚未播放的歌曲跳过字幕")
            source = store.media / Path(row["source_file"] or "").name
            if not source.is_file():
                bail(409, "原视频尚未就绪，无法进入队列")
            try:
                store.db.execute(
                    "UPDATE songs SET media_file=source_file,prep_state='original',"
                    "skip_subtitles=1,prep_stage='skipped',prep_stage_at=?,"
                    "prep_error='' WHERE id=?",
                    (time_now(), song_id),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True, "id": song_id, "status": "ready", "source": "original"}

    @router.post("/api/admin/songs/{song_id}/media")
    async def upload(
        song_id: int,
        file: Annotated[UploadFile, File()],
        vc_admin: str | None = Cookie(default=None),
    ):
        require_admin(vc_admin)
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in MEDIA_MIMES:
            bail(400, "仅支持 MP4、M4V、WebM 文件")
        with store.lock:
            row = store.db.execute(
                "SELECT status,media_file,source_file FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if row["status"] not in ("waiting", "ready"):
                bail(409, "正在播放或已结束，不能覆盖视频")
        target = store.media / f"{song_id}-{secrets.token_hex(8)}{suffix}"
        size = 0
        try:
            with target.open("wb") as out:
                while block := await file.read(1024 * 1024):
                    size += len(block)
                    out.write(block)
                    # Include this file and all existing room artifacts in
                    # the aggregate 200 GiB budget, not just this upload.
                    if size % (16 * 1024 * 1024) < len(block):
                        try:
                            store.disk_budget.check()
                        except DiskBudgetExceeded as error:
                            bail(507, str(error))
            try:
                store.disk_budget.check()
            except DiskBudgetExceeded as error:
                bail(507, str(error))
            if not size:
                bail(400, "视频为空")
            target = await run_in_threadpool(
                normalize_hevc_source,
                target,
                force_h264=store.settings()["force_h264_originals"],
            )
            store.disk_budget.check()
            try:
                actual_duration = await run_in_threadpool(app.state.probe, target)
            except ValueError as error:
                bail(400, str(error))
            with store.lock:
                row = store.db.execute(
                    "SELECT status,media_file,source_file FROM songs WHERE id=?",
                    (song_id,),
                ).fetchone()
                if row["status"] not in ("waiting", "ready"):
                    bail(409, "歌曲状态已变化")
                try:
                    store.db.execute(
                        "UPDATE songs SET status='ready',media_file=?,source_file=?,duration=?,render_error='',prep_state='manual',prep_error='' WHERE id=?",
                        (target.name, target.name, round(actual_duration), song_id),
                    )
                    store.db.commit()
                except BaseException:
                    # The outer handler must see the last committed media
                    # pointer before deciding whether to remove this upload.
                    store.db.rollback()
                    raise
            for old in set(filter(None, (row["media_file"], row["source_file"]))):
                if old != target.name:
                    try:
                        (store.media / old).unlink(missing_ok=True)
                    except OSError as error:
                        logging.getLogger(__name__).warning(
                            "Song #%s obsolete upload cleanup failed: %s",
                            song_id,
                            error,
                        )
        except BaseException:
            with store.lock:
                if not store.db.execute(
                    "SELECT 1 FROM songs WHERE id=? AND media_file=?",
                    (song_id, target.name),
                ).fetchone():
                    target.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        return {"id": song_id, "status": "ready", "bytes": size}

    @router.post("/api/admin/songs/{song_id}/lyrics")
    def set_lyrics(
        song_id: int, body: Lyrics, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        with store.lock:
            if not store.db.execute(
                "SELECT 1 FROM songs WHERE id=?", (song_id,)
            ).fetchone():
                bail(404, "歌曲不存在")
            if (
                store.db.execute(
                    "SELECT status FROM songs WHERE id=?", (song_id,)
                ).fetchone()[0]
                == "rendering"
            ):
                bail(409, "字幕制作中暂不能修改歌词")
            try:
                store.db.execute(
                    "UPDATE songs SET lyrics=? WHERE id=?", (body.text, song_id)
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True}

    @router.get("/api/admin/songs/{song_id}/lyrics")
    def get_lyrics(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            row = store.db.execute(
                "SELECT lyrics FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            return {"text": row[0]}

    @router.post("/api/admin/songs/{song_id}/alignment/import")
    async def import_textgrid(
        song_id: int,
        file: Annotated[UploadFile, File()],
        offset: float = 0,
        vc_admin: str | None = Cookie(default=None),
    ):
        require_admin(vc_admin)
        if not (file.filename or "").lower().endswith(".textgrid"):
            bail(400, "请上传 TIFA TextGrid 文件")
        content = await file.read()
        await file.close()
        with store.lock:
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?",
                (song_id,),
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if row["status"] not in ("ready", "waiting") or not store.replay_assets(
                row
            ).get("source_file"):
                bail(409, "请先上传原始视频，且不能在播放或制作过程中替换时间轴")
            original = row["lyrics"]
            duration = row["duration"]
        try:
            with tempfile.TemporaryDirectory(prefix="vc-ktv-grid-") as temp:
                path = Path(temp) / "alignment.TextGrid"
                path.write_bytes(content)
                proposed, report = convert_textgrid(path, original, duration, offset)
                write_ass(proposed, Path(temp) / "validated.ass", duration, 1.8)
        except (ValueError, UnicodeError, OSError, AssertionError) as error:
            bail(400, f"TextGrid 无法安全导入：{error}")
        # Keep raw alignment and original text for reproducibility.
        archive = store.media / f"{song_id}-alignment-{secrets.token_hex(5)}"
        archive.mkdir(mode=0o700)
        try:
            (archive / "alignment.TextGrid").write_bytes(content)
            (archive / "lyrics.before.txt").write_text(original, encoding="utf-8")
            (archive / "lyrics.enhanced.lrc").write_text(proposed, encoding="utf-8")
            with store.lock:
                latest = store.db.execute(
                    "SELECT status,lyrics FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if (
                    not latest
                    or latest["status"] not in ("ready", "waiting")
                    or latest["lyrics"] != original
                ):
                    bail(409, "歌词或歌曲状态发生变化，请重新导入")
                try:
                    store.db.execute(
                        "UPDATE songs SET lyrics=? WHERE id=?", (proposed, song_id)
                    )
                    store.db.commit()
                except sqlite3.Error:
                    store.db.rollback()
                    raise
        except BaseException:
            import shutil

            shutil.rmtree(archive)
            raise
        return {"id": song_id, "report": report, "lyrics": proposed}

    @router.post("/api/admin/songs/{song_id}/render")
    def render_song(
        song_id: int, body: RenderRequest, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        return manual.start(song_id, body.lead)

    return router
