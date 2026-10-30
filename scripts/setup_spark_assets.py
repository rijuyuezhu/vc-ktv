#!/usr/bin/env python3
"""Download upstream-pinned Kim/Qwen model assets for Spark-WSL.

All pinned assets are installed directly in VCKTV's permanent pipeline directory.
The separately provisioned TIFA source/weights remain independent of the repo.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

LOCK = (
    Path(__file__).resolve().parents[1] / "third_party/autoassrenderer/assets.lock.json"
)
ASSETS = json.loads(LOCK.read_text(encoding="utf-8"))["assets"]
CHOICES = {"kim", "qwen", "all"}


def selected(group):
    for item in ASSETS:
        name = item["name"]
        if (
            group in ("kim", "all")
            and name in ("kim-source.zip", "Kim-MelBandRoformer.ckpt")
        ) or (group in ("qwen", "all") and name.startswith("qwen-")):
            yield item


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(item, root):
    target = root / item["target"]
    sha = item["sha256"]
    if target.is_file() and digest(target) == sha:
        return "cached"
    if target.is_dir() and (target / ".asset.sha256").is_file():
        if (target / ".asset.sha256").read_text().strip() == sha:
            return "cached"
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = root / "downloads" / item["name"]
    staged.parent.mkdir(parents=True, exist_ok=True)
    if staged.exists() and digest(staged) == sha:
        pass
    else:
        links = [item["url"]]
        if item["url"].startswith("https://huggingface.co/"):
            links.insert(
                0,
                item["url"].replace(
                    "https://huggingface.co/", "https://hf-mirror.com/", 1
                ),
            )
        for link in links:
            result = subprocess.run(
                [
                    "curl",
                    "--silent",
                    "--show-error",
                    "--fail",
                    "--location",
                    "--retry",
                    "2",
                    "--retry-delay",
                    "2",
                    "--connect-timeout",
                    "20",
                    "--continue-at",
                    "-",
                    "--output",
                    str(staged),
                    link,
                ],
                check=False,
            )
            if result.returncode == 0 and digest(staged) == sha:
                break
        else:
            raise RuntimeError(f"Download failed or SHA256 mismatch: {item['name']}")
    if item["kind"] == "zip":
        if target.exists():
            raise RuntimeError(f"Refusing to overwrite existing source tree: {target}")
        with tempfile.TemporaryDirectory(dir=target.parent) as d:
            temporary = Path(d) / "extracted"
            temporary.mkdir()
            with zipfile.ZipFile(staged) as z:
                for record in z.infolist():
                    parts = Path(record.filename).parts[item.get("strip", 0) :]
                    if not parts:
                        continue
                    if any(part in ("", ".", "..") for part in parts):
                        raise ValueError("Unsafe archive entry")
                    if (record.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("Symlink in source archive")
                    out = temporary.joinpath(*parts)
                    if record.is_dir():
                        out.mkdir(parents=True, exist_ok=True)
                    else:
                        out.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(record) as stream, out.open("wb") as dest:
                            shutil.copyfileobj(stream, dest)
            (temporary / ".asset.sha256").write_text(sha + "\n")
            os.replace(temporary, target)
    else:
        os.replace(staged, target)
    return "downloaded"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=sorted(CHOICES))
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.home() / ".local/share/vc-ktv/pipeline",
    )
    args = parser.parse_args()
    for item in selected(args.group):
        status = download(item, args.root.expanduser().resolve())
        print(f"{status}: {item['name']}", flush=True)


if __name__ == "__main__":
    main()
