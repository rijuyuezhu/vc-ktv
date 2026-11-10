"""Catch broken JavaScript selectors without pinning page copy or styling."""

import re
from html.parser import HTMLParser
from pathlib import Path


class IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == "id":
                assert value not in self.ids, f"Duplicate HTML ID: {value}"
                self.ids.add(value)


def app_scripts(root):
    return "\n".join(
        (root / name).read_text()
        for name in (
            "app.js",
            "song-detail.js",
            "lyrics.js",
            "song-requests.js",
            "remote-controls.js",
            "admin-playback.js",
            "tv-primary.js",
            "admin-events.js",
            "admin-settings.js",
            "admin-accounts.js",
            "admin-netease.js",
            "ui-common.js",
        )
    )


def test_lyric_picker_is_loaded_as_a_module():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text()
    main = (root / "app.js").read_text()
    lyrics = (root / "lyrics.js").read_text()
    assert '<script type="module" src="/app.js"></script>' in html
    assert 'import {createLyricPicker} from "./lyrics.js";' in main
    assert "export function createLyricPicker(" in lyrics
    assert "const {openLyricPicker} = lyricPicker;" in main
    assert "selectedSongId:()=>selectingSongId" in lyrics


def test_song_detail_and_manual_lyric_editor_have_one_owner():
    root = Path(__file__).resolve().parents[1] / "static"
    main = (root / "app.js").read_text()
    detail = (root / "song-detail.js").read_text()
    assert 'import {setupSongDetail} from "./song-detail.js";' in main
    assert "export function setupSongDetail({" in detail
    assert "const {refreshSongDetail,invalidateSongDetail}=setupSongDetail({" in main
    assert "invalidateSongDetail();playedKey=null;" in main
    assert main.index("const isAdminPath=") < main.index("setupSongDetail({")
    assert "function addSourceTag(parent,song){" in main
    assert "async function refreshSongDetail()" in detail
    assert "async function editLyrics(songId, songTitle)" in detail
    assert "const key=JSON.stringify([song,isAdminPath()])" in detail
    assert "return {refreshSongDetail,invalidateSongDetail};" in detail
    assert "popupSongId" not in main and "detailKey" not in main


def test_song_request_actions_are_loaded_from_a_module():
    root = Path(__file__).resolve().parents[1] / "static"
    main = (root / "app.js").read_text()
    requests = (root / "song-requests.js").read_text()
    assert 'import {setupSongRequests} from "./song-requests.js";' in main
    assert "export function setupSongRequests(" in requests
    assert "setupSongRequests({" in main
    assert "let preview = null;" in requests
    assert "async function queryNeteaseSongs()" in requests


def test_admin_remote_controller_is_loaded_from_module():
    root = Path(__file__).resolve().parents[1] / "static"
    main = (root / "app.js").read_text()
    playback = (root / "admin-playback.js").read_text()
    remote = (root / "remote-controls.js").read_text()
    assert 'import {setupAdminPlayback} from "./admin-playback.js";' in main
    assert 'import {setupRemoteControls} from "./remote-controls.js";' in playback
    assert "export function setupRemoteControls(" in remote
    assert (
        "getState:()=>({playbackMode,currentAdminSong,nextAdminSong,currentMediaId,"
        in playback
    )
    assert "...primaryTV.state()" in playback
    assert "const {updateRemoteControls} = setupRemoteControls({" in playback
    assert '$("remote-pause").onclick=' in remote
    assert "function updatePlaybackUI()" in playback
    assert "await adminPlayback.syncFromAdminState(adm,changed);" in main


def test_primary_tv_management_lives_in_its_own_module():
    root = Path(__file__).resolve().parents[1] / "static"
    main = (root / "app.js").read_text()
    playback = (root / "admin-playback.js").read_text()
    primary = (root / "tv-primary.js").read_text()
    assert 'import {setupAdminPlayback} from "./admin-playback.js";' in main
    assert 'import {setupTVPrimary} from "./tv-primary.js";' in playback
    assert "export function setupTVPrimary(" in primary
    assert "const primaryTV=setupTVPrimary({" in playback
    assert "state:()=>({tvPrimaryState,tvRemoteState})" in primary
    assert "...primaryTV.state()" in playback
    for action in ("tv-link-btn", "tv-copy-btn", "tv-revoke-btn"):
        assert f'$("{action}").onclick=' in primary


