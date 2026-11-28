"""Explicit incremental preparation of outdated, unplayed media generations."""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path

from vc_ktv.core.artifacts import (
    MEDIA_SETTINGS,
    bundle_archive,
    burn_required,
    find_bundle,
    needs_bandwidth,
    source_origin,
    source_reasons,
    source_record,
)
from vc_ktv.infra.disk_budget import check_current_budget
from vc_ktv.infra.media_cleanup import prune_alignment_attempt
from vc_ktv.pipeline import (
    RenderOptions,
    Timeline,
    pipeline_settings,
    probe,
    render,
    write_bundle,
)
from vc_ktv.processing.backing_audio import ready_audio_file, source_identity
from vc_ktv.processing.bitrate import fits_budget, read_source_profile
from vc_ktv.processing.media_preparation import (
    prepare_tv_source,
    render_provider_video,
    render_upstream_video,
    stream_video_kbps,
)
from vc_ktv.processing.media_properties import (
    audio_identity,
    inspect_media,
    known_media,
)
from vc_ktv.processing.publication import retire_render
from vc_ktv.rendering.subtitle_styles import preset_style

log = logging.getLogger(__name__)
LYRIC_INPUTS = (
    "lyrics",
    "lyric_choice",
    "lyric_source",
    "lyric_timing_mode",
    "provider_lrc",
    "source_lrc_raw",
    "lyric_offset_ms",
    "skip_subtitles",
)


def same_soundtrack(old: Path, new: Path, old_props: dict, new_props: dict) -> bool:
    if old == new:
        return True
    if not old_props.get("duration") or not new_props.get("duration"):
        return False
    if abs(old_props["duration"] - new_props["duration"]) > 0.1:
        return False
    if abs(old_props.get("video_start", 0) - new_props.get("video_start", 0)) > 0.02:
        return False
    key = audio_identity(old)
    return bool(key and key == audio_identity(new))


def _new_source(owner, row, old: Path, old_meta: dict, settings: dict, staging: Path):
    """Reuse a compatible local variant, then prefer native provider streams.

    Return source path, measured/provenance record, and whether only the local
    encoding changed (the visual content is unchanged).
    """
    store = owner.store
    old_props = old_meta
    origin = old_meta.get("origin") or source_origin(row, old.name)
    baseline = {**old_meta, **old_props, "origin": origin}
    if not source_reasons(baseline, settings):
        return old, baseline, True
    lineage = old_meta.get("lineage") or old.name
    # A soundtrack lineage is not evidence of identical visuals: provider
    # renditions may differ. Only verified local conversions share this origin.
    visual_origin = old_meta.get("visual_origin") or old.name
    with store.lock:
        variants = store.rows(
            "SELECT filename,metadata FROM media_artifacts WHERE kind='source'"
        )
    for variant in variants:
        meta = json.loads(variant["metadata"])
        candidate = store.media / Path(variant["filename"]).name
        if (
            candidate == old
            or meta.get("lineage") != lineage
            or not candidate.is_file()
        ):
            continue
        if not source_reasons(meta, settings):
            same_visual = (meta.get("visual_origin") or candidate.name) == visual_origin
            return candidate, meta, same_visual

    downloaded = None
    target_height = settings["video_quality"]
    mode = settings["original_codec_mode"]
    quality_change = any(
        reason.startswith("quality") for reason in source_reasons(baseline, settings)
    )
    if origin == "bilibili":
        folder = staging / "download"
        folder.mkdir()
        try:
            downloaded = owner.download_source(
                {**row, **settings, "source_type": "bilibili"}, folder
            )
            candidate_props = inspect_media(downloaded, measure_bitrate=False)
            if not candidate_props.get("codec"):
                raise ValueError("无法验证新原片")
            # No reason to transcode a second lossy representation when the
            # old local source already meets the requested quality.
            if (
                not quality_change
                and mode != "default"
                and candidate_props["codec"] != mode
            ):
                downloaded = None
        except Exception:
            if quality_change:
                raise
            log.info("Native codec variant unavailable; convert existing source")
            downloaded = None
    check_current_budget()
    local = downloaded is None
    if local:
        if (
            origin == "bilibili"
            and old_props.get("height", target_height) > target_height
        ):
            raise ValueError("无法获取符合清晰度要求的原片")
        # normalize_original_codec edits its input in place: never give it a
        # published source, even if the old file has no other references.
        downloaded = staging / ("convert" + old.suffix)
        shutil.copyfile(old, downloaded)
    new = owner.normalize_source(downloaded, settings)
    props = inspect_media(new)
    if not props.get("codec"):
        raise ValueError("无法验证更新后的编码")
    name = f"{row['id']}-original-{secrets.token_hex(6)}{new.suffix}"
    target = staging / name
    new.replace(target)
    equivalent = same_soundtrack(old, target, old_props, props)
    meta = source_record(
        row, settings, props, origin=origin, lineage=lineage if equivalent else name
    )
    same_visual = (
        local and equivalent and props.get("height") == old_props.get("height")
    )
    meta["visual_origin"] = visual_origin if same_visual else name
    if source_reasons(meta, settings):
        raise ValueError("原片未满足当前设置")
    return target, meta, same_visual


