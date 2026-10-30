from pathlib import Path


def test_no_escaped_javascript_template_delimiters():
    script = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    assert "\\`" not in script
    assert "\\${" not in script


def test_admin_has_stable_subpage_navigation():
    static = Path(__file__).resolve().parents[1] / "static"
    html = (static / "index.html").read_text()
    script = (static / "app.js").read_text()

    assert 'href="/admin/"' in html
    assert 'href="/"' in html
    assert 'role="tablist"' not in html
    assert "window.location.pathname" in script
    assert "showPage();" in script
    assert "showTab(" not in script


def test_ui_uses_light_indigo_palette():
    static = Path(__file__).resolve().parents[1] / "static"
    css = (static / "style.css").read_text()
    assert "--bg: #f6f7f9" in css
    assert "--surface: #ffffff" in css
    assert "--accent: #4f5bd5" in css
    assert "color-scheme: light" in css
    assert "focus-visible" in css


def test_event_entry_has_join_register_login_and_logout_controls():
    html = (Path(__file__).resolve().parents[1] / "static/index.html").read_text()
    script = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    for control in (
        "join-code",
        "join-btn",
        "register-name",
        "register-password",
        "register-confirm",
        "register-btn",
        "user-login-name",
        "user-login-password",
        "user-login-btn",
        "user-logout-btn",
    ):
        assert ('id="' + control + '"') in html
    assert 'api("/api/entry")' in script
    assert 'api("/api/join","POST"' in script
    assert 'api("/api/users/register","POST"' in script
    assert 'api("/api/users/logout","POST")' in script


def test_state_refresh_preserves_unchanged_song_list_and_recovers():
    script = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    assert "if(listKeys.get(id)===key)return;" in script
    assert 'document.addEventListener("visibilitychange"' in script
    assert 'window.addEventListener("online"' in script
    assert "if(refreshQueued){refreshQueued=false;refresh();}" in script


def test_admin_policy_and_archive_panel_elements():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text()
    js = (root / "app.js").read_text()
    for element in (
        "event-settings-panel",
        "queue-policy",
        "allow-unsubtitled",
        "compact-video",
        "settings-save-btn",
        "event-archive-panel",
        "event-history",
    ):
        assert 'id="' + element + '"' in html
    assert 'api("/api/admin/settings")' in js
    assert 'api("/api/admin/settings","PUT"' in js
    assert 'compact_video:$("compact-video").checked' in js
    assert '"/songs.csv"' in js


def test_ui_never_uses_middle_dot_separator():
    static = Path(__file__).resolve().parents[1] / "static"
    for name in ("index.html", "app.js", "tv.html", "tv.js"):
        assert "·" not in (static / name).read_text(), name


def test_essentials_remain_accessible_after_ui_cleanup():
    static = Path(__file__).resolve().parents[1] / "static"
    html = (static / "index.html").read_text()
    for identifier in (
        "guest-access",
        "request-panel",
        "queue-panel",
        "guest-lyrics-dialog",
        "queue-policy",
        "allow-unsubtitled",
        "compact-video",
        "settings-save-btn",
        "event-history",
        "player",
        "admin-advanced",
        "manage-list",
        "stats",
    ):
        assert f'id="{identifier}"' in html
    assert (
        '<details class="panel full-width fold-panel" id="event-archive-panel">' in html
    )
    assert (
        '<details class="panel full-width fold-panel" id="performance-panel">' in html
    )


def test_event_admin_login_and_account_controls_present():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text()
    js = (root / "app.js").read_text()
    for ident in (
        "admin-username",
        "admin-role",
        "admin-accounts-panel",
        "admin-account-name",
        "admin-account-password",
        "admin-account-create",
        "admin-account-list",
    ):
        assert ('id="' + ident + '"') in html
    assert 'api("/api/admin/accounts")' in js
    assert 'data.role==="global"' in js
    assert 'api("/api/admin/login","POST"' in js


def test_full_page_admin_navigation_and_register_first_auth():
    from pathlib import Path

    static_dir = Path(__file__).resolve().parents[1] / "static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    js = (static_dir / "app.js").read_text(encoding="utf-8")
    css = (static_dir / "style.css").read_text(encoding="utf-8")
    assert 'id="auth-register"' in html
    assert 'id="auth-login"' in html and 'auth-single hidden" id="auth-login"' in html
    assert 'id="show-user-login"' in html and "已有账号？登录" in html
    assert 'id="show-user-register"' in html
    assert 'id="subpage"' in html and 'id="subpage-back"' in html
    assert 'href="#settings"' in html and 'href="#stats"' in html
    for key in ("settings-playback", "settings-event", "settings-history", "stats"):
        assert key in js
    assert 'window.addEventListener("hashchange"' in js
    assert ".subpage { max-width: 960px;" in css


def test_song_rows_open_full_page_detail_and_guest_played_downloads():
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'href="#played"' in html
    assert 'id="current-song-download"' in html
    assert 'window.location.hash="song-"+song.id' in js
    assert "const authenticated=isAdmin?admin:guestAuthenticated" in js
    assert 'api("/api/songs/played")' in js
    assert "api(`/api/songs/${id}/detail`)" in js
    assert "detail-download" in js
    assert '/api/songs/"+song.id+"/cancel' in js


def test_song_detail_cold_review_keeps_keyboard_and_manual_controls_safe():
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert "if(e.target!==outer)return;" in js
    assert 'extras.append($("lead-seconds"))' not in js
    assert 'lead.oninput=()=>{$("lead-seconds").value=lead.value;}' in js
    assert "const timingWasOpen=Boolean(" in js
    assert "details.open=timingWasOpen" in js
    assert js.count('$("manual-download").classList.add("hidden")') == 1
    assert html.count('id="current-song-download"') == 1
    assert html.index('id="current-song-download"') < html.index('id="playback-mode"')
    assert "const pending=[...adm.queue,...adm.waiting]" in js
