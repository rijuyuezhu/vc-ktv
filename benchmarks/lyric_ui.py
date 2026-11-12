"""Check timed-lyric selection and boundary recovery in isolated mobile browsers."""

import argparse
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import uvicorn

import vc_ktv.api.lyrics as lyrics_api
from vc_ktv.application.factory import create_app
from vc_ktv.lyrics.lyric_selection import _candidate
from vc_ktv.lyrics.timed_lyrics import offset_timeline

raw = """[00:00.10]作词：一位作者
[00:01.00]第一句歌词欢迎大家
[00:05.20]第二句歌词跟着节奏
[00:11.40]第三句歌词留在这里
[00:16.90]第四句歌词唱到这里
[00:24.50]第五句歌词一起快乐"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offsets",
        default="0.3,-0.3,-100,1.5",
        help="Comma-separated signed offsets in seconds",
    )
    parser.add_argument(
        "--invalid-offset",
        default="-100",
        help="Offset expected to be rejected before retrying with zero",
    )
    parser.add_argument(
        "--widths", default="320,390", help="Comma-separated mobile viewport widths"
    )
    parser.add_argument("--no-prune-line", action="store_false", dest="prune_line")
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    offsets = [x.strip() for x in args.offsets.split(",") if x.strip()]
    widths = [int(x) for x in args.widths.split(",") if x.strip()]
    if not offsets or not widths or len(offsets) > 20 or any(x < 280 for x in widths):
        parser.error("Provide 1–20 offsets and viewport widths >= 280")
    try:
        for x in offsets:
            float(x)
    except ValueError:
        parser.error("All offsets must be numeric seconds")

    original_search = lyrics_api.search_lyric_candidates

    async def search(term, phase="all"):
        if phase == "netease":
            return [
                _candidate(
                    "现场真实操作歌词", "网易云音乐", raw, "https://music.163.com/"
                )
            ]
        return []

    lyrics_api.search_lyric_candidates = search
    with tempfile.TemporaryDirectory(prefix="vcktv-lyric-ui-party-") as directory:
        app = create_app(
            root=Path(directory) / "data",
            password="private",
            secret=b"lyric-ui-e2e-secret",
            auto_prepare=False,
            gpu_alignment=True,
        )
        app.state.events.create("Timed Lyrics Mobile Test", "765432")

        async def lookup(bvid, page):
            return dict(
                bvid=bvid,
                page=page,
                title="现场歌词随机偏移实测",
                uploader="Fixture Artist",
                duration=180,
                cover="",
                pages=[{"page": 1, "part": "one"}],
            )

        app.state.lookup = lookup
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=port, access_log=False, log_level="error"
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if urlopen(base + "/api/health", timeout=0.2).status == 200:
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("server not ready")
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                contexts = []
                errors = []
                try:
                    for i, offset in enumerate(offsets):
                        context = browser.new_context(
                            viewport={"width": widths[i % len(widths)], "height": 780}
                        )
                        contexts.append(context)
                        page = context.new_page()
                        page.on(
                            "pageerror",
                            lambda e, index=i: errors.append((index, str(e))),
                        )
                        page.route(
                            "https://player.bilibili.com/**",
                            lambda route: route.fulfill(
                                status=200,
                                content_type="text/html",
                                body="<html></html>",
                            ),
                        )
                        page.goto(base, wait_until="domcontentloaded")
                        page.locator("#join-code").fill("765432")
                        page.locator("#join-btn").click()
                        page.locator("#register-name").wait_for(state="visible")
                        page.locator("#register-name").fill("Singer-" + str(i + 1))
                        page.locator("#register-password").fill("password2026")
                        page.locator("#register-confirm").fill("password2026")
                        page.locator("#register-btn").click()
                        page.locator("#request-panel").wait_for(state="visible")
                        page.locator("#bvid").fill(f"BV{100 + i:010d}")
                        page.locator("#quick-submit-btn").click()
                        page.locator("#guest-lyrics-dialog").wait_for(
                            state="visible", timeout=10000
                        )
                        page.locator(".lyric-option").first.wait_for(
                            state="visible", timeout=8000
                        )
                        page.locator(".lyric-option").first.click()
                        page.locator("#guest-lyrics-select").click()
                        page.locator("#netease-review-dialog").wait_for(state="visible")
                        if args.prune_line and i == len(offsets) - 1:
                            line = page.locator(
                                "#netease-review-lines .netease-review-line"
                            ).nth(2)
                            line.click()
                            assert line.get_attribute("aria-pressed") == "false"
                        page.locator(
                            "#netease-alignment-choice .netease-accordion-toggle"
                        ).click()
                        page.locator(".offset-details summary").click()
                        page.locator("#netease-review-offset").fill(offset)
                        page.locator("#netease-review-timed").click()
                        if offset == args.invalid_offset:
                            page.locator("#netease-review-dialog").wait_for(
                                state="visible", timeout=7000
                            )
                            page.wait_for_function(
                                "document.querySelector('#toast').textContent.includes('视频边界')",
                                timeout=7000,
                            )
                            page.locator("#netease-review-offset").fill("0")
                            page.locator("#netease-review-timed").click()
                        page.locator("#netease-review-dialog").wait_for(
                            state="hidden", timeout=10000
                        )
                        song_id = (
                            app.state.events.active()
                            .db.execute(
                                "SELECT id FROM songs WHERE title='现场歌词随机偏移实测' ORDER BY id DESC LIMIT 1"
                            )
                            .fetchone()[0]
                        )
                        with app.state.events.active().lock:
                            row = dict(
                                app.state.events.active()
                                .db.execute(
                                    "SELECT * FROM songs WHERE id=?", (song_id,)
                                )
                                .fetchone()
                            )
                        expected_ms = int(
                            round(
                                float("0" if offset == args.invalid_offset else offset)
                                * 1000
                            )
                        )
                        assert row["lyric_timing_mode"] == "provider"
                        assert row["lyric_offset_ms"] == expected_ms, (
                            i,
                            row["lyric_offset_ms"],
                            expected_ms,
                        )
                        assert offset_timeline(
                            row["provider_lrc"], expected_ms, row["duration"]
                        )
                        if args.prune_line and i == len(offsets) - 1:
                            assert "第二句歌词" not in row["lyric_choice"], row[
                                "lyric_choice"
                            ]
                        assert not page.evaluate(
                            "document.documentElement.scrollWidth>innerWidth"
                        ), ("horizontal overflow", offset, widths[i % len(widths)])
                        print(
                            "REAL_BROWSER_LRC",
                            i,
                            "offset_ms",
                            expected_ms,
                            "selected_lines",
                            len(row["lyric_choice"].splitlines()),
                            "invalid_recovered",
                            offset == args.invalid_offset,
                            "overflow",
                            page.evaluate(
                                "document.documentElement.scrollWidth>innerWidth"
                            ),
                            flush=True,
                        )
                    assert not errors, errors
                    print(
                        "BROWSER_OFFSETS_OK",
                        len(contexts),
                        "page_errors",
                        len(errors),
                        flush=True,
                    )
                finally:
                    for ctx in contexts:
                        ctx.close()
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            lyrics_api.search_lyric_candidates = original_search


if __name__ == "__main__":
    main()
