"""Portable application paths; machine-specific runtimes stay outside Git."""

import os
from pathlib import Path

from ktv_app.config import Settings


def pipeline_settings(root: Path | None = None) -> Settings:
    """Resolve installed models and executables from a configurable data root."""
    root = Path(
        root or os.getenv("VC_KTV_PIPELINE_ROOT", "~/.local/share/vc-ktv/pipeline")
    ).expanduser()
    models = root / "models"
    runtime = root / "runtime"
    return Settings(
        tifa_source=models / "forced_alignment/sources/TIFA",
        tifa_python=runtime / "tifa/bin/python",
        tifa_model=models / "forced_alignment/tifa/TIFA-1.0-ST/model.pt",
        kim_source=models / "separation/sources/Kim",
        kim_model=models / "separation/Kim/MelBandRoformer.ckpt",
        separation_python=runtime / "kim/bin/python",
        asr_model=models / "Qwen3-ASR-1.7B",
        locator_python=runtime / "qwen/bin/python",
    )
