"""Optional real-browser KTV smoke test, isolated from production and external APIs.

Requires Playwright Chromium and FFmpeg. This is intentionally not part of CI:
it verifies browser decoding and OS media dependencies on the target machine.
"""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urljoin
from urllib.request import urlopen


def serve() -> None:
    if os.environ.get("VC_KTV_BROWSER_TEST_ISOLATED") != "1":
        raise RuntimeError("Refusing to start outside an isolated browser test")

    import uvicorn

    from vc_ktv.application.factory import create_app

    root = Path(os.environ["STAGE_ROOT"])
    app = create_app(
        root=root / "data",
        password=(root / "config/admin-password").read_text().strip(),
        secret=bytes.fromhex((root / "config/token-secret").read_text().strip()),
        auto_prepare=False,
    )

    async def fixture_lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "Synthetic Browser Song",
            "uploader": "Offline Fixture",
            "cover": "",
            "duration": 8,
        }

    app.state.lookup = fixture_lookup
    app.state.probe = lambda _path: 8
    app.state.events.create("Isolated Browser Smoke", os.environ["STAGE_ROOM_CODE"])
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ["STAGE_PORT"]),
        log_level="warning",
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def synthetic_video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "8",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "28",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )


def ready(port: int, process: subprocess.Popen) -> None:
    for _ in range(60):
        if process.poll() is not None:
            raise RuntimeError("Isolated KTV server exited before becoming ready")
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as resp:
                health = json.load(resp)
            if health["ok"] and health["auto_prepare"] is False:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise RuntimeError("Isolated KTV server failed to become ready")


