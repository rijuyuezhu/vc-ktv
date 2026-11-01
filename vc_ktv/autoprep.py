"""Local best-effort media preparation. No LLM, external agent, or human confirmation.

The original video is downloadable before enhancement but is playable only
when the active event allows unsubtitled media.
Source failures never block the fair queue of already playable songs.
"""

from __future__ import annotations

import json
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

from ktv_app.media import probe
from ktv_app.rendering import RenderOptions, capped_h264_args, render
from ktv_app.subtitles import write_bundle
from ktv_app.timeline import Line, Timeline, Unit

from .bilibili_media import QUALITY_HEIGHTS, public_media_download
from .karaoke import parse_lrc
from .subtitle_styles import DEFAULT_PRESET, StylePreset, preset_style
from .timed_lyrics import offset_timeline
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
    enclosed = [x.strip() for x in re.findall(r"《([^《》]{1,48})》", name)]
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


def media_download(
    bvid: str, page: int, directory: Path, *, max_height: int = 720
) -> Path:
    if max_height not in QUALITY_HEIGHTS:
        raise ValueError("不支持的清晰度")
    output = directory / "source.%(ext)s"
    args = [
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
    ]
    # Try higher quality first. If the stream itself fails, retry a lower
    # ceiling, not merely a lower format when a format is unavailable.
    for height in (height for height in QUALITY_HEIGHTS if height <= max_height):
        for old in directory.glob("source.*"):
            old.unlink(missing_ok=True)
        selection = (
            f"bv[height<={height}][ext=mp4]+ba[ext=m4a]/"
            f"bv[height<={height}]+ba/best[height<={height}]"
        )
        result = subprocess.run(
            [
                *args,
                "-f",
                selection,
                "--merge-output-format",
                "mp4",
                "-o",
                str(output),
                f"https://www.bilibili.com/video/{bvid}/?p={page}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            log.warning(
                "yt-dlp <=%sp failed for %s p%s: %s",
                height,
                bvid,
                page,
                (result.stderr[-300:] or result.stdout[-300:]).strip(),
            )
            continue
        matches = [
            p
            for p in directory.glob("source.*")
            if p.suffix in (".mp4", ".webm", ".m4v")
        ]
        if len(matches) == 1 and 0 < matches[0].stat().st_size <= 512 * 1024 * 1024:
            return matches[0]
        log.warning("yt-dlp <=%sp yielded no valid media for %s", height, bvid)
    log.warning(
        "yt-dlp exhausted quality fallbacks for %s p%s; using public API", bvid, page
    )
    return public_media_download(bvid, page, directory, max_height=max_height)


def prepare_tv_source(
    source: Path, archive: Path, duration: float, bandwidth_mbps: float
) -> None:
    """Only transcode high-average-bitrate original video into a private TV cache."""
    if (
        duration <= 0
        or source.stat().st_size * 8 <= duration * bandwidth_mbps * 800_000
    ):
        return
    destination = archive / "tv-source.mp4"
    temporary = archive / "tv-source.tmp.mp4"
    try:
        subprocess.run(
            [
                os.environ.get("KTV_FFMPEG", "ffmpeg"),
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-map_metadata",
                "0",
                "-vf",
                "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:a",
                "copy",
                *capped_h264_args(stream_video_kbps(bandwidth_mbps)),
                str(temporary),
            ],
            check=True,
            timeout=1800,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if temporary.stat().st_size < 1024:
            raise ValueError("invalid compressed source")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def stream_video_kbps(bandwidth_mbps: float) -> int:
    """Reserve 384 kbps for copied audio and MP4 overhead."""
    return max(300, round(bandwidth_mbps * 1000) - 384)


def render_upstream_video(
    source: Path,
    lyrics: str,
    destination: Path,
    archive: Path,
    duration: float,
    *,
    preset: StylePreset = DEFAULT_PRESET,
    max_bandwidth_mbps: float = 3.0,
    progress=None,
) -> dict:
    """Run the selected alignment pipeline; no implicit fallback or retiming."""
    # AutoPrep owns a new attempt directory; the pipeline owns its own
    # isolated child directory and does not mutate the original source.
    options = {
        "style": preset_style(preset),
        "render_options": RenderOptions(
            max_video_kbps=stream_video_kbps(max_bandwidth_mbps),
        ),
    }
    options["mode"] = os.environ.get("VC_KTV_PIPELINE_MODE", "quality")
    if progress is not None:
        options["progress"] = progress
    result = make_karaoke_video(
        source, lyrics, archive / "pipeline", spark_settings(), **options
    )
    shutil.copyfile(result.video, destination)
    shutil.copyfile(result.ass.parent / "lyrics.lrc", archive / "aligned.lrc")
    return json.loads(result.report.read_text(encoding="utf-8"))


def render_provider_video(
    source: Path,
    timed_lrc: str,
    offset_ms: int,
    destination: Path,
    archive: Path,
    duration: float,
    *,
    max_bandwidth_mbps: float = 3.0,
    preset: StylePreset = DEFAULT_PRESET,
) -> dict:
    """Render explicitly chosen line timestamps, without loading GPU models."""
    archive.mkdir(exist_ok=True, parents=True)
    shifted = offset_timeline(timed_lrc, offset_ms, duration)
    (archive / "aligned.lrc").write_text(shifted, encoding="utf-8")
    (archive / "provider_timing.json").write_text(
        json.dumps(
            {
                "mode": "provider_line_only",
                "offset_ms": offset_ms,
                "verified_against_audio": False,
                "not_word_aligned": True,
                "character_timing": "equal_split_estimate",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # The provider gives line starts only; synthesize an evenly timed sweep.
    rows = parse_lrc(shifted)
    lines = []
    for i, row in enumerate(rows):
        start = row["start"]
        next_start = rows[i + 1]["start"] if i + 1 < len(rows) else duration
        end = min(next_start, start + 8)
        text = row["text"]
        lines.append(
            Line(
                id=f"provider-{i}",
                text=text,
                start=start,
                end=end,
                completion_note="逐字渐变按来源行时长等分估算，未经过声学验证",
                units=[
                    Unit(
                        id=f"provider-{i}-{j}",
                        text=char,
                        source_start=j,
                        source_end=j + 1,
                        start=start + (end - start) * j / len(text),
                        end=start + (end - start) * (j + 1) / len(text),
                        timing_source="estimated",
                    )
                    for j, char in enumerate(text)
                ],
            )
        )
    bundle = archive / "provider_subtitles"
    write_bundle(
        Timeline(
            duration=duration,
            model="provider_line_only",
            lines=lines,
            diagnostics={
                "timing_source": "provider_lrc",
                "offset_ms": offset_ms,
                "verified_against_audio": False,
                "character_timing": "equal_split_estimate",
            },
        ),
        preset_style(preset),
        bundle,
    )
    options = RenderOptions(
        max_video_kbps=stream_video_kbps(max_bandwidth_mbps),
    )
    # Reuse the same audio-preserving encoder, style/bitrate and container as
    # the full-quality route; only the model alignment stages are skipped.
    video = render(
        source,
        bundle,
        probe(source, spark_settings()),
        spark_settings(),
        options,
        "karaoke.mp4",
    )
    shutil.copyfile(video, destination)
    return {
        "mode": "provider_line_only",
        "lines": len(rows),
        "offset_ms": offset_ms,
        "verified_against_audio": False,
        "character_timing": "equal_split_estimate",
    }


class AutoPrep:
    def __init__(
        self,
        store,
        probe,
        download=media_download,
        enhance=render_upstream_video,
        max_workers: int | None = None,
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
        self.store = store
        self.probe = probe
        self.download = download
        self.enhance = enhance
        self.stop_signal = threading.Event()
        self.worker: threading.Thread | None = None
        self.workers: list[threading.Thread] = []
        self.busy = threading.Event()
        self.active_claims = 0

    def start(self):
        with self.store.lock:
            # Recover unfinished tasks after a process restart.
            self.store.db.execute(
                "UPDATE songs SET prep_state='pending',prep_stage='' WHERE prep_state='downloading' AND status='waiting'"
            )
            self.store.db.execute(
                "UPDATE songs SET prep_state='downloaded',prep_stage='queued' WHERE prep_state='aligning' AND status='ready'"
            )
            self.store.db.commit()
        self.workers = [
            threading.Thread(
                target=self.loop, name=f"vc-ktv-auto-prep-{index + 1}", daemon=True
            )
            for index in range(self.max_workers)
        ]
        self.worker = self.workers[0]  # Existing lifecycle compatibility.
        for thread in self.workers:
            thread.start()

    def stop(self):
        self.stop_signal.set()
        for thread in self.workers:
            thread.join(timeout=2)
        return all(not thread.is_alive() for thread in self.workers)

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
                    "prep_error='',prep_stage='download',prep_stage_at=?,"
                    "prep_timing='{}' WHERE id=?",
                    (int(time.time()), row["id"]),
                )
            else:
                row = self.store.db.execute(
                    """SELECT * FROM songs WHERE status='ready'
                       AND prep_state='downloaded' AND lyric_choice != ''
                       AND skip_subtitles=0
                       ORDER BY requested_at,id LIMIT 1"""
                ).fetchone()
                if row:
                    self.store.db.execute(
                        "UPDATE songs SET prep_state='aligning',"
                        "prep_stage='queued',prep_stage_at=? WHERE id=?",
                        (int(time.time()), row["id"]),
                    )
            self.store.db.commit()
            if row:
                self.active_claims += 1
                self.busy.set()
                claimed = dict(row)
                if row["status"] == "waiting":
                    claimed["video_quality"] = self.store.settings()["video_quality"]
                if row["status"] == "ready":
                    # Snapshot under the Store lock at subtitle job claim.
                    # Later admin changes affect only subsequent jobs.
                    settings = self.store.settings()
                    claimed["style_preset"] = settings["subtitle_style"]
                    claimed["max_bandwidth_mbps"] = settings["max_bandwidth_mbps"]
                    claimed["tv_html_overlay"] = settings["tv_html_overlay"]
                return claimed
            return None

    def loop(self):
        while not self.stop_signal.is_set():
            row = self.claim()
            if not row:
                self.stop_signal.wait(1)
                continue
            attempt_started = time.monotonic()
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
            finally:
                # Other workers may still be processing a different song.
                # Account for every claim under the same lock as claim().
                with self.store.lock:
                    self.active_claims -= 1
                    if self.active_claims == 0:
                        self.busy.clear()

    def prepare_source(self, row):
        song_id = row["id"]
        started = time.monotonic()
        with tempfile.TemporaryDirectory(
            prefix=f"vc-ktv-{song_id}-", dir=self.store.root
        ) as tmp:
            if self.download is media_download:
                path = self.download(
                    row["bvid"],
                    row["page"],
                    Path(tmp),
                    max_height=row["video_quality"],
                )
            else:
                path = self.download(row["bvid"], row["page"], Path(tmp))
            actual_duration = self.probe(path)
            target = (
                self.store.media
                / f"{song_id}-original-{secrets.token_hex(6)}{path.suffix}"
            )
            shutil.copyfile(path, target)
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
                    target.unlink(missing_ok=True)
                    return
                self.store.db.execute(
                    """UPDATE songs SET media_file=?,source_file=?,duration=?,status='ready',
                       prep_state=?,prep_stage=?,prep_stage_at=?,prep_timing=?,
                       prep_error='',prep_retry_at=0 WHERE id=?""",
                    (
                        target.name,
                        target.name,
                        round(actual_duration),
                        "original" if latest["skip_subtitles"] else "downloaded",
                        "skipped" if latest["skip_subtitles"] else "downloaded",
                        int(time.time()),
                        json.dumps({"download": round(time.monotonic() - started, 2)}),
                        song_id,
                    ),
                )
                self.store.db.commit()
        log.info("Song #%s original available, subtitle enhancement follows", song_id)

    def prepare_subtitles(self, row):
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
            with self.store.lock:
                self.store.db.execute(
                    "UPDATE songs SET prep_timing=? WHERE id=? AND status='ready'",
                    (json.dumps(stages), song_id),
                )
                self.store.db.commit()
            (archive / "attempt.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