def test_admin_playback_state_and_browser_media_have_one_owner():
    root = Path(__file__).resolve().parents[1] / "static"
    main = (root / "app.js").read_text()
    playback = (root / "admin-playback.js").read_text()
    assert "export function setupAdminPlayback({" in playback
    assert "const adminPlayback=setupAdminPlayback({" in main
    assert "await adminPlayback.syncFromAdminState(adm,changed)" in main
    assert "setInterval(adminPlayback.pollTVRemoteStatus,1000)" in main
    assert "if(!await adminPlayback.prepareLogout())return;" in main
    assert "adminPlayback.resetPlayer();" in main
    for state in (
        "currentMediaId",
        "currentAdminSong",
        "nextAdminSong",
        "manualMode",
        "playbackMode",
    ):
        assert f"let {state}" in playback
        assert f"let {state}" not in main
    for method in (
        "async function playNext()",
        "async function finishActive(",
        "async function assignPlayer(",
        "function updatePlaybackUI()",
    ):
        assert method in playback
        assert method not in main


def test_admin_account_and_netease_connection_modules():
    root = Path(__file__).resolve().parents[1] / "static"
    main = (root / "app.js").read_text()
    events = (root / "admin-events.js").read_text()
    accounts = (root / "admin-accounts.js").read_text()
    netease = (root / "admin-netease.js").read_text()
    assert 'import {setupAdminEvents} from "./admin-events.js";' in main
    assert "const {refreshEvents,leaveNeteasePage}=setupAdminEvents({" in main
    assert 'import {setupAdminAccounts} from "./admin-accounts.js";' in events
    assert 'import {setupNeteaseConnection} from "./admin-netease.js";' in events
    assert "const {refreshAdminAccounts} = setupAdminAccounts();" in events
    assert (
        "const {stopNeteaseLoginPolling,refreshNeteaseLogin} = setupNeteaseConnection();"
        in events
    )
    assert 'from "./ui-common.js";' in accounts
    assert 'from "./ui-common.js";' in netease
    assert "export function setupAdminAccounts(" in accounts
    assert "export function setupNeteaseConnection(" in netease
    assert "let neteaseLoginPolling=null;" in netease
    assert "async function refreshAdminAccounts()" in accounts
    assert "async function refreshNeteaseLogin()" in netease
    assert "adminAccountsEventId" in events
    assert "neteaseLoginPageActive" in events
    assert "leaveNeteasePage();" in main


def test_event_settings_are_owned_by_a_module():
    root = Path(__file__).resolve().parents[1] / "static"
    main = (root / "app.js").read_text()
    events = (root / "admin-events.js").read_text()
    settings = (root / "admin-settings.js").read_text()
    assert 'import {setupAdminEvents} from "./admin-events.js";' in main
    assert 'import {setupAdminSettings} from "./admin-settings.js";' in events
    assert "export function setupAdminSettings(" in settings
    assert "const {refreshAdminSettings} = setupAdminSettings({" in events
    assert "await refreshAdminSettings(active?.id||null)" in events
    assert "let settingsEventId=null;" in settings
    assert "document.activeElement" in settings
    assert "invalidateQueueState();" in settings
    assert "function updateStylePreview()" in settings


def test_shared_ui_helpers_have_explicit_es_module_imports():
    root = Path(__file__).resolve().parents[1] / "static"
    common = (root / "ui-common.js").read_text()
    for feature in (
        "app.js",
        "song-detail.js",
        "lyrics.js",
        "song-requests.js",
        "remote-controls.js",
        "admin-playback.js",
        "tv-primary.js",
        "admin-events.js",
        "admin-settings.js",
        "admin-accounts.js",
        "admin-netease.js",
    ):
        assert 'from "./ui-common.js";' in (root / feature).read_text()
    for helper in ("toast", "api", "error", "guarded", "duration", "appendText"):
        assert re.search(rf"export (?:async )?function {helper}\(", common)
    assert "export {$};" in common


def test_guest_and_admin_js_element_references_exist():
    root = Path(__file__).resolve().parents[1] / "static"
    document = IdCollector()
    document.feed((root / "index.html").read_text())
    js = app_scripts(root)
    referenced = set(re.findall(r'\$\("([a-z0-9-]+)"\)', js))
    # These pages are built once by setupSubpages rather than present in HTML.
    runtime_pages = {"page-settings", "page-played", "page-song"}
    assert referenced <= document.ids | runtime_pages, sorted(
        referenced - document.ids - runtime_pages
    )