def _copy_bundle(old: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for name in (
        "timeline.json",
        "style.json",
        "display_plan.json",
        "lyrics.ass",
        "lyrics.lrc",
        "lyrics.full.txt",
        "unmatched.json",
        "export_report.json",
    ):
        if (old / name).is_file():
            shutil.copyfile(old / name, destination / name)


def _render_from_bundle(source: Path, bundle: Path, target: Path, limit: float) -> None:
    settings = pipeline_settings()
    result = render(
        source,
        bundle,
        probe(source, settings),
        settings,
        RenderOptions(max_video_kbps=stream_video_kbps(limit)),
        "refresh.mp4",
    )
    result.replace(target)


def _reuse_tv_cache(
    old_bundle: Path | None, source: Path, archive: Path, limit: float
) -> bool:
    if old_bundle is None:
        return False
    old_archive = bundle_archive(old_bundle)
    cache = old_archive / "tv-source.mp4"
    try:
        metadata = json.loads((old_archive / "attempt.json").read_text())
        if (
            metadata.get("source_file") != source.name
            or metadata.get("tv_source_ready") is False
            or float(metadata.get("max_bandwidth_mbps", 0)) > limit
            or cache.stat().st_size < 1024
        ):
            return False
    except (OSError, ValueError, TypeError):
        return False
    os.link(cache, archive / "tv-source.mp4")
    return True


def _alias_backing(store, old: Path, new: Path, model: str) -> Path | None:
    old_key = source_identity(old, model)
    new_key = source_identity(new, model)
    if old_key == new_key or ready_audio_file(store.root, new_key):
        return None
    asset = ready_audio_file(store.root, old_key)
    row = store.db.execute(
        "SELECT * FROM backing_tracks WHERE cache_key=? AND status='ready'", (old_key,)
    ).fetchone()
    if asset is None or row is None:
        return None
    target = asset.with_name(new_key + ".m4a")
    if target.exists() or target.is_symlink():
        return None
    os.link(asset, target)
    try:
        store.db.execute(
            """INSERT INTO backing_tracks(cache_key,source_file,model_sha256,status,updated_at,elapsed_seconds)
            VALUES(?,?,?,'ready',?,?) ON CONFLICT(cache_key) DO UPDATE SET status='ready',error='',updated_at=excluded.updated_at,elapsed_seconds=excluded.elapsed_seconds""",
            (new_key, new.name, model, int(time.time()), row["elapsed_seconds"]),
        )
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def execute_refresh(owner, row: dict) -> None:
    store = owner.store
    settings = row["refresh_settings"]
    song_id = row["id"]
    with store.lock:
        resolved = store.replay_assets(row)
        old_source = store.media / Path(resolved.get("source_file") or "").name
        old_media = store.media / Path(resolved.get("media_file") or "").name
        old_source_meta = store.artifact(old_source.name, "source")
        old_render_meta = (
            store.artifact(old_media.name, "render")
            if old_media != old_source
            else old_source_meta
        )
        old_bundle = find_bundle(store, row)
    if not old_source.is_file() or not old_media.is_file():
        raise ValueError("旧媒体缺失，无法增量更新")
    with tempfile.TemporaryDirectory(
        prefix=f"vc-ktv-{song_id}-refresh-", dir=store.root
    ) as temporary:
        staging = Path(temporary)
        # Check actual media before doing expensive work on older records.
        old_props = known_media(old_source, old_source_meta)
        baseline = {**old_source_meta, **old_props}
        if source_reasons(baseline, settings):
            source, source_meta, local_conversion = _new_source(
                owner, row, old_source, baseline, settings, staging
            )
        else:
            source, source_meta, local_conversion = old_source, baseline, True
        source_changed = source != old_source
        source_props = source_meta
        audio_same = not source_changed or same_soundtrack(
            old_source, source, old_props, source_props
        )
        limit = settings["max_bandwidth_mbps"]
        style = settings["subtitle_style"]
        old_render_meta = {
            **old_render_meta,
            **(
                known_media(old_media, old_render_meta)
                if old_media != old_source
                else old_props
            ),
        }
        old_html = bool(row.get("html_only")) and row["prep_state"] == "done"
        html_only = old_html and not burn_required(settings)
        if old_html and old_bundle is not None:
            info = json.loads((bundle_archive(old_bundle) / "attempt.json").read_text())
            old_render_meta = {
                **old_render_meta,
                "subtitle_style": info.get("subtitle_style"),
                "bandwidth": info.get("max_bandwidth_mbps"),
            }
        style_changed = (
            row["prep_state"] == "done"
            and not row["skip_subtitles"]
            and old_render_meta.get("subtitle_style") not in (None, style)
        )
        bandwidth_changed = needs_bandwidth(old_render_meta, settings) or (
            old_html and (old_render_meta.get("bandwidth") or limit) > limit
        )
        auto_subtitles = (
            row["prep_state"] == "done"
            and not row["skip_subtitles"]
            and not old_render_meta.get("manual")
        )
        media = old_media
        render_meta = old_render_meta
        archive = None
        lyrics = row["lyrics"]
        duration = source_props.get("duration") or owner.probe(source)
        if auto_subtitles and (
            style_changed or bandwidth_changed or source_changed or old_html
        ):
            # For a video-only local conversion, the existing burned H.264 is
            # still valid. A new provider representation may require rebaking.
            bake = not html_only and (
                old_html
                or style_changed
                or bandwidth_changed
                or not local_conversion
                or not audio_same
            )
            archive = staging / "attempt"
            archive.mkdir()
            bundle = archive / "pipeline" / "subtitles"
            media_target = staging / f"{song_id}-auto-{secrets.token_hex(6)}.mp4"
            if audio_same and old_bundle is not None:
                if style_changed:
                    timeline = Timeline.model_validate_json(
                        (old_bundle / "timeline.json").read_text()
                    )
                    write_bundle(timeline, preset_style(style), bundle)
                else:
                    _copy_bundle(old_bundle, bundle)
                if bake:
                    _render_from_bundle(source, bundle, media_target, limit)
            else:
                # Genuine soundtrack/timeline changes need a new alignment;
                # a legacy missing timing artifact is the only other fallback.
                if row["lyric_timing_mode"] == "provider":
                    render_provider_video(
                        source,
                        row["provider_lrc"],
                        row["lyric_offset_ms"],
                        media_target,
                        archive,
                        duration,
                        max_bandwidth_mbps=limit,
                        preset=style,
                        **({"burn_subtitles": False} if html_only else {}),
                    )
                else:
                    if not owner.gpu_alignment:
                        raise ValueError("需要重新对齐歌词，但 GPU 对齐未启用")
                    options = {"preset": style}
                    if owner.enhance is render_upstream_video:
                        options["max_bandwidth_mbps"] = limit
                        if html_only:
                            options["burn_subtitles"] = False
                    owner.enhance(
                        source,
                        row["lyric_choice"],
                        media_target,
                        archive,
                        duration,
                        **options,
                    )
                bake = not html_only
                aligned = archive / "aligned.lrc"
                if aligned.is_file():
                    lyrics = aligned.read_text()
            if html_only:
                media = source
                render_meta = source_meta
            elif bake:
                if not media_target.is_file():
                    raise ValueError("更新后的成片缺失")
                media = media_target
                render_meta = {
                    **inspect_media(media),
                    "subtitle_style": style,
                    "bandwidth": limit,
                }
            if (bundle / "lyrics.lrc").is_file():
                (archive / "aligned.lrc").write_text(
                    (bundle / "lyrics.lrc").read_text()
                )
            if not source_changed and old_bundle is not None:
                legacy_profile = read_source_profile(source, bundle_archive(old_bundle))
                if legacy_profile:
                    source_meta = {**legacy_profile, **source_meta}
            if settings["tv_html_overlay"]:
                try:
                    if not (
                        not source_changed
                        and _reuse_tv_cache(old_bundle, source, archive, limit)
                    ):
                        native = (
                            (row["bvid"], int(row["page"]))
                            if source_meta.get("origin") == "bilibili"
                            and settings["original_codec_mode"] == "av1"
                            else None
                        )
                        prepare_tv_source(
                            source,
                            archive,
                            duration,
                            limit,
                            native_av1=native,
                            profile=source_meta,
                        )
                except Exception:
                    if html_only:
                        raise
                    log.warning(
                        "Optional refresh TV cache unavailable for song #%s", song_id
                    )
                    (archive / "tv-source.mp4").unlink(missing_ok=True)
            if (
                html_only
                and not fits_budget(source, duration, limit, source_meta)
                and (
                    not (archive / "tv-source.mp4").is_file()
                    or (archive / "tv-source.mp4").stat().st_size < 1024
                )
            ):
                raise ValueError("电视播放缓存制作失败")
            manifest = {
                "song_id": song_id,
                "outcome": "rendered",
                "source_file": source.name,
                "media_file": media.name,
                "subtitle_style": style,
                "render_bandwidth_mbps": render_meta.get("bandwidth", limit),
                "max_bandwidth_mbps": limit,
                "lyric_timing_mode": row["lyric_timing_mode"],
                "tv_source_ready": (archive / "tv-source.mp4").is_file(),
            }
            (archive / "attempt.json").write_text(
                json.dumps(manifest, ensure_ascii=False)
            )
        elif not auto_subtitles:
            # Raw/manual audio and uploaded visuals remain authoritative. A
            # bitrate-only refresh can encode the existing (even manual-baked)
            # output, not regenerate or realign its lyrics.
            if old_media == old_source or (
                source_changed
                and (
                    old_render_meta.get("raw_playback")
                    or row["prep_state"] == "downloaded"
                )
            ):
                media = source
                render_meta = source_meta
            elif source_changed and not (local_conversion and audio_same):
                raise ValueError("手工成片与新原片不一致，需要手工确认")
            if bandwidth_changed or (
                source_changed and old_render_meta.get("raw_playback")
            ):
                raw = staging / "raw-cache"
                raw.mkdir()
                prepare_tv_source(media, raw, duration, limit, profile=render_meta)
                cached = raw / "tv-source.mp4"
                if cached.is_file():
                    target = staging / f"{song_id}-playback-{secrets.token_hex(6)}.mp4"
                    cached.replace(target)
                    media = target
                    render_meta = {
                        **inspect_media(media),
                        "bandwidth": limit,
                        "manual": bool(old_render_meta.get("manual")),
                        "raw_playback": old_media == old_source
                        or bool(old_render_meta.get("raw_playback")),
                    }
                elif media == source:
                    source_meta = {**source_meta, "bandwidth": limit}
                    render_meta = source_meta
                else:
                    render_meta = {**render_meta, "bandwidth": limit}
        check_current_budget()
        moved: list[Path] = []
        new_archive: Path | None = None
        backing_link: Path | None = None
        with store.lock:
            current_row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            current = dict(current_row) if current_row else {}
            now = store.settings()
            if (
                not current
                or current["status"] != "ready"
                or current["refresh_state"] != "working"
            ):
                return
            latest = store.replay_assets(current)
            if (
                owner.stop_signal.is_set()
                or current["prep_state"] != row["prep_state"]
                or any(current[k] != row[k] for k in LYRIC_INPUTS)
                or latest.get("source_file") != old_source.name
                or latest.get("media_file") != old_media.name
                or any(now[k] != settings[k] for k in MEDIA_SETTINGS)
            ):
                store.db.execute(
                    "UPDATE songs SET refresh_state=?,refresh_error='' WHERE id=?",
                    ("queued" if store.refresh_reasons(current, now) else "", song_id),
                )
                store.db.commit()
                return
            try:
                for file in dict.fromkeys((source, media)):
                    if file.parent == staging:
                        destination = store.media / file.name
                        file.replace(destination)
                        moved.append(destination)
                if archive is not None:
                    new_archive = (
                        Path(store.media)
                        / f"{song_id}-auto-alignment"
                        / f"attempt-{time.time_ns()}-{secrets.token_hex(3)}"
                    )
                    new_archive.parent.mkdir(exist_ok=True)
                    archive.replace(new_archive)
                store.record_artifact(source.name, "source", source_meta)
                if media.name != source.name:
                    store.record_artifact(media.name, "render", render_meta)
                if source_changed and audio_same:
                    backing_link = _alias_backing(
                        store,
                        old_source,
                        store.media / source.name,
                        owner.backing.model_sha256,
                    )
                store.db.execute(
                    "UPDATE songs SET source_file=?,media_file=?,duration=?,lyrics=?,html_only=?,html_attempt=?,refresh_state='',refresh_error='' WHERE id=?",
                    (
                        source.name,
                        media.name,
                        round(duration),
                        lyrics,
                        int(html_only),
                        str(new_archive.relative_to(store.media))
                        if html_only and new_archive
                        else "",
                        song_id,
                    ),
                )
                store.db.commit()
            except BaseException:
                store.db.rollback()
                for file in moved:
                    file.unlink(missing_ok=True)
                if new_archive is not None:
                    shutil.rmtree(new_archive, ignore_errors=True)
                if backing_link is not None:
                    backing_link.unlink(missing_ok=True)
                raise
            # Only remove superseded renders after the new generation commits.
            # Keep old source variants and any media referenced by a replay.
            if (
                old_media != media
                and retire_render(store, old_media.name, old_source.name)
                and old_bundle is not None
            ):
                obsolete = bundle_archive(old_bundle)
                if obsolete.parent.name == f"{song_id}-auto-alignment":
                    try:
                        prune_alignment_attempt(obsolete)
                    except OSError as error:
                        log.warning("Obsolete alignment cleanup failed: %s", error)
