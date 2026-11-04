#!/usr/bin/env python3
"""Prepare separate Kim/Qwen runtime overlays, reusing existing read-only CUDA torch.

Does not modify the pre-existing production TIFA venv or shared CUDA torch.
"""

import argparse
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE_ROOT = Path(
    os.getenv("VC_KTV_PIPELINE_ROOT", "~/.local/share/vc-ktv/pipeline")
).expanduser()


def tifa_python(root: Path, override: Path | None = None) -> Path:
    return override or root / "runtime/tifa/bin/python"


def python_output(python, expression):
    return subprocess.check_output([str(python), "-c", expression], text=True).strip()


def require_cuda(python):
    if (
        python_output(python, "import torch; print(torch.cuda.is_available())")
        != "True"
    ):
        raise RuntimeError(f"CUDA torch unavailable in {python}")


def setup(group, staging, shared_python):
    if group not in ("kim", "qwen"):
        raise ValueError(group)
    target = staging / "runtime" / group
    python = target / "bin/python"
    if not shared_python.is_file():
        raise FileNotFoundError(
            f"TIFA CUDA Python not found: {shared_python}. "
            "Provision a working CUDA/TIFA runtime before installing overlays."
        )
    # Fail before creating or mutating a runtime overlay.
    require_cuda(shared_python)
    if not python.exists():
        subprocess.run(["uv", "venv", "--python", "3.12", str(target)], check=True)
    shared_packages = python_output(
        shared_python, "import site; print(site.getsitepackages()[0])"
    )
    torch_dir = python_output(
        shared_python,
        "import torch; from pathlib import Path; print(Path(torch.__file__).resolve().parent.parent)",
    )
    # Use read-only, already functioning CUDA PyTorch and TIFA runtime packages;
    # local installs in these separate overlays always win first.
    overlay_site = Path(
        python_output(python, "import site; print(site.getsitepackages()[0])")
    )
    (overlay_site / "shared_cuda_readonly.pth").write_text(
        f"{shared_packages}\n{torch_dir}\n", encoding="utf-8"
    )
    requirements = (
        ROOT
        / "third_party/autoassrenderer"
        / ("kim-requirements.txt" if group == "kim" else "qwen-requirements.txt")
    )
    # No-deps avoids overwriting the known-good shared CUDA PyTorch with an
    # incompatible ARM64 CPU torch wheel. Missing imports are diagnosed later.
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            "-r",
            str(requirements),
        ],
        check=True,
    )
    if group == "qwen":
        # The shared CUDA toolchain has tokenizers 0.23.3 (too new for
        # transformers 4.57.6), and Nagisa needs ARM64 DyNet38.
        # Both have aarch64 CPython 3.12 wheels; no source build is needed.
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                "tokenizers==0.22.2",
                "DyNet38==2.2",
            ],
            check=True,
        )
    print(group, python, flush=True)
    require_cuda(python)
    # The Qwen package eagerly imports Nagisa even for ASR-only inference.
    if group == "qwen":
        subprocess.run(
            [str(python), "-c", "import torch, tokenizers, dynet, nagisa, qwen_asr"],
            check=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=("kim", "qwen", "all"))
    parser.add_argument("--root", type=Path, default=PIPELINE_ROOT)
    parser.add_argument("--tifa-python", type=Path)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    override = args.tifa_python.expanduser().absolute() if args.tifa_python else None
    for group in ("kim", "qwen") if args.group == "all" else (args.group,):
        setup(group, root, tifa_python(root, override))


if __name__ == "__main__":
    main()
