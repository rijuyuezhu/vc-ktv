"""Diagnostic replay-lyrics regression: Issue #450, isolated from real sessions."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from vc_ktv.application.factory import create_app
from vc_ktv.lyrics.lyric_selection import _candidate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-fixed",
        action="store_true",
        help="Fail if changing lyrics on a virtual replay is not yet supported",
    )
    parser.add_argument("--output", type=Path, default=Path("benchmarks/.results"))
    args = parser.parse_args()
    with TemporaryDirectory(prefix="ktv-replay-lyrics-") as temp:
        app = create_app(
            root=Path(temp) / "data",
            password="fixture-password",
            secret=b"replay-lyrics-fixture-secret",
            auto_prepare=False,
            gpu_alignment=True,
        )
        app.state.events.create("Replay regression", "123456")
        store = app.state.events.active()
        with store.lock:
            store.db.execute(
                "INSERT INTO room_state(name,value) VALUES('playback_mode','browser') "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value"
            )
            store.db.commit()

        async def lookup(bvid, page):
            return dict(
                bvid=bvid,
                page=page,
                title="Synthetic replay song",
                uploader="Test uploader",
                duration=180,
                cover="",
            )

        async def candidates(term):
            return [
                _candidate(
                    term,
                    "网易云音乐",
                    "[00:04.00]第一句测试歌词\n[00:14.00]第二句测试歌词",
                    "https://music.163.com/",
                )
            ]

        app.state.lookup = lookup
        app.state.candidate_search = candidates
        with TestClient(app) as guest, TestClient(app) as admin:
            assert guest.post("/api/join", json={"code": "123456"}).status_code == 200
            identity = dict(
                username="Fixture Singer",
                password="fixture-login",
                confirm_password="fixture-login",
            )
            assert guest.post("/api/users/register", json=identity).status_code == 200
            assert (
                admin.post(
                    "/api/admin/login", json={"password": "fixture-password"}
                ).status_code
                == 200
            )
            request = guest.post("/api/songs", json={"bvid": "BV0000000001"})
            assert request.status_code == 200
            song_id = request.json()["id"]
            assert guest.post(f"/api/songs/{song_id}/lyrics/skip").status_code == 200
            (store.media / "source.mp4").write_bytes(b"synthetic" * 2000)
            with store.lock:
                store.db.execute(
                    "UPDATE songs SET status='ready',prep_state='original',"
                    "media_file='source.mp4',source_file='source.mp4' WHERE id=?",
                    (song_id,),
                )
                store.db.commit()
            assert admin.post("/api/admin/next").json()["active"]["id"] == song_id
            assert (
                admin.post("/api/admin/finish", json={"song_id": song_id}).status_code
                == 200
            )
            replay = guest.post(f"/api/songs/{song_id}/replay")
            assert replay.status_code == 200
            replay_id = replay.json()["id"]
            with store.lock:
                row = dict(
                    store.db.execute(
                        "SELECT * FROM songs WHERE id=?", (replay_id,)
                    ).fetchone()
                )
            assert store.replay_assets(row)["source_file"] == "source.mp4"
            skipped = guest.post(f"/api/songs/{replay_id}/lyrics/skip")
            signed = guest.get(f"/api/songs/{replay_id}/lyrics/candidates")
            assert signed.status_code == 200
            ticket = signed.json()["candidates"][0]
            selected = guest.post(
                f"/api/songs/{replay_id}/lyrics/select",
                json={
                    "source": ticket["source"],
                    "text": ticket["text"],
                    "ticket": ticket["ticket"],
                    "timing_mode": "provider",
                    "timed_lrc": ticket["timed_lrc"],
                    "source_lrc_raw": ticket["source_lrc_raw"],
                    "timing_ticket": ticket["timing_ticket"],
                },
            )
            with store.lock:
                after = dict(
                    store.db.execute(
                        "SELECT * FROM songs WHERE id=?", (replay_id,)
                    ).fetchone()
                )
            bug_present = skipped.status_code != 200 or (
                selected.status_code == 200
                and after["prep_state"] == "downloaded"
                and not after["source_file"]
            )
            result = {
                "issue": 450,
                "bug_present": bug_present,
                "skip_status": skipped.status_code,
                "selection_status": selected.status_code,
                "replay_prep_state": after["prep_state"],
                "source_inherited": bool(store.replay_assets(after)["source_file"]),
            }
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / "replay-lyrics.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            )
            print("REPLAY_LYRICS", json.dumps(result, ensure_ascii=False), flush=True)
            if args.require_fixed:
                assert not bug_present, (
                    "virtual replay lyric edit is still broken (Issue #450)"
                )


if __name__ == "__main__":
    main()
