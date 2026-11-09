"""Protect reader-facing Markdown links from becoming stale."""

import re
from pathlib import Path


def test_relative_documentation_links_exist():
    root = Path(__file__).resolve().parents[1]
    documents = [
        root / "README.md",
        *sorted((root / "docs").glob("*.md")),
        root / "third_party/autoassrenderer/README.md",
    ]
    missing = []
    for document in documents:
        for url in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text()):
            target = url.split("#", 1)[0].split("?", 1)[0]
            if not target or target.startswith(("https://", "http://", "mailto:")):
                continue
            if not (document.parent / target).exists():
                missing.append(f"{document.relative_to(root)}: {url}")
    assert not missing, "Broken relative documentation links:\n" + "\n".join(missing)
