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
            "pages": (
                [
                    {"page": 1, "part": "日语版《恋の魔法ポーション》"},
                    {"page": 2, "part": "中文版《恋爱色魔法》"},
                ]
                if bvid == "BV1pX4y1a7Cd"
                else [{"page": 1, "part": "Synthetic Browser Song"}]
            ),
        }

    app.state.lookup = fixture_lookup
    app.state.probe = lambda _path: 8
    # Two earlier events exercise public/private archives without touching production.
    older = app.state.events.create("公开示例场次", "111111")
    with app.state.events.active().lock:
        db = app.state.events.active().db
        db.execute("INSERT INTO guests(id,name) VALUES ('archived','历史点歌者')")
        db.execute(
            """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,
               status,prep_state,played_at) VALUES ('BV0000000001',2,'归档歌曲','档案UP','',65,
               'archived',10,'played','done',20)"""
        )
        for i in range(2, 24):
            db.execute(
                """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,
                   status,prep_state,played_at) VALUES ('BV0000000001',1,?,'档案UP','',60,
                   'archived',?,'played','done',?)""",
                (f"归档补充歌曲{i}", i + 10, i + 20),
            )
        db.execute(
            """INSERT INTO songs (bvid,page,title,uploader,cover,duration,requester,requested_at,
               status,prep_state,played_at,source_type,source_id)
               VALUES ('',1,'归档网易云歌曲','网易云歌手','',60,'archived',100,
               'played','done',100,'netease','987654')"""
        )
        db.commit()
    app.state.events.close()
    info_path = root / "data" / "sessions" / older["id"] / "info.json"
    info = json.loads(info_path.read_text())
    info["public"] = True
    info_path.write_text(json.dumps(info, ensure_ascii=False))
    app.state.events.create("私有示例场次", "222222")
    with app.state.events.active().lock:
        db = app.state.events.active().db
        db.execute("INSERT INTO guests(id,name) VALUES ('private','私有点歌者')")
        db.execute(
            """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,
               status,prep_state) VALUES ('BV0000000002',1,'私有歌曲','UP','',55,
               'private',10,'closed_unplayed','done')"""
        )
        db.commit()
    app.state.events.close()
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
    for api_path in ("/api/events", "/api/events/20260101-000000-00000000/songs"):
        assert guest.request.get(base + api_path).status == 401
    page.goto(base + "/#archive-events", wait_until="domcontentloaded")
    page.locator("#guest-access").wait_for(state="visible")
    assert page.locator("#page-archive-events").is_hidden()
    page.goto(base, wait_until="domcontentloaded")
    assert page.locator("#guest-entry-description").get_attribute("role") == "status"
    assert page.locator("#entry-code .ui-field").count() == 1
    for width in (320, 390):
        page.set_viewport_size({"width": width, "height": 844})
        page.evaluate(
            "() => import('/ui-common.js').then(ui => ui.toast('操作已完成'))"
        )
        page.locator("#toast.show").wait_for(state="visible")
        # UX contract: an overlay never participates in layout or intercepts input.
        behavior = page.locator("#toast").evaluate("""el => ({
          position: getComputedStyle(el).position,
          pointerEvents: getComputedStyle(el).pointerEvents,
          top: el.getBoundingClientRect().top,
          bottom: el.getBoundingClientRect().bottom,
          width: el.getBoundingClientRect().width
        })""")
        assert behavior["position"] == "fixed"
        assert behavior["pointerEvents"] == "none"
        assert behavior["top"] >= 0 and behavior["bottom"] <= 844
        assert behavior["width"] <= width
        assert page.locator("#join-code").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth") <= width
        page.evaluate(
            "() => import('/ui-common.js').then(ui => ui.toast('测试错误通知', true))"
        )
        assert page.locator("#toast").get_attribute("role") == "alert"
        assert page.locator("#toast").get_attribute("aria-live") == "assertive"
    page.set_viewport_size({"width": 390, "height": 844})
    print("PASS accessible nonblocking notifications on 320/390px login")
    page.locator("#join-code").fill(code)
    page.locator("#join-btn").click()
    page.locator("#register-name").wait_for(state="visible")
    assert page.locator("#auth-register .ui-field").count() == 3
    page.locator("#register-name").fill("Synthetic UI User")
    # Password controls share one native template without changing form behavior.
    assert page.locator("#auth-register .password-toggle").count() == 2
    secret = page.locator("#register-password")
    toggle = page.locator("#register-password + .password-toggle")
    assert secret.get_attribute("type") == "password"
    toggle.click()
    assert secret.get_attribute("type") == "text"
    assert toggle.get_attribute("aria-pressed") == "true"
    toggle.click()
    assert secret.get_attribute("type") == "password"
    assert (
        page.locator("#register-confirm + .password-toggle").get_attribute("aria-label")
        == "显示重复密码"
    )
    page.locator("#register-password").fill(user_password)
    page.locator("#register-confirm").fill(user_password)
    page.locator("#register-btn").click()
    page.locator("#request-panel").wait_for(state="visible")
    assert page.locator("#account-name").text_content() == "Synthetic UI User"
    page.locator("#account-menu summary").click()
    assert page.locator(".account-dropdown").inner_text().strip() == "退出登录"
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
                },
                {
                    "source": "网易云音乐",
                    "url": "synthetic://alternate-lyrics",
                    "title": "Synthetic Alternate Lyrics",
                    "text": "\n".join(f"第{i}行" for i in range(1, 61)),
                    "ticket": "test-alternate-ticket",
                    "timed_lrc": "[00:00.00]第一行\n[00:01.00]第二行",
                    "timing_ticket": "test-alternate-timing",
                },
            ]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"candidates": candidates}, ensure_ascii=False),
        )

    page.route("**/api/songs/*/lyrics/candidates*", lyric_candidates)
    # Isolate iframe playback from the third-party Bilibili network.
    page.route(
        "https://player.bilibili.com/**",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<html><body>Preview fixture</body></html>",
        ),
    )
    page.goto(base, wait_until="domcontentloaded")
    page.locator("#join-code").fill(code)
    page.locator("#join-btn").click()
    page.locator("#show-user-login").click()
    page.locator("#user-login-name").fill("Synthetic UI User")
    page.locator("#user-login-password").fill(user_password)
    page.locator("#user-login-btn").click()
    page.locator("#request-panel").wait_for(state="visible")
    # Both ordering sources use the same form hierarchy and label treatment.
    assert page.locator("#source-panel-bilibili .source-input-field").count() == 1
    assert page.locator("#source-panel-netease .source-input-field").count() == 1
    assert (
        page.locator("#source-panel-bilibili .source-input-field > label").inner_text()
        == "B站链接或 BV 号"
    )
    page.locator("#queue-panel").wait_for(state="visible")
    assert page.locator("#queue-panel .section-head h2").inner_text() == "☷ 队列"
    assert (
        page.locator("#queue-panel a[href='#played']").get_attribute("class")
        == "btn neutral queue-records-link"
    )
    page.route(
        "**/api/netease/search*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "songs": [
                        {
                            "source_id": "123",
                            "title": "Synthetic NetEase Song",
                            "uploader": "Synthetic Singer",
                            "duration": 60,
                            "cover": "",
                        }
                    ]
                }
            ),
        ),
    )
    page.locator("#source-tab-netease").click()
    assert not page.locator("#netease-search-message").is_visible()
    page.locator("#netease-song-query").fill("Synthetic")
    page.locator("#netease-search-btn").click()
    result = page.locator(".netease-search-result strong")
    result.wait_for(state="visible")
    assert result.text_content() == "Synthetic NetEase Song"
    page.locator("#source-tab-bilibili").click()
    print("PASS source switching and NetEase search results")

    # Public archive navigation retains search state, nested return paths and
    # gives source actions without serving any archived video.
    page.locator('#queue-panel a[href="#played"]').click()
    page.locator("#subpage-action").click()
    public_list = page.locator("#page-archive-events")
    public_list.wait_for(state="visible")
    assert public_list.locator(".archive-entry").count() == 1
    public_list.locator("input[type=search]").fill("公开")
    public_list.locator(".archive-entry").click()
    page.locator("#page-archive-event .song-row").first.wait_for(state="visible")
    assert page.locator("#page-archive-event .song-row").count() == 24
    for width in (320, 390):
        page.set_viewport_size({"width": width, "height": 780})
        assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert page.locator("#subpage-back").get_attribute("href") == "#archive-events"
    page.locator("#page-archive-event .song-row").first.click()
    page.locator("#page-archive-song").wait_for(state="visible")
    assert (
        page.locator("#subpage-back")
        .get_attribute("href")
        .startswith("#archive-event-")
    )
    assert (
        page.locator("#page-archive-song .detail-header h2").inner_text() == "归档歌曲"
    )
    assert page.locator("#page-archive-song a[href*='download']").count() == 0
    assert page.locator("#page-archive-song .detail-tools button").count() >= 2
    # Browser Back/Forward must retain the nested archive context.
    page.go_back()
    page.locator("#page-archive-event").wait_for(state="visible")
    page.go_forward()
    page.locator("#page-archive-song").wait_for(state="visible")
    # Reorder must submit a new current-session request, not replay old media.
    replay_calls = []

    def fake_archive_order(route):
        replay_calls.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"id": 999, "title": "归档歌曲", "lyric_query": "归档歌曲"}
            ),
        )

    page.route("**/api/songs", fake_archive_order)
    page.get_by_role("button", name="↻ 再点一首").click()
    page.locator("#guest-lyrics-dialog").wait_for(state="visible")
    assert replay_calls == [{"bvid": "BV0000000001", "page": 2}]
    page.get_by_role("button", name="稍后").click()
    page.locator("#guest-lyrics-dialog").wait_for(state="hidden")
    page.unroute("**/api/songs", fake_archive_order)
    assert page.locator("#page-archive-song").is_visible()
    page.locator("#subpage-back").click()
    page.locator("#page-archive-event").wait_for(state="visible")
    # Returning from a lower song should restore the long playlist's scroll.
    page.locator("#page-archive-event .song-row").last.scroll_into_view_if_needed()
    scroll_before = page.evaluate("window.scrollY")
    assert scroll_before > 100
    page.locator("#page-archive-event .song-row").last.click()
    page.locator("#page-archive-song").wait_for(state="visible")
    page.wait_for_function(
        'document.querySelector("#page-archive-song .detail-header h2")?.textContent === "归档网易云歌曲"'
    )
    netease_calls = []

    def fake_netease_reorder(route):
        netease_calls.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"id": 998, "title": "归档网易云歌曲", "lyric_query": "归档网易云歌曲"}
            ),
        )

    page.route("**/api/netease/songs", fake_netease_reorder)
    page.get_by_role("button", name="↻ 再点一首").click()
    page.locator("#guest-lyrics-dialog").wait_for(state="visible")
    assert netease_calls == [{"ref": "987654"}]
    page.get_by_role("button", name="稍后").click()
    page.locator("#guest-lyrics-dialog").wait_for(state="hidden")
    page.unroute("**/api/netease/songs", fake_netease_reorder)
    page.locator("#subpage-back").click()
    page.locator("#page-archive-event").wait_for(state="visible")
    page.wait_for_function(
        "minimum => window.scrollY >= minimum", arg=round(scroll_before * 0.75)
    )
    page.locator("#subpage-back").click()
    public_list.wait_for(state="visible")
    assert public_list.locator("input[type=search]").input_value() == "公开"
    assert public_list.locator(".archive-entry").count() == 1
    assert page.locator("#subpage-back").get_attribute("href") == "#played"
    page.locator("#subpage-back").click()
    page.locator("#page-played").wait_for(state="visible")
    page.locator("#subpage-back").click()
    page.locator("#request-panel").wait_for(state="visible")
    assert page.locator("#home-brand").get_attribute("href") == "/"
    print(
        "PASS public archive, full song detail, source-only actions, new order, and nested back"
    )

    # A real two-P BVID is used with offline metadata; navigating Back creates
    # no order, and a ?p=2 link is retained through final submission.
    page.locator("#bvid").fill("BV1pX4y1a7Cd")
    page.locator("#quick-submit-btn").click()
    page.locator("#bilibili-preview-dialog").wait_for(state="visible")
    assert (
        page.locator("#bilibili-preview-dialog")
        .get_by_role("button", name="返回")
        .is_visible()
    )
    assert page.locator(".bili-part").count() == 2
    assert "page=1" in page.locator("#bilibili-preview-player").get_attribute("src")
    page.locator('.bili-part[data-page="2"]').click()
    assert "page=2" in page.locator("#bilibili-preview-player").get_attribute("src")
    assert (
        page.locator('.bili-part[data-page="2"]').get_attribute("aria-pressed")
        == "true"
    )
    page.locator("#bilibili-preview-back").click()
    page.locator("#bilibili-preview-dialog").wait_for(state="hidden")
    page.wait_for_function(
        "!document.getElementById('bilibili-preview-player').getAttribute('src')"
    )
    assert guest.request.get(f"{base}/api/state").json()["waiting"] == []

    page.locator("#bvid").fill("https://www.bilibili.com/video/BV1pX4y1a7Cd/?p=2")
    page.locator("#quick-submit-btn").click()
    page.locator("#bilibili-preview-dialog").wait_for(state="visible")
    assert (
        page.locator('.bili-part[data-page="2"]').get_attribute("aria-pressed")
        == "true"
    )
    page.locator("#bilibili-preview-confirm").click()
    page.locator("#guest-lyrics-dialog").wait_for(state="visible")
    assert (
        page.locator("#guest-lyrics-dialog")
        .get_by_role("heading", name="选择歌词")
        .is_visible()
    )
    assert (
        page.locator("#guest-lyrics-dialog")
        .get_by_role("button", name="稍后")
        .is_visible()
    )
    assert page.locator("#guest-lyrics-status").get_attribute("role") == "status"
    multi = guest.request.get(f"{base}/api/state").json()["waiting"]
    assert (
        len(multi) == 1 and multi[0]["bvid"] == "BV1pX4y1a7Cd" and multi[0]["page"] == 2
    )
    page.wait_for_function(
        "!document.getElementById('bilibili-preview-player').getAttribute('src')"
    )
    page.locator("#guest-lyrics-preview").click()
    page.locator("#bilibili-preview-dialog").wait_for(state="visible")
    assert "page=2" in page.locator("#bilibili-preview-player").get_attribute("src")
    assert page.locator("#bilibili-preview-confirm").is_hidden()
    page.locator("#bilibili-preview-back").click()
    assert page.locator("#guest-lyrics-dialog").is_visible()

    # Submit a fast provider-timed selection through the new single-choice footer.
    # Synthetic lyric tickets are intentionally fulfilled by the fixture route.
    selected_lyrics = []

    def accept_lyric_selection(route):
        selected_lyrics.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/songs/*/lyrics/select", accept_lyric_selection)
    page.locator(".lyric-option").first.wait_for(state="visible")
    page.locator(".lyric-option").first.click()
    page.locator("#guest-lyrics-select").click()
    page.locator("#netease-review-dialog").wait_for(state="visible")
    page.locator("#netease-alignment-choice .netease-accordion-toggle").click()
    page.locator("#netease-review-timed").click()
    page.locator("#netease-review-dialog").wait_for(state="hidden")
    assert len(selected_lyrics) == 1
    assert selected_lyrics[0]["timing_mode"] == "provider"
    assert selected_lyrics[0]["source"] == "网易云音乐"
    assert guest.request.post(f"{base}/api/songs/{multi[0]['id']}/cancel").status == 200
    print("PASS two-P preview, ?p=2 submit, lyric preview and timed selection")

    page.locator("#bvid").fill("BV0000000001")
    page.locator("#quick-submit-btn").click()
    page.locator("#guest-lyrics-dialog").wait_for(state="visible")
    assert not page.locator("#bilibili-preview-dialog").is_visible()
    assert page.locator("#guest-lyrics-select").is_disabled()
    page.locator(".lyric-option").nth(1).wait_for(state="visible")
    first, second = (
        page.locator(".lyric-option").nth(0),
        page.locator(".lyric-option").nth(1),
    )
    previews = page.locator(".lyric-preview-btn")
    assert previews.count() == page.locator(".lyric-option").count() == 2
    assert page.locator(".lyric-option button").count() == 0
    previews.first.click()
    preview_dialog = page.locator("#guest-lyric-preview-dialog")
    preview_dialog.wait_for(state="visible")
    assert preview_dialog.get_by_role("button", name="返回").is_visible()
    assert (
        page.locator("#guest-lyric-preview-info").inner_text()
        == "Synthetic Lyrics / 网易云音乐"
    )
    assert (
        page.locator("#guest-lyric-preview-text").inner_text()
        == "制作说明\n第一行\n第二行"
    )
    assert page.locator("#guest-lyrics-dialog").evaluate("el => el.open")
    assert page.locator("#guest-lyrics-select").is_disabled()
    page.locator("#guest-lyric-preview-close").click()
    preview_dialog.wait_for(state="hidden")
    assert page.locator("#guest-lyrics-dialog").is_visible()
    first.click()
    assert first.get_attribute("aria-pressed") == "true"
    second.click()
    assert first.get_attribute("aria-pressed") == "false"
    assert second.get_attribute("aria-pressed") == "true"
    assert page.locator("#guest-lyrics-select").is_enabled()
    # Preview does not change the selected card, even when another is open.
    previews.first.click()
    assert page.locator("#guest-lyric-preview-text").inner_text().startswith("制作说明")
    page.keyboard.press("Escape")
    preview_dialog.wait_for(state="hidden")
    assert second.get_attribute("aria-pressed") == "true"
    assert page.locator("#guest-lyrics-select").is_enabled()
    excerpt = second.locator(".lyric-excerpt")
    assert excerpt.evaluate("el => getComputedStyle(el).webkitLineClamp") == "2"
    assert excerpt.evaluate(
        "el => parseFloat(getComputedStyle(el).fontSize)"
    ) < second.locator("strong").evaluate(
        "el => parseFloat(getComputedStyle(el).fontSize)"
    )
    assert excerpt.evaluate(
        "el => el.getBoundingClientRect().height <= 2 * parseFloat(getComputedStyle(el).lineHeight) + 1"
    )
    for width in (320, 390):
        page.set_viewport_size({"width": width, "height": 780})
        assert page.evaluate("document.documentElement.scrollWidth") <= width
        assert page.locator("#guest-lyrics-select").is_visible()
        assert page.locator("#guest-lyrics-select").bounding_box()["y"] < 780
        previews.nth(1).click()
        preview_dialog.wait_for(state="visible")
        assert page.locator("#guest-lyric-preview-text").inner_text().endswith("第60行")
        assert page.locator("#guest-lyric-preview-text").evaluate(
            "el => el.scrollHeight > el.clientHeight"
        )
        assert preview_dialog.bounding_box()["width"] <= width
        assert preview_dialog.bounding_box()["y"] >= 0
        page.locator("#guest-lyric-preview-close").click()
        preview_dialog.wait_for(state="hidden")
        assert (
            page.locator("#guest-lyrics-dialog")
            .get_by_role("button", name="稍后")
            .is_visible()
        )
        assert second.get_attribute("aria-pressed") == "true"
        assert page.locator("#guest-lyrics-select").is_enabled()
    print(
        "PASS each lyric card previews the entire text, including credits, without changing selection"
    )
    page.locator("#guest-lyrics-select").click()
    page.locator("#netease-review-dialog").wait_for(state="visible")
    assert (
        page.locator("#netease-review-dialog")
        .get_by_role("button", name="返回")
        .is_visible()
    )
    for width in (320, 390):
        page.set_viewport_size({"width": width, "height": 780})
        assert page.evaluate("document.documentElement.scrollWidth") <= width
        assert page.locator("#netease-review-dialog").bounding_box()["width"] <= width
    page.locator("#netease-review-back").click()
    page.locator("#guest-lyrics-dialog").wait_for(state="visible")
    assert second.get_attribute("aria-pressed") == "true"
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
    assert page.locator("#login-box .ui-field").count() == 2
    assert "wide" in page.locator("#login-btn").get_attribute("class")
    assert page.locator("#login-box .ui-hint-inline").inner_text() == "全局管理员留空"
    assert page.locator("#password + .password-toggle").count() == 1
    page.locator("#password").fill(admin_password)
    page.locator("#login-btn").click()
    page.locator("#login-box").wait_for(state="hidden")
    page.locator("#admin-settings-link").wait_for(state="visible")
    assert page.locator("#account-name").text_content() == "管理员"
    assert page.locator(".quick-nav").count() == 0
    page.locator("#account-menu summary").click()
    assert page.locator(".account-dropdown").inner_text().strip() == "退出登录"
    page.locator("#account-menu summary").click()
    assert page.locator("#admin-queue-panel").is_visible()
    assert page.locator("#admin-queue-panel .section-head h2").inner_text() == "☷ 队列"
    assert (
        page.locator("#admin-queue-panel a[href='#played']").get_attribute("class")
        == "btn neutral queue-records-link"
    )
    assert page.locator("#admin-queue-panel .song-list").count() == 2
    assert page.locator("#admin-current").is_visible()
    assert page.locator("#admin-current .current-label").text_content() == "正在播放"
    assert page.locator("#admin-queue-panel h3").all_text_contents() == [
        "🎵 待播放",
        "⏳ 准备中",
    ]
    admin_state = admin.request.get(f"{base}/api/admin/state").json()
    assert (
        page.locator("#admin-queue-count").text_content()
        == f"{len(admin_state['queue'])} 首可播"
    )
    assert page.locator("#admin-queue-ready .song-row").count() == len(
        admin_state["queue"]
    )
    assert page.locator("#admin-queue-waiting .song-row").count() == len(
        admin_state["waiting"]
    )
    assert page.locator("#admin-current .song-row").count() == int(
        admin_state["current"] is not None
    )
    assert page.locator("#manage-status").count() == 0
    page.locator('#admin-queue-panel a[href="#played"]').click()
    page.locator("#page-played").wait_for(state="visible")
    history = admin.request.get(f"{base}/api/songs/history?admin_view=true")
    assert history.status == 200
    assert page.locator("#page-played .song-row").count() == len(
        history.json()["songs"]
    )
    page.locator("#page-played .song-row").first.click()
    page.locator("#page-song").wait_for(state="visible")
    assert page.locator("#subpage-back").get_attribute("href") == "#played"
    page.locator("#subpage-back").click()
    page.locator("#page-played").wait_for(state="visible")
    page.locator("#subpage-back").click()
    page.locator("#admin-queue-panel").wait_for(state="visible")
    for width in (320, 390):
        page.set_viewport_size({"width": width, "height": 780})
        assert page.evaluate("document.documentElement.scrollWidth") <= width
        assert page.locator('#admin-queue-panel a[href="#played"]').is_visible()
    page.set_viewport_size({"width": 1280, "height": 720})
    print("PASS administrator queue parity, song history, details and mobile layout")
    # Administrator archive hierarchy and brand navigation are stable.
    assert (
        page.locator(".topbar").evaluate("el => getComputedStyle(el).position")
        == "sticky"
    )
    assert page.locator("#remote-error").get_attribute("role") == "alert"
    assert page.locator("#remote-error").is_hidden()
    page.locator("#admin-settings-link").click()
    assert page.locator("#home-brand").get_attribute("href") == "/admin/"
    assert page.locator(".topbar").is_visible()
    assert page.locator("#tab-admin").evaluate(
        "el => el.classList.contains('selected')"
    )
    page.locator('a[href="#settings-history"]').click()
    archives = page.locator("#event-history .archive-entry")
    assert archives.count() == 2
    archives.filter(has_text="私有示例场次").click()
    page.locator("#page-archive-event").wait_for(state="visible")
    assert page.locator("#subpage-back").get_attribute("href") == "#settings-history"
    assert page.locator("#page-archive-event .archive-event-tools").is_visible()
    for width in (320, 390):
        page.set_viewport_size({"width": width, "height": 780})
        assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.set_viewport_size({"width": 1280, "height": 720})
    page.locator("#page-archive-event .song-row").first.click()
    page.locator("#page-archive-song").wait_for(state="visible")
    assert page.locator("#page-archive-song a[href*='download']").count() == 0
    page.locator("#page-archive-song select.archive-status-select").select_option(
        "skipped"
    )
    page.wait_for_function(
        'document.querySelector("#page-archive-song .detail-state strong").textContent === "已跳过"'
    )
    page.locator("#subpage-back").click()
    page.locator("#page-archive-event").wait_for(state="visible")
    page.wait_for_function(
        'document.querySelector("#page-archive-event .song-row .song-state")?.textContent === "已跳过"'
    )
    page.locator("#subpage-back").click()
    page.locator("#event-history").wait_for(state="visible")
    page.locator("#subpage-back").click()
    page.wait_for_function(
        'document.querySelector("#subpage-title").textContent === "设置与统计"'
    )
    page.locator("#home-brand").click()
    page.locator("#admin-queue-panel").wait_for(state="visible")
    page.locator("#admin-settings-link").click()
    page.locator('a[href="#stats"]').click()
    page.wait_for_function(
        'document.querySelector("#subpage-title").textContent === "演唱统计"'
    )
    assert page.locator("#subpage-back").get_attribute("href") == "#settings"
    page.locator("#subpage-back").click()
    page.wait_for_function(
        'document.querySelector("#subpage-title").textContent === "设置与统计"'
    )
    page.locator("#subpage-back").click()
    page.set_viewport_size({"width": 320, "height": 780})
    assert page.evaluate("document.documentElement.scrollWidth") == 320
    page.set_viewport_size({"width": 1280, "height": 720})

    # Global administrator actions are isolated to the disposable test event.
    page.goto(base + "/admin/#settings-accounts", wait_until="domcontentloaded")
    page.locator("#admin-account-create").wait_for(state="visible")
    page.locator("#admin-account-name").fill("Synthetic Event Admin")
    page.locator("#admin-account-password").fill("smoke-password-123")
    page.locator("#admin-account-create").click()
    account = page.locator("#admin-account-list .account-entry").filter(
        has_text="Synthetic Event Admin"
    )
    account.wait_for(state="visible")
    account.locator("summary").click()
    account.locator('input[type="password"]').fill("smoke-reset-123")
    account.get_by_role("button", name="重置密码").click()
    page.wait_for_function(
        'document.querySelector("#admin-account-list input[type=password]").value === ""'
    )
    page.once("dialog", lambda dialog: dialog.accept())
    account.get_by_role("button", name="撤销账号").click()
    account.wait_for(state="detached")
    print("PASS event administrator create, reset and revoke")

    # Exercise both settings forms and keep an in-progress edit intact across
    # the administrator's periodic refresh.
    page.goto(base + "/admin/#settings-playback", wait_until="networkidle")
    quality = page.locator("#video-quality")
    quality.wait_for(state="visible")
    codec = page.locator("#original-codec-mode")
    assert quality.input_value() == "1080"
    assert codec.input_value() == "av1"
    quality.focus()
    quality.select_option("720")
    codec.select_option("h264")
    page.wait_for_timeout(4000)
    assert quality.input_value() == "720"
    assert codec.input_value() == "h264"
    with page.expect_response(
        lambda response: (
            "/api/admin/settings" in response.url and response.request.method == "PUT"
        )
    ) as saved:
        page.locator("#settings-save-btn").click()
    assert saved.value.status == 200
    video_settings = admin.request.get(base + "/api/admin/settings").json()
    assert video_settings["video_quality"] == 720
    assert video_settings["original_codec_mode"] == "h264"
    page.reload(wait_until="networkidle")
    assert quality.input_value() == "720"
    assert codec.input_value() == "h264"

    # Keep later synthetic upload byte-identical; the test exercises
    # settings persistence, not actual transcoding.
    quality.select_option("720")
    codec.select_option("default")
    with page.expect_response(
        lambda response: (
            "/api/admin/settings" in response.url and response.request.method == "PUT"
        )
    ) as restored:
        page.locator("#settings-save-btn").click()
    assert restored.value.status == 200
    restored_settings = admin.request.get(base + "/api/admin/settings").json()
    assert restored_settings["video_quality"] == 720
    assert restored_settings["original_codec_mode"] == "default"

    page.goto(base + "/admin/#settings-queue", wait_until="networkidle")
    page.locator("#queue-settings-save").wait_for(state="visible")
    page.locator("#pause-orders").check()
    with page.expect_response(
        lambda response: (
            "/api/admin/settings" in response.url and response.request.method == "PUT"
        )
    ) as saved:
        page.locator("#queue-settings-save").click()
    assert saved.value.status == 200
    assert admin.request.get(base + "/api/admin/settings").json()["orders_paused"]
    page.locator("#pause-orders").uncheck()
    with page.expect_response(
        lambda response: (
            "/api/admin/settings" in response.url and response.request.method == "PUT"
        )
    ) as saved:
        page.locator("#queue-settings-save").click()
    assert saved.value.status == 200
    assert not admin.request.get(base + "/api/admin/settings").json()["orders_paused"]
    print("PASS playback and queue settings save, with focused edit preserved")

    # Mock connection APIs; no NetEase network access or credentials.
    page.route(
        "**/api/admin/netease/status",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"connected":false,"pending":false,"phase":"idle"}',
        ),
    )
    page.route(
        "**/api/admin/netease/qr",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "connected": False,
                    "pending": True,
                    "phase": "waiting",
                    "qr_image": "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=",
                }
            ),
        ),
    )
    page.goto(base + "/admin/#settings-netease", wait_until="domcontentloaded")
    page.locator("#netease-login-start").wait_for(state="visible")
    page.locator("#netease-login-start").click()
    page.locator("#netease-login-qr-box").wait_for(state="visible")
    assert (
        page.locator("#netease-login-qr-image")
        .get_attribute("src")
        .startswith("data:image/gif")
    )
    page.goto(base + "/admin/", wait_until="domcontentloaded")
    print("PASS NetEase admin connection QR UI and navigation")

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
    admin_page = admin.pages[0]
    admin_page.wait_for_function(
        'document.querySelectorAll("#admin-queue-ready .song-row").length === 1',
        timeout=10000,
    )
    assert admin_page.locator("#admin-queue-ready .song-row").first.is_visible()
    assert admin_page.locator("#admin-queue-waiting .song-row").count() == 0
    tv_link = admin.request.get(base + "/api/admin/tv-link")
    assert tv_link.status == 200
    print("PASS administrator login and real synthetic MP4 upload")

    tv = browser.new_context(viewport={"width": 1280, "height": 720})
    page = tv.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    url = urljoin(base, tv_link.json()["url"])
    page.goto(url, wait_until="domcontentloaded")
    # The administrator panel sees the primary via live remote reports.
    # Exercise a real command before song playback (no timing-sensitive pause).
    admin_page = admin.pages[0]
    admin_page.wait_for_function(
        'document.querySelector("#tv-online").textContent==="主屏在线"',
        timeout=12000,
    )
    admin_page.wait_for_function(
        'document.querySelector("#tv-link-btn").textContent.includes("更换主屏")',
        timeout=12000,
    )
    assert "复制更换链接" in admin_page.locator("#tv-copy-btn").text_content()
    assert admin_page.locator("#tv-revoke-btn").is_visible()
    assert admin_page.locator("#remote-opacity").is_enabled()
    assert admin_page.locator("#remote-pause").get_attribute("aria-label") in (
        "暂停",
        "继续",
    )
    assert (
        admin_page.locator("#remote-restart").get_attribute("aria-label") == "从头重播"
    )
    for width in (320, 390):
        admin_page.set_viewport_size({"width": width, "height": 780})
        assert admin_page.evaluate("document.documentElement.scrollWidth") <= width
        assert admin_page.locator("#remote-replay").is_visible()
        assert admin_page.locator("#tv-remote-extra .remote-offsets").is_visible()
        assert admin_page.locator("#remote-replay").evaluate(
            "el => el.getBoundingClientRect().height >= 44"
        )
    admin_page.set_viewport_size({"width": 1280, "height": 720})
    admin_page.locator("#remote-opacity").evaluate(
        '(control) => {control.value="20";control.dispatchEvent(new Event("change"));}'
    )
    page.wait_for_function(
        'document.querySelector("#banner-transparency").value==="20"',
        timeout=8000,
    )
    print("PASS administrator TV remote command and acknowledgment")
    page.locator("#start").click()
    page.wait_for_function(
        "document.querySelector('#video').currentTime > 0.3", timeout=12000
    )
    media = page.locator("#video").evaluate(
        "(v) => ({url:v.currentSrc,width:v.videoWidth,height:v.videoHeight})"
    )
    assert (media["width"], media["height"]) == (320, 180)
    admin_page.wait_for_function(
        'document.querySelectorAll("#admin-current .song-row").length === 1',
        timeout=6000,
    )
    assert admin_page.locator("#admin-current .song-row").first.is_visible()
    assert (
        admin_page.locator("#admin-current .current-label").text_content() == "正在播放"
    )
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
