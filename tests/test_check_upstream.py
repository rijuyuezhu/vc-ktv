"""No network, gh credentials, or issue writes required for notifier tests."""

import json

import pytest

from scripts import check_upstream as notifier

PIN = json.loads(notifier.PIN_FILE.read_text(encoding="utf-8"))["commit"]
NEW = "1234567890abcdef1234567890abcdef12345678"
NEWER = "abcdef1234567890abcdef1234567890abcdef12"


def test_no_change_does_not_even_list_issues(monkeypatch):
    calls = []

    def fake_gh(*args):
        calls.append(args)
        return PIN

    monkeypatch.setattr(notifier, "gh", fake_gh)
    assert "unchanged" in notifier.check()
    assert len(calls) == 1 and calls[0][0] == "api"


def test_dry_run_simulated_sha_without_any_writes(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "rijuyuezhu/vc-ktv")
    calls = []

    def fake_gh(*args):
        calls.append(args)
        assert args[:2] == ("issue", "list")
        return "[]"

    monkeypatch.setattr(notifier, "gh", fake_gh)
    assert notifier.check(dry_run=True, head=NEW) == (
        "DRY RUN: Create upstream update issue for 1234567890ab"
    )
    assert len(calls) == 1


def test_new_version_creates_issue_with_label_and_compare_link(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "rijuyuezhu/vc-ktv")
    calls = []

    def fake_gh(*args):
        calls.append(args)
        if args[:2] == ("issue", "list"):
            return "[]"
        return "ok"

    # Direct non-dry-run path uses the real HEAD lookup, mocked here.

    def fake_gh_with_head(*args):
        if args[0] == "api":
            return NEW
        return fake_gh(*args)

    monkeypatch.setattr(notifier, "gh", fake_gh_with_head)
    assert "Create upstream update issue" in notifier.check()
    assert [call[:2] for call in calls] == [
        ("issue", "list"),
        ("label", "create"),
        ("issue", "create"),
    ]
    body = calls[-1][calls[-1].index("--body") + 1]
    assert f"<!-- upstream-head:{NEW} -->" in body
    assert f"/compare/{PIN}...{NEW}" in body
    assert "assets.lock.json" in body


def test_open_existing_issue_updated_once(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "rijuyuezhu/vc-ktv")
    calls = []
    existing = {
        "number": 42,
        "state": "OPEN",
        "body": f"{notifier.MARKER}\n<!-- upstream-head:{NEW} -->",
    }

    def fake_gh(*args):
        calls.append(args)
        if args[0] == "api":
            return NEWER
        if args[:2] == ("issue", "list"):
            return json.dumps([existing])
        return "ok"

    monkeypatch.setattr(notifier, "gh", fake_gh)
    assert notifier.check() == "Update issue #42 to abcdef123456"
    assert calls[-1][:3] == ("issue", "edit", "42")


@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
def test_same_sha_already_reported_is_deduplicated(monkeypatch, state):
    monkeypatch.setenv("GITHUB_REPOSITORY", "rijuyuezhu/vc-ktv")
    calls = []

    def fake_gh(*args):
        calls.append(args)
        if args[0] == "api":
            return NEW
        return json.dumps(
            [
                {
                    "number": 51,
                    "state": state,
                    "body": f"{notifier.MARKER}\n<!-- upstream-head:{NEW} -->",
                },
            ]
        )

    monkeypatch.setattr(notifier, "gh", fake_gh)
    assert "Already notified" in notifier.check()
    assert len(calls) == 2


def test_closed_old_notification_allows_a_new_revision(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "rijuyuezhu/vc-ktv")
    calls = []

    def fake_gh(*args):
        calls.append(args)
        if args[0] == "api":
            return NEWER
        if args[:2] == ("issue", "list"):
            return json.dumps(
                [
                    {
                        "number": 52,
                        "state": "CLOSED",
                        "body": f"{notifier.MARKER}\n<!-- upstream-head:{NEW} -->",
                    }
                ]
            )
        return "ok"

    monkeypatch.setattr(notifier, "gh", fake_gh)
    assert "Create upstream update issue" in notifier.check()
    assert calls[-1][:2] == ("issue", "create")


def test_only_bot_owned_open_issue_can_be_edited(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "rijuyuezhu/vc-ktv")
    calls = []

    def fake_gh(*args):
        calls.append(args)
        if args[0] == "api":
            return NEW
        if args[:2] == ("issue", "list"):
            return json.dumps([{"number": 17, "state": "OPEN", "body": "manual issue"}])
        return "ok"

    monkeypatch.setattr(notifier, "gh", fake_gh)
    assert "Create" in notifier.check()
    assert not any(args[:2] == ("issue", "edit") for args in calls)


def test_simulation_never_permits_issue_write():
    with pytest.raises(ValueError, match="dry-run"):
        notifier.check(head=NEW)
    with pytest.raises(ValueError, match="Invalid upstream SHA"):
        notifier.check(dry_run=True, head="not-a-sha")