def test_app_ui_omits_middle_dot_separator():
    root = Path(__file__).resolve().parents[1] / "static"
    for file in (
        "index.html",
        "app.js",
        "song-detail.js",
        "lyrics.js",
        "song-requests.js",
        "remote-controls.js",
        "admin-playback.js",
        "tv-primary.js",
        "admin-events.js",
        "admin-settings.js",
        "admin-accounts.js",
        "admin-netease.js",
        "tv.html",
        "tv.js",
    ):
        assert "·" not in (root / file).read_text()


def test_tv_control_panel_selectors_exist():
    root = Path(__file__).resolve().parents[1] / "static"
    document = IdCollector()
    document.feed((root / "tv.html").read_text())
    js = (root / "tv.js").read_text()
    referenced = set(re.findall(r'getElementById\("([a-z0-9-]+)"\)', js))
    assert referenced <= document.ids, sorted(referenced - document.ids)


def test_tv_now_next_banner_has_no_popup():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "tv.html").read_text()
    js = (root / "tv.js").read_text()
    assert 'id="song-banner"' in html
    assert 'id="next-up-row"' in html
    assert 'id="next-up"' in html
    assert 'id="next-tip"' not in html
    assert "nextTip" not in js
    assert "updateNextSong(queue[0])" in js


def test_tv_banner_transparency_control_exists():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "tv.html").read_text()
    js = (root / "tv.js").read_text()
    assert 'id="banner-transparency"' in html
    assert 'id="banner-transparency-value"' in html
    assert "background:#151b27c9" in html
    assert "background:#172131f3" in html
    assert "var(--banner-opacity,.85)" in html
    assert "vcktv-banner-transparency" in js


def test_provider_timing_help_explains_same_recording_and_no_word_sweep():
    js = app_scripts(Path(__file__).resolve().parents[1] / "static")
    assert "使用来源原时间轴" in js
    assert "普通歌词对齐" in js
    for warning in (
        "网易云音乐",
        "同一音源",
        "演唱会",
        "改编版",
        "逐字渐变",
        "等分估算",
    ):
        assert warning in js


def test_lyric_cards_are_not_deduplicated_across_recordings():
    js = app_scripts(Path(__file__).resolve().parents[1] / "static")
    assert "const key=[candidate.source,candidate.url,candidate.text].join(" in js


def test_netease_line_review_dialog_is_wired_with_signed_row_selection():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text()
    js = app_scripts(root)
    css = (root / "style.css").read_text()
    for key in (
        "netease-review-dialog",
        "netease-review-lines",
        "netease-review-count",
        "netease-review-align",
        "netease-review-timed",
        "netease-review-back",
    ):
        assert f'id="{key}"' in html
    assert "openNeteaseReview(candidate,useLyrics)" in js
    assert "selected_lines:" in js and "selection_ticket:" in js
    assert ".netease-review-line.kept" in css
    assert "background: #fff0a8" in css
    assert 'id="netease-line-review" class="netease-review-section is-open"' in html
    assert 'id="netease-alignment-choice" class="netease-review-section"' in html
    assert 'aria-controls="netease-review-content"' in html
    assert 'aria-controls="netease-alignment-cards"' in html
    assert 'showNeteaseSection("netease-line-review")' in js
    assert "$(contentId).hidden=!expanded" in js
    assert '$(sectionId).classList.toggle("is-open",expanded)' in js
    assert 'aria-expanded="false" aria-controls="netease-alignment-cards"' in html
    assert ".netease-review-section.is-open { display: flex; flex: 1 1 0;" in css
    assert ".netease-review-section > .netease-accordion-toggle" in css
    assert 'id="netease-review-ai-card"' in html
    assert 'id="netease-review-select-all"' in html
    assert ">使用原时间轴</button>" in html
    assert ">AI 重新对齐</button>" in html
    assert "review-check" in js


def test_tv_html_overlay_and_bandwidth_ui_controls():
    root = Path(__file__).resolve().parents[1] / "static"
    admin = (root / "index.html").read_text()
    admin_js = app_scripts(root)
    tv = (root / "tv.html").read_text()
    player = (root / "tv.js").read_text()
    assert 'id="tv-html-overlay" type="checkbox" checked' in admin
    assert 'id="max-bandwidth-mbps"' in admin
    assert 'id="video-quality"' in admin
    assert 'id="original-codec-mode"' in admin
    assert 'for="video-quality">B站清晰度' in admin
    assert 'for="max-bandwidth-mbps">播放码率预算（Mbps）' in admin
    assert admin.index('id="video-quality"') < admin.index('id="original-codec-mode"')
    assert admin.index('id="original-codec-mode"') < admin.index(
        'id="max-bandwidth-mbps"'
    )
    assert "original_codec_mode:" in admin_js
    assert 'id="compact-video"' not in admin
    assert '"tv_html_overlay"' not in admin  # UI binds via JS.
    assert "tv_html_overlay:" in admin_js
    assert "max_bandwidth_mbps:" in admin_js
    assert 'id="html-captions"' in tv
    assert "tv-overlay.js" in tv
    assert 'id="offset-controls"' in tv
    assert all(f'data-offset="{d}"' in tv for d in (-50, -10, -1, 1, 10, 50))
    assert "loadOverlay(answer.active)" in player
    assert "resetOverlay()" in player


