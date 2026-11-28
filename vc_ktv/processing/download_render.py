"""Generate optional download-only MP4s without changing playback state."""

import json
import shutil
import tempfile
from pathlib import Path

from vc_ktv.core.artifacts import bundle_archive, find_bundle
from vc_ktv.infra.disk_budget import check_current_budget
from vc_ktv.processing.refresh import _render_from_bundle


def execute_download_render(owner, row):
    store = owner.store
    attempt = row["html_attempt"]
    with store.lock:
        bundle = find_bundle(store, row)
        if bundle is None:
            raise ValueError("subtitle generation unavailable")
        archive = bundle_archive(bundle)
        if str(archive.relative_to(store.media)) != attempt:
            raise ValueError("subtitle generation changed")
        source = store.media / Path(store.replay_assets(row)["source_file"]).name
        limit = float(
            json.loads((archive / "attempt.json").read_text())["max_bandwidth_mbps"]
        )
    with tempfile.TemporaryDirectory(
        prefix="vc-ktv-download-", dir=store.root
    ) as folder:
        staging = Path(folder)
        timing = staging / "subtitles"
        shutil.copytree(bundle, timing)
        target = staging / "download.mp4"
        _render_from_bundle(source, timing, target, limit)
        check_current_budget()
        if not target.is_file() or target.stat().st_size == 0:
            raise ValueError("download render missing")
        with store.lock:
            if owner.stop_signal.is_set():
                raise RuntimeError("worker stopping")
            # A replay can still reference this generation after the
            # original song is refreshed. Never attach to a new generation.
            if not store.db.execute(
                "SELECT 1 FROM songs WHERE html_attempt=? AND html_only=1 AND prep_state='done' AND status IN ('ready','playing','played','skipped') LIMIT 1",
                (attempt,),
            ).fetchone():
                raise ValueError("subtitle generation retired")
            record = store.db.execute(
                "SELECT status FROM download_renders WHERE attempt=?", (attempt,)
            ).fetchone()
            if not record or record["status"] != "working":
                return
            destination = archive / "download.mp4"
            target.replace(destination)
            try:
                store.db.execute(
                    "UPDATE download_renders SET status='ready' WHERE attempt=?",
                    (attempt,),
                )
                store.db.commit()
            except BaseException:
                store.db.rollback()
                destination.unlink(missing_ok=True)
                raise
