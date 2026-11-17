"""Optional CUDA worker for the offline backing-vocals POC.

Uses the verified MelBand implementation already bundled with VC-KTV's
optional Kim runtime. Model checkpoints and source trees remain outside Git.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path


def main() -> None:
    # This worker runs in the optional separation runtime, not the server.
    import numpy as np
    import soundfile as sf
    import torch
    import yaml
    from ml_collections import ConfigDict

    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    source = Path(request["model_source"])
    checkpoint = Path(request["checkpoint"])
    mixture = Path(request["mixture"])
    output_dir = Path(request["output_dir"])
    role = request["role"]
    if role not in ("vocals", "karaoke"):
        raise ValueError(f"Unknown model role: {role}")
    if not torch.cuda.is_available():
        raise RuntimeError("The lead/backing vocals POC requires a CUDA device")

    sys.path.insert(0, str(source))
    from models.mel_band_roformer import MelBandRoformer
    from utils import demix_track

    config_path = source / "configs/config_vocals_mel_band_roformer.yaml"
    config = ConfigDict(
        yaml.load(config_path.read_text(encoding="utf-8"), Loader=yaml.FullLoader)
    )
    config.inference.num_overlap = 2 if role == "vocals" else 4

    started = time.monotonic()
    with checkpoint.open("rb") as f:
        checkpoint_hash = hashlib.file_digest(f, "sha256").hexdigest()
    model = MelBandRoformer(**dict(config.model))
    model.load_state_dict(
        torch.load(checkpoint, map_location="cpu", weights_only=True), strict=True
    )
    model = model.eval().cuda()

    data, samplerate = sf.read(mixture, dtype="float32", always_2d=True)
    if samplerate != 44100 or data.shape[1] != 2:
        raise ValueError("Expected 44.1 kHz stereo input")
    with torch.inference_mode():
        stems, _ = demix_track(config, model, torch.from_numpy(data.T.copy()), "cuda")

    target = stems[config.training.target_instrument].T
    if target.shape != data.shape or not np.isfinite(target).all():
        raise ValueError("Target stem is invalid or not aligned with input")

    output_dir.mkdir(parents=True, exist_ok=True)
    sf.write(output_dir / "vocals.wav", target, samplerate, subtype="FLOAT")
    sf.write(output_dir / "other.wav", data - target, samplerate, subtype="FLOAT")
    (output_dir / "model-run.json").write_text(
        json.dumps(
            {
                "role": role,
                "checkpoint_sha256": checkpoint_hash,
                "overlap": config.inference.num_overlap,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "peak_gpu_gib": round(torch.cuda.max_memory_allocated() / 1024**3, 3),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
