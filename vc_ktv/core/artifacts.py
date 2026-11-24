"""Small, event-local provenance records for published media artifacts."""

from __future__ import annotations

import json
from pathlib import Path

MEDIA_SETTINGS = (
    "original_codec_mode",
    "video_quality",
    "subtitle_style",
    "max_bandwidth_mbps",
)


def source_origin(song: dict, filename: str) -> str:
    if song.get("source_type") == "netease":
        return "netease" if "-original-" in filename else "upload"
    if "-original-" in filename:
        return "bilibili"
    return "upload"


def source_record(
    song: dict, settings: dict, properties: dict, *, origin: str, lineage: str = ""
) -> dict:
    return {
        **properties,
        "origin": origin,
        "lineage": lineage,
        "requested_codec": settings["original_codec_mode"],
        "quality_ceiling": settings["video_quality"] if origin == "bilibili" else None,
        "bandwidth": settings["max_bandwidth_mbps"],
    }


def source_reasons(meta: dict, settings: dict) -> list[str]:
    if not meta:
        return []  # Unmodified pre-provenance events retain their existing behavior.
    reasons = []
    target = settings["original_codec_mode"]
    codec = meta.get("codec")
    if codec:
        if (target == "default" and codec == "hevc") or (
            target != "default" and codec != target
        ):
            reasons.append("codec")
    elif target != "default" and target != meta.get("requested_codec"):
        reasons.append("codec_unknown")
    if meta.get("origin") == "bilibili":
        ceiling = meta.get("quality_ceiling")
        height = meta.get("height")
        target_height = settings["video_quality"]
        if height is not None:
            if height > target_height or (
                height < target_height
                and ceiling is not None
                and ceiling < target_height
            ):
                reasons.append("quality")
        elif ceiling is not None and ceiling != target_height:
            reasons.append("quality_unknown")
    return reasons


def meets_measured_budget(
    meta: dict,
    limit: float,
    *,
    headroom: float = 1.0,
    allow_unknown_peaks: bool = False,
) -> bool:
    """Shared playback budget; original passthrough reserves extra headroom."""
    unknown = 0.0 if allow_unknown_peaks else float("inf")
    return (
        meta.get("average_mbps", float("inf")) <= limit * headroom
        and meta.get("peak_1s_mbps", unknown) <= limit * 1.5
        and meta.get("peak_5s_mbps", unknown) <= limit
    )


def needs_bandwidth(meta: dict, settings: dict) -> bool:
    limit = settings["max_bandwidth_mbps"]
    prior = meta.get("bandwidth")
    return (
        prior is not None and prior > limit and not meets_measured_budget(meta, limit)
    )


def find_bundle(store, song: dict) -> Path | None:
    """Find the committed timing/presentation bundle for the effective assets."""
    if song.get("prep_state") != "done" or song.get("skip_subtitles"):
        return None
    resolved = store.replay_assets(song)
    if not resolved.get("source_file") or not resolved.get("media_file"):
        return None
    source = store.media / Path(resolved["source_file"]).name
    video = store.media / Path(resolved["media_file"]).name
    for ancestor in store.replay_chain(song):
        for field, actual in (("source_file", source), ("media_file", video)):
            path = ancestor.get(field)
            if path:
                try:
                    if not (store.media / Path(path).name).samefile(actual):
                        return None
                except OSError:
                    return None
        if ancestor["id"] != song["id"] and any(
            ancestor.get(field) != song.get(field)
            for field in ("lyrics", "lyric_choice", "lyric_offset_ms")
        ):
            return None
        root = store.media / f"{ancestor['id']}-auto-alignment"
        for attempt in sorted(root.glob("attempt-*/attempt.json"), reverse=True):
            try:
                info = json.loads(attempt.read_text(encoding="utf-8"))
                if info.get("outcome") != "rendered" or info.get("source_file") not in (
                    resolved["source_file"],
                    ancestor.get("source_file"),
                ):
                    continue
                if info.get("media_file") and info["media_file"] not in (
                    resolved["media_file"],
                    ancestor.get("media_file"),
                ):
                    continue
                for name in ("pipeline/subtitles", "provider_subtitles"):
                    folder = attempt.parent / name
                    if all(
                        (folder / f).is_file()
                        for f in ("timeline.json", "display_plan.json", "style.json")
                    ):
                        return folder
            except (OSError, ValueError, TypeError):
                continue
    return None


def bundle_archive(bundle: Path) -> Path:
    return bundle.parent.parent if bundle.name == "subtitles" else bundle.parent


def freshness(store, song: dict, settings: dict) -> list[str]:
    if song.get("status") != "ready":
        return []
    resolved = (
        store.replay_assets(song)
        if not song.get("source_file") or not song.get("media_file")
        else song
    )
    source_name, media_name = resolved.get("source_file"), resolved.get("media_file")
    if not source_name:
        return []
    source = store.artifact(source_name, "source")
    reasons = source_reasons(source, settings)
    output = (
        store.artifact(media_name, "render")
        if media_name and media_name != source_name
        else source
    )
    if (
        song.get("prep_state") == "done"
        and not song.get("skip_subtitles")
        and (
            output.get("subtitle_style")
            and output["subtitle_style"] != settings["subtitle_style"]
        )
    ):
        reasons.append("style")
    if needs_bandwidth(output, settings):
        reasons.append("bandwidth")
    return reasons


def remember_legacy(store, settings: dict) -> None:
    """Baseline pre-provenance files before relevant settings change.

    Unknown codec/height are not invented; a later incompatible setting asks
    for inspection. Already known artifact records are never overwritten.
    """
    for song in store.rows(
        "SELECT * FROM songs WHERE source_file IS NOT NULL OR media_file IS NOT NULL"
    ):
        src = song.get("source_file")
        prior = store.artifact(src, "source") if src else {}
        if src and not prior.get("origin"):
            origin = source_origin(song, src)
            store.record_artifact(
                src,
                "source",
                source_record(song, settings, prior, origin=origin, lineage=src),
            )
        media = song.get("media_file")
        if not media or media == src or store.artifact(media, "render"):
            continue
        info = {"bandwidth": settings["max_bandwidth_mbps"]}
        if song.get("prep_state") == "done" and not song.get("skip_subtitles"):
            info["subtitle_style"] = settings["subtitle_style"]
            bundle = find_bundle(store, song)
            if bundle:
                try:
                    manifest = json.loads(
                        (bundle_archive(bundle) / "attempt.json").read_text()
                    )
                    info["subtitle_style"] = manifest.get(
                        "subtitle_style", info["subtitle_style"]
                    )
                    info["bandwidth"] = manifest.get(
                        "render_bandwidth_mbps",
                        manifest.get("max_bandwidth_mbps", info["bandwidth"]),
                    )
                except (OSError, ValueError, TypeError):
                    pass
        store.record_artifact(media, "render", info)