def check_ui(
    browser, port: int, code: str, admin_password: str, user_password: str, video: Path
) -> None:
    base = f"http://127.0.0.1:{port}"
    errors = []

    guest = browser.new_context(viewport={"width": 390, "height": 844})
    page = guest.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base, wait_until="domcontentloaded")
    page.locator("#join-code").fill(code)
    page.locator("#join-btn").click()
    page.locator("#register-name").wait_for(state="visible")
    page.locator("#register-name").fill("Synthetic UI User")
    page.locator("#register-password").fill(user_password)
    page.locator("#register-confirm").fill(user_password)
    page.locator("#register-btn").click()
    page.locator("#request-panel").wait_for(state="visible")
    assert "Synthetic UI User" in page.locator("#account-name").text_content()
    guest.close()
    print("PASS mobile join and registration")

    # A second browser context exercises persistent login, not saved cookies.
    guest = browser.new_context(viewport={"width": 390, "height": 844})
    page = guest.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))

    # Exercise the real lyric-review UI with isolated, synthetic candidates.
    def lyric_candidates(route):
        candidates = []
        if "phase=netease" in route.request.url:
            candidates = [
                {
                    "source": "网易云音乐",
                    "url": "synthetic://lyrics",
                    "title": "Synthetic Lyrics",
                    "text": "第一行\n第二行",
                    "unfiltered_text": "制作说明\n第一行\n第二行",
                    "ticket": "test-ticket",
                    "unfiltered_ticket": "test-original-ticket",
                    "timed_lrc": "[00:00.00]第一行\n[00:01.00]第二行",
                    "timing_ticket": "test-timing-ticket",
                }
            ]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"candidates": candidates}, ensure_ascii=False),
        )

    page.route("**/api/songs/*/lyrics/candidates*", lyric_candidates)
    page.goto(base, wait_until="domcontentloaded")
    page.locator("#join-code").fill(code)
    page.locator("#join-btn").click()
    page.locator("#show-user-login").click()
    page.locator("#user-login-name").fill("Synthetic UI User")
    page.locator("#user-login-password").fill(user_password)
    page.locator("#user-login-btn").click()
    page.locator("#request-panel").wait_for(state="visible")
    page.locator("#bvid").fill("BV0000000001")
    page.locator("#quick-submit-btn").click()
    page.locator("#guest-lyrics-dialog").wait_for(state="visible")
    page.get_by_role("button", name="逐行校正并使用").click()
    page.locator("#netease-review-dialog").wait_for(state="visible")
    page.locator("#netease-review-back").click()
    page.locator("#guest-lyrics-dialog").wait_for(state="visible")
    page.locator("#guest-lyrics-skip").click()
    page.locator("#guest-lyrics-dialog").wait_for(state="hidden")
    state = guest.request.get(f"{base}/api/state")
    assert state.status == 200
    waiting = state.json()["waiting"]
    assert len(waiting) == 1 and waiting[0]["skip_subtitles"] == 1
    song_id = waiting[0]["id"]
    print("PASS mobile login, BV request and no-lyrics selection")

    admin = browser.new_context(viewport={"width": 1280, "height": 720})
    page = admin.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base + "/admin/", wait_until="domcontentloaded")
    page.locator("#password").fill(admin_password)
    page.locator("#login-btn").click()
    page.locator("#login-box").wait_for(state="hidden")
    response = admin.request.post(
        f"{base}/api/admin/songs/{song_id}/media",
        multipart={
            "file": {
                "name": "synthetic.mp4",
                "mimeType": "video/mp4",
                "buffer": video.read_bytes(),
            }
        },
    )
    assert response.status == 200, response.text()
    assert response.json()["bytes"] == video.stat().st_size
    tv_link = admin.request.get(base + "/api/admin/tv-link")
    assert tv_link.status == 200
    print("PASS administrator login and real synthetic MP4 upload")

    tv = browser.new_context(viewport={"width": 1280, "height": 720})
    page = tv.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    url = urljoin(base, tv_link.json()["url"])
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#start").click()
    page.wait_for_function(
        "document.querySelector('#video').currentTime > 0.3", timeout=12000
    )
    media = page.locator("#video").evaluate(
        "(v) => ({url:v.currentSrc,width:v.videoWidth,height:v.videoHeight})"
    )
    assert (media["width"], media["height"]) == (320, 180)
    partial = tv.request.get(media["url"], headers={"Range": "bytes=0-1023"})
    assert partial.status == 206 and len(partial.body()) == 1024

    # The link contains a one-time pairing ticket; TV state requires the
    # activated control token, which the player keeps in memory.
    tv_token = page.evaluate("token")
    state_url = base + "/api/tv/state?token=" + quote(tv_token, safe="")
    for _ in range(32):
        state = tv.request.get(state_url)
        assert state.status == 200
        if state.json()["current"] is None:
            break
        page.wait_for_timeout(500)
    else:
        raise AssertionError("TV did not finish the synthetic song")
    downloaded = guest.request.get(f"{base}/api/songs/{song_id}/download")
    assert downloaded.status == 200, downloaded.text()[:200]
    assert len(downloaded.body()) == video.stat().st_size
    assert not errors, errors
    print("PASS browser video decode, signed HTTP Range, end-of-song and download")
    tv.close()
    admin.close()
    guest.close()


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise SystemExit(
            "Install optional Playwright to run browser_smoke.py"
        ) from error

    repo = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="vcktv-browser-smoke-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        (root / "config").mkdir()
        (root / "data").mkdir()
        admin_password = secrets.token_urlsafe(20)
        user_password = secrets.token_urlsafe(20)
        code = f"{secrets.randbelow(1_000_000):06d}"
        (root / "config/admin-password").write_text(admin_password + "\n")
        (root / "config/token-secret").write_text(secrets.token_hex(32) + "\n")
        for file in (root / "config").iterdir():
            file.chmod(0o600)
        video = root / "synthetic.mp4"
        synthetic_video(video)
        port = free_port()
        env = {
            **os.environ,
            "STAGE_ROOT": str(root),
            "STAGE_PORT": str(port),
            "STAGE_ROOM_CODE": code,
            "VC_KTV_BROWSER_TEST_ISOLATED": "1",
            "VC_KTV_AUTOPREPARE": "0",
            "PYTHONPATH": str(repo) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        app_python = os.getenv("VC_KTV_APP_PYTHON", sys.executable)
        with (root / "server.log").open("w") as log:
            process = subprocess.Popen(
                [app_python, str(Path(__file__).resolve()), "--serve"],
                cwd=repo,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                ready(port, process)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        check_ui(
                            browser, port, code, admin_password, user_password, video
                        )
                    finally:
                        browser.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve"]:
        serve()
    elif len(sys.argv) == 1:
        main()
    else:
        raise SystemExit("Usage: python scripts/browser_smoke.py")
