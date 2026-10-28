"""Local best-effort media preparation. No LLM, external agent, or human confirmation.

The original video becomes playable BEFORE slower optional karaoke enhancement.
Source failures never block the fair queue of already playable songs.
"""

from __future__ import annotations

import logging
import os
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
from .karaoke import render_video
from .textgrid_import import convert_textgrid

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


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


def alignment_lyrics(lyrics: str) -> str:
    """Omit non-lexical Latin vocalizations that TIFA cannot pronounce.

    Only remove short interjections before Chinese lyric lines; retain raw source.
    """
    prefix = re.compile(
        r"^(?:(?:ha|ah|aa|la|oh|woo|ooh|wu|na|ya)\b[\s~]*){1,8}(?=[\u3400-\u9fff])",
        re.I,
    )
    lines = [prefix.sub("", line).strip() for line in lyrics.splitlines()]
    return "\n".join(line for line in lines if line)


def make_tifa_video(
    source: Path, lyrics: str, destination: Path, archive: Path, duration: float
) -> dict:
    """Optional best-effort stage; callers must fall back to source on any exception."""
    tifa = ROOT / ".runtime/TIFA"
    python = ROOT / ".runtime/tifa-env/bin/python"
    model = ROOT / ".runtime/models/TIFA-1.0-ST/model.pt"
    if not (python.is_file() and (tifa / "infer.py").is_file() and model.is_file()):
        raise RuntimeError("TIFA 未安装，本次播放原视频")
    archive.mkdir(parents=True, exist_ok=True)
    audio = archive / "audio.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            str(audio),
        ],
        check=True,
        capture_output=True,
        timeout=70,
    )
    normalized = alignment_lyrics(lyrics)
    (archive / "lyrics.source.txt").write_text(lyrics, encoding="utf-8")
    (archive / "audio.txt").write_text(normalized, encoding="utf-8")
    output = archive / "alignment"
    output.mkdir(exist_ok=True)
    cmd = [
        str(python),
        str(tifa / "infer.py"),
        str(audio),
        "-m",
        str(model),
        "-l",
        "zh",
        "-o",
        str(output),
        "--batch-size",
        "1",
        "--num-workers",
        "0",
    ]
    with (archive / "tifa.log").open("w") as trace:
        proc = subprocess.run(
            cmd,
            cwd=tifa,
            stdout=trace,
            stderr=subprocess.STDOUT,
            timeout=360,
            env={**os.environ, "OMP_NUM_THREADS": "4"},
        )
    if proc.returncode:
        raise RuntimeError("TIFA 未能完成对齐")
    grid = output / "audio.TextGrid"
    if not grid.is_file():
        raise RuntimeError("TIFA 未返回 TextGrid")
    lrc, report = convert_textgrid(grid, normalized, duration)
    (archive / "aligned.lrc").write_text(lrc, encoding="utf-8")
    render_video(source, lrc, destination, duration, lead=1.8)
    return report


class AutoPrep:
    def __init__(
        self,
        store,
        probe,
        download=media_download,
        enhance=make_tifa_video,
    ):
        self.store = store
        self.probe = probe
        self.download = download
        self.enhance = enhance
        self.stop_signal = threading.Event()
        self.worker: threading.Thread | None = None

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
            self.worker.join(timeout=1)

    def claim(self):
        with self.store.lock:
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
                            "prep_error='字幕处理失败，原视频仍可播放' WHERE id=? AND status='ready'",
                            (row["id"],),
                        )
                    self.store.db.commit()

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
        archive = self.store.media / f"{song_id}-auto-alignment"
        target = self.store.media / f"{song_id}-auto-{secrets.token_hex(6)}.mp4"
        try:
            self.enhance(source, lyrics, target, archive, float(row["duration"]))
            with self.store.lock:
                latest = self.store.db.execute(
                    "SELECT status,source_file,media_file FROM songs WHERE id=?",
                    (song_id,),
                ).fetchone()
                if (
                    latest
                    and latest["status"] == "ready"
                    and latest["source_file"] == row["source_file"]
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
                    target.unlink(
                        missing_ok=True
                    )  # playback has started: keep original
        except Exception as error:
            log.warning(
                "Song #%s subtitle enhancement failed, original preserved: %s",
                song_id,
                error,
            )
            target.unlink(missing_ok=True)
            with self.store.lock:
                self.store.db.execute(
                    "UPDATE songs SET prep_state='original',prep_error='字幕准备失败，播放原视频' WHERE id=?",
                    (song_id,),
                )
                self.store.db.commit()
