"""Notify on newer commits in the upstream repository copied into ktv_app/.

Uses the GitHub CLI supplied by GitHub Actions; never changes vendored code.
"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

PIN_FILE = (
    Path(__file__).resolve().parents[1] / "third_party/autoassrenderer/UPSTREAM.json"
)
LABEL = "upstream-update"
MARKER = "<!-- vc-ktv-upstream-check -->"


def gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def valid_sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError(f"Invalid upstream SHA: {value!r}")
    return value


def check(*, dry_run: bool = False, head: str | None = None) -> str:
    if head is not None and not dry_run:
        raise ValueError("--head may only be used with --dry-run")

    pin = json.loads(PIN_FILE.read_text(encoding="utf-8"))
    url = urlparse(pin["repository"])
    path = url.path.strip("/")
    if (
        url.scheme != "https"
        or url.netloc != "github.com"
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path)
    ):
        raise ValueError("UPSTREAM.json must identify a GitHub repository")
    ref = pin["ref"]
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", ref):
        raise ValueError("Invalid upstream ref")
    pinned = valid_sha(pin["commit"])
    current = valid_sha(
        head
        if head is not None
        else gh("api", f"repos/{path}/commits/{ref}", "--jq", ".sha")
    )
    if current == pinned:
        return f"Upstream unchanged at {pinned[:12]}; no notification"

    repository = os.environ["GITHUB_REPOSITORY"]
    issues = json.loads(
        gh(
            "issue",
            "list",
            "-R",
            repository,
            "--label",
            LABEL,
            "--state",
            "all",
            "--limit",
            "1000",
            "--json",
            "number,state,body",
        )
    )
    tracked = [issue for issue in issues if MARKER in issue.get("body", "")]
    # A dismissed alert for this exact SHA must not reappear every Monday.
    if any(f"<!-- upstream-head:{current} -->" in issue["body"] for issue in tracked):
        return f"Already notified about {current[:12]}; no notification"

    title = f"Upstream AutoASSrenderer4karaOK advanced to {current[:12]}"
    body = (
        f"{MARKER}\n<!-- upstream-head:{current} -->\n\n"
        "The vendored AutoASSrenderer4karaOK source has a newer upstream HEAD.\n\n"
        f"- Pinned: [`{pinned[:12]}`](https://github.com/{path}/commit/{pinned})\n"
        f"- Upstream `{ref}`: [`{current[:12]}`](https://github.com/{path}/commit/{current})\n"
        f"- [Compare changes](https://github.com/{path}/compare/{pinned}...{current})\n"
        "- Recorded source pin: `third_party/autoassrenderer/UPSTREAM.json`\n"
        "- Model asset lock: `third_party/autoassrenderer/assets.lock.json`\n\n"
        "**Manual review only.** Compare vendored files, preserve attribution, "
        "test the adapted pipeline and update the pinned SHA only after merging "
        "a reviewed PR. Do not upgrade model weights or production automatically.\n\n"
        "Tracked by #31.\n"
    )
    existing = next((issue for issue in tracked if issue["state"] == "OPEN"), None)
    if existing:
        action = f"Update issue #{existing['number']} to {current[:12]}"
        if not dry_run:
            gh(
                "issue",
                "edit",
                str(existing["number"]),
                "-R",
                repository,
                "--title",
                title,
                "--body",
                body,
            )
    else:
        action = f"Create upstream update issue for {current[:12]}"
        if not dry_run:
            gh(
                "label",
                "create",
                LABEL,
                "-R",
                repository,
                "--force",
                "--color",
                "57606a",
                "--description",
                "Vendored source has moved upstream",
            )
            gh(
                "issue",
                "create",
                "-R",
                repository,
                "--label",
                LABEL,
                "--title",
                title,
                "--body",
                body,
            )
    return f"DRY RUN: {action}" if dry_run else action


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--head", help="Simulate a specific SHA (only with --dry-run)")
    args = parser.parse_args()
    print(check(dry_run=args.dry_run, head=args.head))


if __name__ == "__main__":
    main()
