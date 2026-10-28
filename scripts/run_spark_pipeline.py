#!/usr/bin/env python3
"""Run the adopted model pipeline on an isolated media/lyrics pair (never DB)."""

import argparse
import json
from pathlib import Path

from ktv_app.config import Settings
from vc_ktv.upstream_pipeline import make_karaoke_video


def spark_settings(root: Path, tifa_root: Path) -> Settings:
    models = root / "models"
    runtime = root / "runtime"
    return Settings(
        tifa_source=tifa_root / ".runtime/TIFA",
        tifa_python=tifa_root / ".runtime/tifa-env/bin/python",
        tifa_model=tifa_root / ".runtime/models/TIFA-1.0-ST/model.pt",
        kim_source=models / "separation/sources/Kim",
        kim_model=models / "separation/Kim/MelBandRoformer.ckpt",
        separation_python=runtime / "kim/bin/python",
        asr_model=models / "Qwen3-ASR-1.7B",
        locator_python=runtime / "qwen/bin/python",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--lyrics", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.home() / ".local/share/vc-ktv-pipeline-staging",
    )
    parser.add_argument(
        "--existing-tifa-root", type=Path, default=Path.home() / "Code/vc-ktv"
    )
    args = parser.parse_args()
    result = make_karaoke_video(
        args.source,
        args.lyrics.read_text(encoding="utf-8"),
        args.out,
        spark_settings(
            args.root.expanduser().resolve(), args.existing_tifa_root.resolve()
        ),
        progress=lambda event: print(event, flush=True),
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
