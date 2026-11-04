"""Create a separate, pinned TIFA runtime from an explicitly supplied CUDA torch.

Verified on Linux ARM64 / WSL, Python 3.12, CUDA 13.0.
The supplied torch environment must outlive TIFA; neither is copied or modified.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REQUIREMENTS = (
    REPO / "third_party/autoassrenderer/tifa-requirements-spark-arm64-cu130.txt"
)
TORCH_VERSION = "2.13.0+cu130"
AUDIO_VERSION = "2.11.0+cu130"

SOURCE_PROBE = """
import json, pathlib, site, sys, torch, torchaudio
root = pathlib.Path(site.getsitepackages()[0]).resolve()
print(json.dumps({
    "python": list(sys.version_info[:2]),
    "site": str(root),
    "torch": torch.__version__,
    "audio": torchaudio.__version__,
    "cuda": torch.version.cuda,
    "device": torch.cuda.is_available(),
    "direct": (root / "torch").is_dir()
        and pathlib.Path(torch.__file__).resolve().is_relative_to(root)
        and (root / "torchaudio").is_dir()
        and pathlib.Path(torchaudio.__file__).resolve().is_relative_to(root),
}))
"""

VERIFY_TIFA = """
import os, sys, torch, torchaudio
import g2pflow, librosa, lightning, omegaconf, textgrid
sys.path.insert(0, os.environ["VC_KTV_TIFA_SOURCE"])
from inference.api import load_g2p_config, load_inference_model
from lib.g2p import build_pipeline_from_config
assert torch.cuda.is_available() and torch.version.cuda == "13.0"
assert torch.__version__ == "2.13.0+cu130"
assert torchaudio.__version__ == "2.11.0+cu130"
print("TIFA imports + CUDA torch verified")
"""


def run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 45,
) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.CalledProcessError as error:
        details = (error.stderr or error.stdout or "").strip()[-1500:]
        raise RuntimeError(
            f"{command[0]} exited {error.returncode}: {details}"
        ) from error
    return result.stdout.strip()


def inspect_source(python: Path) -> Path:
    if not python.is_file():
        raise FileNotFoundError(f"CUDA torch Python not found: {python}")
    try:
        data = json.loads(run([str(python), "-c", SOURCE_PROBE]).splitlines()[-1])
    except (subprocess.SubprocessError, ValueError, IndexError) as error:
        raise RuntimeError("Cannot inspect supplied CUDA torch interpreter") from error
    if (
        data["python"] != [3, 12]
        or data["torch"] != TORCH_VERSION
        or data["audio"] != AUDIO_VERSION
        or data["cuda"] != "13.0"
        or not data["device"]
        or not data["direct"]
    ):
        raise RuntimeError(
            "CUDA source must directly contain working Python 3.12 "
            f"torch=={TORCH_VERSION} and torchaudio=={AUDIO_VERSION}; "
            "indirect .pth links and CPU-only wheels are not supported"
        )
    return Path(data["site"])


def setup(root: Path, torch_python: Path) -> Path:
    root = root.expanduser().resolve()
    target = root / "runtime/tifa"
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Refusing to modify existing TIFA runtime: {target}")
    source = root / "models/forced_alignment/sources/TIFA"
    if not (source / "infer.py").is_file():
        raise FileNotFoundError(
            f"TIFA source missing at {source}; run setup_spark_assets first"
        )
    if platform.system() != "Linux":
        raise RuntimeError("This CUDA/TIFA runtime setup requires Linux")
    # venv/bin/python is usually a symlink to the base interpreter.
    # resolve() would bypass pyvenv.cfg and lose the venv's CUDA packages.
    shared_site = inspect_source(torch_python.expanduser().absolute())
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(["uv", "venv", "--python", "3.12", str(target)])
        run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(target / "bin/python"),
                "--no-deps",
                "-r",
                str(REQUIREMENTS),
            ],
            timeout=900,
        )
        site = Path(
            run(
                [
                    str(target / "bin/python"),
                    "-c",
                    "import site; print(site.getsitepackages()[0])",
                ]
            )
        )
        if not site.resolve().is_relative_to(target):
            raise RuntimeError("New TIFA site-packages is outside the target runtime")
        (site / "explicit_cuda_torch.pth").write_text(
            f"{shared_site}\n", encoding="utf-8"
        )
        run(
            [str(target / "bin/python"), "-c", VERIFY_TIFA],
            env={**os.environ, "VC_KTV_TIFA_SOURCE": str(source)},
        )
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    print(f"TIFA CUDA runtime ready at {target}; shared CUDA torch: {shared_site}")
    print("The external CUDA torch environment must remain available and unchanged.")
    return target / "bin/python"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Pipeline asset root")
    parser.add_argument(
        "--torch-python",
        type=Path,
        required=True,
        help="Independent Python 3.12 venv with direct CUDA torch+torchaudio wheels",
    )
    args = parser.parse_args()
    setup(args.root, args.torch_python)


if __name__ == "__main__":
    main()
