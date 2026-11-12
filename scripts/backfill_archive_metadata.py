"""Backfill closed-session metadata and durable cover images without rendering videos."""

import argparse
import asyncio
import re
import sqlite3
from pathlib import Path

import httpx
from fastapi import HTTPException

from vc_ktv.infra.cover_cache import (
    cover_file,
    download_cover,
    normalize_existing_cover,
)
from vc_ktv.sources.bilibili import bilibili_lookup
from vc_ktv.sources.netease_catalog import lookup_netease_song

EVENT_RE = re.compile(r"^[a-zA-Z0-9_-]{1,60}$")


async def backfill(folder: Path, apply: bool, normalize_existing: bool = False):
    import json

    info = json.loads((folder / "info.json").read_text())
    if info.get("id") != folder.name or info.get("status") != "closed":
        raise ValueError(f"Not a closed event: {folder.name}")
    if folder.is_symlink():
        raise ValueError("Symlinked event directory")
    db_path = folder / "room.sqlite3"
    with sqlite3.connect(db_path, timeout=15) as db:
        db.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT id,bvid,page,source_type,source_id,title,uploader,cover,duration FROM songs ORDER BY id"
            )
        ]
        covers = folder / "media" / "covers"
        missing = sum(not cover_file(covers, r["id"]) for r in rows)
        print(
            f"{folder.name}: {len(rows)} songs, {missing} uncached covers", flush=True
        )
        if not apply:
            return
        sem = asyncio.Semaphore(3)
        fetched = {}
        counts = {"images": 0, "metadata": 0, "resized": 0, "failed": 0}

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(14, connect=5),
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 VC-KTV/0.5"},
        ) as client:

            async def enrich(row):
                async with sem:
                    cover = row["cover"] or ""
                    uploader = row["uploader"] or ""
                    duration = row["duration"]
                    changed = False
                    needs_meta = not cover or not uploader or duration <= 0
                    if needs_meta:
                        key = (
                            row["source_type"],
                            row["source_id"] or row["bvid"],
                            row["page"],
                        )
                        try:
                            if key in fetched:
                                meta = fetched[key]
                            elif row["source_type"] == "bilibili" and row["bvid"]:
                                meta = await bilibili_lookup(row["bvid"], row["page"])
                                fetched[key] = meta
                            elif (
                                row["source_type"] == "netease"
                                and row["source_id"].isdigit()
                            ):
                                meta = await lookup_netease_song(int(row["source_id"]))
                                fetched[key] = meta
                            else:
                                meta = {}
                            if meta:
                                counts["metadata"] += 1
                                cover = cover or meta.get("cover", "")
                                uploader = uploader or meta.get("uploader", "")
                                duration = (
                                    duration
                                    if duration > 0
                                    else meta.get("duration", 0)
                                )
                                changed = True
                        except (
                            HTTPException,
                            httpx.HTTPError,
                            LookupError,
                            OSError,
                            ValueError,
                        ) as exc:
                            print(
                                f"  metadata #{row['id']}: {type(exc).__name__}",
                                flush=True,
                            )
                            counts["failed"] += 1
                        await asyncio.sleep(0.25)
                    prior = cover_file(covers, row["id"])
                    if prior and normalize_existing:
                        if await asyncio.to_thread(normalize_existing_cover, prior):
                            counts["resized"] += 1
                        else:
                            print(f"  image #{row['id']}: cannot normalize", flush=True)
                            counts["failed"] += 1
                    elif cover and not prior:
                        if await download_cover(client, cover, covers, row["id"]):
                            counts["images"] += 1
                        else:
                            print(f"  image #{row['id']}: unavailable", flush=True)
                            counts["failed"] += 1
                    return row["id"], changed, uploader, cover, duration

            # Each event is a bounded batch; DB writes only update missing metadata.
            for i in range(0, len(rows), 20):
                batch = await asyncio.gather(*(enrich(row) for row in rows[i : i + 20]))
                for song_id, changed, uploader, cover, duration in batch:
                    if changed:
                        db.execute(
                            """UPDATE songs SET uploader=CASE WHEN uploader='' THEN ? ELSE uploader END,
                               cover=CASE WHEN cover='' THEN ? ELSE cover END,
                               duration=CASE WHEN duration<=0 THEN ? ELSE duration END
                               WHERE id=?""",
                            (uploader, cover, duration, song_id),
                        )
                db.commit()
                print(f"  {min(i + 20, len(rows))}/{len(rows)} scanned", flush=True)
        print(
            f"  images={counts['images']} metadata={counts['metadata']} resized={counts['resized']} failures={counts['failed']}",
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_ids", nargs="+")
    parser.add_argument(
        "--root", type=Path, default=Path("~/.local/share/vc-ktv").expanduser()
    )
    parser.add_argument(
        "--apply", action="store_true", help="Fetch and persist metadata and images"
    )
    parser.add_argument(
        "--normalize-existing",
        action="store_true",
        help="Re-encode existing covers within 720x720 and 256 KiB (requires --apply)",
    )
    args = parser.parse_args()
    if args.normalize_existing and not args.apply:
        parser.error("--normalize-existing requires --apply")
    for event_id in args.event_ids:
        if not EVENT_RE.fullmatch(event_id):
            parser.error(f"Invalid event id: {event_id}")
        asyncio.run(
            backfill(
                args.root / "sessions" / event_id, args.apply, args.normalize_existing
            )
        )


if __name__ == "__main__":
    main()
