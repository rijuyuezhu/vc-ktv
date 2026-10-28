"""Local best-effort media preparation. No LLM, external agent, or human confirmation.

The original video is downloadable before enhancement but is playable only
when the active event allows unsubtitled media.
Source failures never block the fair queue of already playable songs.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .bilibili_media import public_media_download
from .upstream_pipeline import make_karaoke_video, spark_settings

log = logging.getLogger(__name__)


class SubtitleError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def title_key(name: str) -> str:
    name = name.strip()
    # Original song titles are frequently embedded in a long Bilibili headline:
    # 「宣传语。对你许下了《歌名》。【UP/活动标签】」.
    # Extract only when there is one unambiguous Chinese title enclosure.
    enclosed = [x.strip() for x in re.findall(r"《([^《》]{2,48})》", name)]
    if len(enclosed) == 1:
        return enclosed[0]
    # Trim tags such as 【洛天依AI】 and [cover] without stripping actual song words.
    for _ in range(4):
        trimmed = re.sub(r"^\s*[【\[][^\]】]{1,55}[】\]]\s*", "", name)
        if trimmed == name:
            break
        name = trimmed
    return re.split(
        r"\s*[-—｜|]\s*(?:PV|MV|翻唱|cover|完整版)\b", name, maxsplit=1, flags=re.I
    )[0].strip()


def media_download(bvid: str, page: int, directory: Path) -> Path:
    output = directory / "source.%(ext)s"
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-part",
        "--no-progress",
        "--no-warnings",
        "--socket-timeout",
        "12",
        "--retries",
        "2",
        "--fragment-retries",
        "2",
        "--max-filesize",
        "512M",
        "-f",
        "bv[height<=720][ext=mp4]+ba[ext=m4a]/bv[height<=720]+ba/best[height<=720]/best",
        "--merge-output-format",
        "mp4",
        "-o",
        str(output),
        f"https://www.bilibili.com/video/{bvid}/?p={page}",
    ]
    process = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if process.returncode != 0:
        error = (process.stderr[-400:] or process.stdout[-400:]).strip()
        log.warning(
            "yt-dlp failed for %s p%s: %s; trying public API", bvid, page, error
        )
        return public_media_download(bvid, page, directory)
    matches = [
        p for p in directory.glob("source.*") if p.suffix in (".mp4", ".webm", ".m4v")
    ]
    if len(matches) != 1:
        raise RuntimeError("下载后缺少唯一的视频文件")
    if matches[0].stat().st_size > 512 * 1024 * 1024:
        raise RuntimeError("视频超过媒体大小限制")
    return matches[0]


def render_upstream_video(
    source: Path, lyrics: str, destination: Path, archive: Path, duration: float
) -> dict:
    """Run the copied upstream pipeline; no legacy automatic TIFA fallback."""
    # AutoPrep owns a new attempt directory; the pipeline owns its own
    # isolated child directory and does not mutate the original source.
    result = make_karaoke_video(source, lyrics, archive / "pipeline", spark_settings())
    shutil.copyfile(result.video, destination)
    shutil.copyfile(result.ass.parent / "lyrics.lrc", archive / "aligned.lrc")
    return json.loads(result.report.read_text(encoding="utf-8"))


class AutoPrep:
    def __init__(
        self,
        store,
        probe,
        download=media_download,
        enhance=render_upstream_video,
    ):
        self.store = store
        self.probe = probe
        self.download = download
        self.enhance = enhance
        self.stop_signal = threading.Event()
        self.worker: threading.Thread | None = None
        self.busy = threading.Event()

    def start(self):
        with self.store.lock:
            # Recover unfinished tasks after a process restart.
            self.store.db.execute(
                "UPDATE songs SET prep_state='pending' WHERE prep_state='downloading' AND status='waiting'"
            )
            self.store.db.execute(
                "UPDATE songs SET prep_state='downloaded' WHERE prep_state='aligning' AND status='ready'"
            )
            self.store.db.commit()
        self.worker = threading.Thread(
            target=self.loop, name="vc-ktv-auto-prep", daemon=True
        )
        self.worker.start()

    def stop(self):
        self.stop_signal.set()
        if self.worker:
            self.worker.join(timeout=2)
            return not self.worker.is_alive()
        return True

    def claim(self):
        with self.store.lock:
            # Closing an idle event can race the next loop iteration.
            if self.stop_signal.is_set():
                return None
            row = self.store.db.execute(
                """SELECT * FROM songs WHERE status='waiting' AND
                   (prep_state='pending' OR
                    (prep_state='unavailable' AND prep_attempts < 3 AND prep_retry_at <= ?))
                   ORDER BY CASE WHEN prep_state='pending' THEN 0 ELSE 1 END,
                            requested_at,id LIMIT 1""",
                (int(time.time()),),
            ).fetchone()
            if row:
                self.store.db.execute(
                    "UPDATE songs SET prep_state='downloading',prep_attempts=prep_attempts+1,"
                    "prep_error='' WHERE id=?",
                    (row["id"],),
                )
            else:
                row = self.store.db.execute(
                    """SELECT * FROM songs WHERE status='ready'
                       AND prep_state='downloaded' AND lyric_choice != ''
                       ORDER BY requested_at,id LIMIT 1"""
                ).fetchone()
                if row:
                    self.store.db.execute(
                        "UPDATE songs SET prep_state='aligning' WHERE id=?",
                        (row["id"],),
                    )
            self.store.db.commit()
            if row:
                self.busy.set()
            return dict(row) if row else None

    def loop(self):
        while not self.stop_signal.is_set():
            row = self.claim()
            if not row:
                self.stop_signal.wait(1)
                continue
            try:
                if row["status"] == "waiting":
                    self.prepare_source(row)
                else:
                    self.prepare_subtitles(row)
            except Exception as error:
                log.exception("Unexpected preparation error for song #%s", row["id"])
                with self.store.lock:
                    if row["status"] == "waiting":
                        attempts = row["prep_attempts"] + 1
                        retry = (
                            int(time.time()) + (60 if attempts == 1 else 300)
                            if attempts < 3
                            else 0
                        )
                        # Only show trusted, short diagnostic text; never expose signed media URLs.
                        reason = "公开媒体下载失败"
                        if "Unable to extract initial state" in str(error):
                            reason = "B站页面结构变化"
                        elif "接口" in str(error):
                            reason = "B站公开播放接口暂不可用"
                        elif "CDN" in str(error):
                            reason = "B站媒体 CDN 暂不可用"
                        message = f"{reason}（已尝试 {attempts}/3 次）"
                        if retry:
                            message += "，稍后自动重试"
                        self.store.db.execute(
                            "UPDATE songs SET prep_state='unavailable',prep_error=?,"
                            "prep_retry_at=? WHERE id=? AND status='waiting'",
                            (message, retry, row["id"]),
                        )
                    else:
                        self.store.db.execute(
                            "UPDATE songs SET prep_state='original',"
                            "prep_error='字幕制作失败' WHERE id=? AND status='ready'",
                            (row["id"],),
                        )
                    self.store.db.commit()
            finally:
                self.busy.clear()

    def prepare_source(self, row):
        song_id = row["id"]
        with tempfile.TemporaryDirectory(
            prefix=f"vc-ktv-{song_id}-", dir=self.store.root
        ) as tmp:
            path = self.download(row["bvid"], row["page"], Path(tmp))
            actual_duration = self.probe(path)
            target = (
                self.store.media
                / f"{song_id}-original-{secrets.token_hex(6)}{path.suffix}"
            )
            shutil.copyfile(path, target)
            with self.store.lock:
                latest = self.store.db.execute(
                    "SELECT status,prep_state FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if (
                    not latest
                    or latest["status"] != "waiting"
                    or latest["prep_state"] != "downloading"
                ):
                    target.unlink(missing_ok=True)
                    return
                self.store.db.execute(
                    """UPDATE songs SET media_file=?,source_file=?,duration=?,status='ready',
                       prep_state='downloaded',prep_error='',prep_retry_at=0 WHERE id=?""",
                    (target.name, target.name, round(actual_duration), song_id),
                )
                self.store.db.commit()
        log.info("Song #%s original available, subtitle enhancement follows", song_id)

    def prepare_subtitles(self, row):
        song_id = row["id"]
        source = self.store.media / Path(row["source_file"] or "").name
        if not source.is_file():
            raise RuntimeError("source file vanished")
        lyrics = row["lyric_choice"]
        if not lyrics:
            return
        # A retry must never reuse old alignment output or overwrite diagnostics.
        # Keep all artifacts scoped to this song/event.
        archive = (
            self.store.media
            / f"{song_id}-auto-alignment"
            / (f"attempt-{time.time_ns()}-{secrets.token_hex(3)}")
        )
        archive.mkdir(parents=True)
        target = self.store.media / f"{song_id}-auto-{secrets.token_hex(6)}.mp4"
        metadata = {
            "song_id": song_id,
            "source_file": row["source_file"],
            "lyric_source": row["lyric_source"],
            "outcome": "running",
        }
        try:
            report = self.enhance(
                source, lyrics, target, archive, float(row["duration"])
            )
            metadata["outcome"] = "rendered"
            metadata["report"] = report
            with self.store.lock:
                latest = self.store.db.execute(
                    "SELECT status,source_file,media_file,prep_state,lyric_choice FROM songs WHERE id=?",
                    (song_id,),
                ).fetchone()
                if (
                    latest
                    and latest["status"] == "ready"
                    and latest["source_file"] == row["source_file"]
                    and latest["prep_state"] == "aligning"
                    and latest["lyric_choice"] == lyrics
                ):
                    self.store.db.execute(
                        "UPDATE songs SET media_file=?,lyrics=?,prep_state='done',prep_error='' WHERE id=?",
                        (target.name, (archive / "aligned.lrc").read_text(), song_id),
                    )
                    self.store.db.commit()
                    if latest["media_file"] != row["source_file"]:
                        (self.store.media / latest["media_file"]).unlink(
                            missing_ok=True
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
                self.store.db.execute(
                    "UPDATE songs SET prep_state='original',prep_error=? "
                    "WHERE id=? AND status='ready' AND prep_state='aligning' "
                    "AND source_file=?",
                    (message, song_id, row["source_file"]),
                )
                self.store.db.commit()
        finally:
            (archive / "attempt.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
