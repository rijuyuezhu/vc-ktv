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
        "settings-save-btn",
        "event-archive-panel",
        "event-history",
    ):
        assert 'id="' + element + '"' in html
    assert 'api("/api/admin/settings")' in js
    assert 'api("/api/admin/settings","PUT"' in js
    assert '"/songs.csv"' in js