def test_netease_review_is_not_a_second_simultaneous_modal():
    js = app_scripts(Path(__file__).resolve().parents[1] / "static")
    assert "suspendingLyricPicker=true" in js
    assert '$("guest-lyrics-dialog").close();' in js
    assert '$("netease-review-dialog").addEventListener("close"' in js
    assert "if(dialog.open)return;" in js


def test_html_tv_paints_separate_stroke_under_animated_lyrics():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "tv.html").read_text()
    script = (root / "tv-overlay.js").read_text()
    for css in (
        ".html-caption-outline",
        "-webkit-text-stroke:",
        ".html-caption-fill",
        ".html-caption-sweep",
        "-webkit-text-fill-color:transparent",
    ):
        assert css in html
    assert 'shadow.className="html-caption-outline"' in script
    assert 'fill.className="html-caption-fill"' in script
    assert "this.style.outline??3" in script
    assert 'this.style.font||"Noto Sans CJK SC"' in script
    assert 'this.style.base||"#FFFFFF"' in script
    assert 'this.style.highlight||"#FFD53D"' in script


def test_tv_html_font_and_baseline_calibrated_to_ass():
    script = (
        Path(__file__).resolve().parents[1] / "static" / "tv-overlay.js"
    ).read_text()
    assert "const ASS_FONT_TO_CSS=0.69" in script
    assert "const ASS_BASELINE_ADJUST=0.075" in script
    assert "assY-scaledFontSize*ASS_BASELINE_ADJUST" in script


def test_tv_font_fallback_and_admin_preview_share_family_list():
    root = Path(__file__).resolve().parents[1] / "static"
    overlay = (root / "tv-overlay.js").read_text()
    css = (root / "style.css").read_text()
    for family in (
        "Noto Sans SC",
        "Microsoft YaHei",
        "PingFang SC",
        "Hiragino Sans GB",
    ):
        assert f'"{family}"' in overlay
        assert f'"{family}"' in css


def test_guest_auth_uses_native_forms_and_password_visibility_controls():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text()
    js = app_scripts(root)
    for form, button in (
        ("entry-code", "join-btn"),
        ("register-form", "register-btn"),
        ("user-login-form", "user-login-btn"),
        ("login-box", "login-btn"),
    ):
        assert f'id="{form}"' in html
        assert f'id="{button}" type="submit"' in html or (
            f'id="{button}" class="btn primary" type="submit"' in html
        )
        assert f'$("{form}").addEventListener("submit"' in js
    for password in (
        "register-password",
        "register-confirm",
        "user-login-password",
        "password",
    ):
        assert f'data-password-target="{password}"' in html
    assert "button.dataset.passwordTarget" in js


def test_mobile_dialogs_use_tall_scrollable_viewport():
    css = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text()
    assert "dialog, .guest-lyrics-dialog, .netease-review-dialog" in css
    assert "height: 94dvh" in css
    assert "max-height: 94dvh" in css
    assert "overflow-y: auto" in css


def test_guest_request_tabs_include_netease_search_and_share():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text()
    js = app_scripts(root)
    css = (root / "style.css").read_text()
    for source in ("bilibili", "netease"):
        assert f'id="source-tab-{source}" role="tab"' in html
        assert f'id="source-panel-{source}" role="tabpanel"' in html
    assert '<label for="netease-song-query">歌名、歌手或网易云分享链接</label>' in html
    assert '<label for="bvid">' in html
    assert 'id="netease-search-form"' in html
    assert 'id="netease-search-results"' in html
    assert "selectSongSource(source)" in js
    assert "/api/netease/search?q=" in js
    assert "/api/netease/preview?ref=" in js
    assert "/api/netease/songs" in js
    assert "const looksLikeShare=" in js
    assert ".netease-search-result" in css
    assert 'id="netease-order-panel"' not in html


