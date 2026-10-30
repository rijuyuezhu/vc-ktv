#!/usr/bin/env python3
"""Prepare separate Kim/Qwen runtime overlays, reusing existing read-only CUDA torch.

Does not modify the pre-existing production TIFA venv or shared CUDA torch.
"""

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE_ROOT = Path.home() / ".local/share/vc-ktv/pipeline"
TIFA_PYTHON = PIPELINE_ROOT / "runtime/tifa/bin/python"


def python_output(python, expression):
    return subprocess.check_output([str(python), "-c", expression], text=True).strip()


def setup(group, staging, shared_python):
    if group not in ("kim", "qwen"):
        raise ValueError(group)
    target = staging / "runtime" / group
    python = target / "bin/python"
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
    subprocess.run(
        [
            str(python),
            "-c",
            "import torch; print('cuda=',torch.cuda.is_available(),'torch=',torch.__version__)",
        ],
        check=True,
    )
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
    parser.add_argument("--tifa-python", type=Path, default=TIFA_PYTHON)
    args = parser.parse_args()
    for group in ("kim", "qwen") if args.group == "all" else (args.group,):
        setup(group, args.root, args.tifa_python)


if __name__ == "__main__":
    main()
