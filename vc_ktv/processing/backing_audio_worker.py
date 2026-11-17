"""Isolated optional Direct Karaoke CUDA inference; invoked by BackingTracks."""

# pyright: reportMissingImports=false
# CUDA and Kim source modules exist only in the optional separation runtime.
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    import numpy as np
    import soundfile as sf
    import torch
    import yaml
    from ml_collections import ConfigDict

    req = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    sys.path.insert(0, str(req["model_source"]))
    from models.mel_band_roformer import MelBandRoformer
    from utils import demix_track

    if not torch.cuda.is_available():
        raise RuntimeError("Direct Karaoke requires CUDA")
    config_path = (
        Path(req["model_source"]) / "configs/config_vocals_mel_band_roformer.yaml"
    )
    config = ConfigDict(
        yaml.load(config_path.read_text(encoding="utf-8"), Loader=yaml.FullLoader)
    )
    config.inference.num_overlap = 4
    model = MelBandRoformer(**dict(config.model))
    model.load_state_dict(
        torch.load(req["checkpoint"], map_location="cpu", weights_only=True),
        strict=True,
    )
    model = model.eval().cuda()
    mixture, rate = sf.read(req["mixture"], dtype="float32", always_2d=True)
    if rate != 44100 or mixture.shape[1] != 2:
        raise ValueError("Expected 44.1 kHz stereo source audio")
    with torch.inference_mode():
        target, _ = demix_track(
            config, model, torch.from_numpy(mixture.T.copy()), "cuda"
        )
    lead = target["vocals"].T
    if lead.shape != mixture.shape or not np.isfinite(lead).all():
        raise ValueError("Invalid model output")
    backing = mixture - lead
    if not np.isfinite(backing).all():
        raise ValueError("Invalid backing audio")
    peak = float(np.max(np.abs(backing)))
    if peak > 0.99:
        backing *= 0.99 / peak
    sf.write(req["output"], backing, rate, subtype="FLOAT")


if __name__ == "__main__":
    main()
