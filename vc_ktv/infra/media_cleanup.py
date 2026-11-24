"""Reclaim alignment scratch while retaining final subtitle metadata."""

import re
import shutil
from pathlib import Path


def _remove_intermediate(path: Path) -> int:
    """Remove a generated artifact without following symlinks."""
    if path.is_symlink():
        path.unlink()
        return 0
    if path.is_dir():
        size = sum(
            file.stat().st_size
            for file in path.rglob("*")
            if file.is_file() and not file.is_symlink()
        )
        shutil.rmtree(path)
        return size
    if path.is_file():
        size = path.stat().st_size
        path.unlink()
        return size
    return 0


def prune_alignment_attempt(attempt: Path) -> int:
    """Keep final subtitles, lyrics, manifest and in-flight media hardlinks."""
    if attempt.is_symlink() or not attempt.is_dir():
        return 0
    freed = 0
    for item in attempt.iterdir():
        if item.name in {
            "attempt.json",
            "aligned.lrc",
            "source-provider.lrc",
            "confirmed_lyrics.txt",
            "provider_subtitles",
        } or item.name.startswith(".vc-ktv-inflight-"):
            continue
        if item.name == "pipeline" and item.is_dir() and not item.is_symlink():
            for stage in item.iterdir():
                if stage.name in {"subtitles", "confirmed_lyrics.txt", "lyrics.lrc"}:
                    continue
                freed += _remove_intermediate(stage)
        else:
            freed += _remove_intermediate(item)
    return freed


def prune_alignment_intermediates(media: Path) -> int:
    freed = 0
    for archive in media.iterdir():
        if (
            not re.fullmatch(r"[0-9]+-auto-alignment", archive.name)
            or archive.is_symlink()
            or not archive.is_dir()
        ):
            continue
        for attempt in archive.iterdir():
            if attempt.name.startswith("attempt-"):
                freed += prune_alignment_attempt(attempt)
    return freed