def test_netease_connection_lives_in_global_admin_settings_subpage():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text()
    js = app_scripts(root)
    assert '<section class="panel full-width hidden" id="netease-admin-panel">' in html
    assert '"settings-netease":["网易云音乐连接","netease-admin-panel"]' in js
    assert '["settings-netease","网易云音乐","音乐源连接"]' in js
    assert '["settings-accounts","settings-netease"]' in js
    assert 'currentRoute()==="settings-netease"&&!neteaseLoginPageActive' in js
    assert 'id="netease-login-check"' in html
    assert '$("netease-login-status").textContent=e.message' in js


def test_netease_cookie_fallback_global_admin_ui():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text()
    js = app_scripts(root)
    assert 'id="netease-cookie-import-panel"' in html
    assert 'id="netease-cookie-import-value"' in html
    assert 'type="password" id="netease-cookie-import-value"' in html
    assert 'id="netease-cookie-import-form"' in html
    assert "/api/admin/netease/import" in js
    assert 'if(state.phase==="verification_required")' in js
    assert 'state.phase==="verification_required")stopNeteaseLoginPolling()' in js
    assert 'finally{field.value="";}' in js


def test_provider_tag_and_copy_are_source_aware():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text()
    js = app_scripts(root)
    css = (root / "style.css").read_text()
    tv_js = (root / "tv.js").read_text()
    assert "📺 B站" in js and "🎵 网易云" in js
    assert 'song.source_type==="netease"' in js
    assert '"https://music.163.com/#/song?id="+encodeURIComponent(song.source_id)' in js
    assert 'song.bvid||""' in js
    assert 'id="current-song-copy-source"' in html
    assert 'id="current-song-copy-bv"' not in html
    assert 'class="source-tag"' not in html  # Only actual songs get a tag.
    assert ".source-tag" in css
    assert 'song.source_type==="netease"' in tv_js
    assert "/download/retry" in js


def test_primary_tv_controls_keep_labels_minimal():
    root = Path(__file__).resolve().parents[1] / "static"
    admin = (root / "index.html").read_text()
    admin_js = app_scripts(root)
    tv_js = (root / "tv.js").read_text()
    assert 'id="tv-primary-status"' in admin
    assert 'id="tv-primary-status"></p>' in admin
    assert "当前主屏" not in admin_js
    assert "▶ 开始播放（当前主屏）" not in tv_js
    assert 'start.textContent="▶ 开始"' in tv_js


def test_tv_replacement_has_separate_open_and_copy_actions():
    js = app_scripts(Path(__file__).resolve().parents[1] / "static")
    assert 'tv.active?"▣ 更换主屏":"▣ 打开主屏"' in js
    assert 'tv.active?"⧉ 复制更换链接":"⧉ 复制主屏链接"' in js
    assert '$("tv-copy-btn").classList.toggle("hidden",tv.active' not in js
    assert 'const tab=window.open("about:blank","_blank");' in js
    assert "if(link)await copyTVLink(link);" in js


def test_button_like_links_and_buttons_share_center_alignment():
    css = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text()
    shared_button_rule = re.search(r"\.btn\s*\{([^}]+)\}", css)
    assert shared_button_rule is not None
    assert re.search(r"text-align:\s*center\s*;", shared_button_rule.group(1))


def test_account_dropdown_shows_name_above_logout():
    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text()
    dropdown = html.split('class="account-dropdown"', 1)[1].split("</details>", 1)[0]
    assert dropdown.index('id="account-name"') < dropdown.index('id="account-logout"')
    summary = html.split('id="account-menu"', 1)[1].split("</summary>", 1)[0]
    assert 'id="account-name"' not in summary


def test_tv_internal_controls_use_external_remote_icons():
    html = (Path(__file__).resolve().parents[1] / "static" / "tv.html").read_text()
    for icon in ("pause", "rotate-ccw", "skip-forward", "repeat-2", "volume-2"):
        assert f"/icons/{icon}.svg" in html
    for provider in ("bilibili", "neteasecloudmusic"):
        assert f"/icons/{provider}.svg" in html
    for control in ("pause-resume", "restart", "skip", "replay-next", "next-list"):
        assert f'id="{control}"' in html
    assert "grid-template-columns:repeat(4,minmax(0,1fr))" in html


def test_public_product_name_is_consistent():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text()
    tv = (root / "static" / "tv.html").read_text()
    app = (root / "static" / "app.js").read_text()
    assert "<strong>VC-KTV</strong>" in html
    assert "<title>VC-KTV |" in html
    assert "<title>VC-KTV |" in tv
    assert 'document.title="VC-KTV | "' in app
