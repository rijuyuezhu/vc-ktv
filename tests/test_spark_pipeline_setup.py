"""Lightweight CPU-only checks for upstream source pins and ARM64 configuration."""

import json
import sys
from pathlib import Path

from ktv_app import model_runtime
from scripts.setup_spark_assets import ASSETS, selected
from vc_ktv.upstream_pipeline import spark_settings


def test_upstream_pin_and_manifest_consistent():
    root = Path(__file__).resolve().parents[1]
    pin = json.loads(
        (root / "third_party/autoassrenderer/UPSTREAM.json").read_text(encoding="utf-8")
    )
    assert pin["repository"] == "https://github.com/Volta233/AutoASSrenderer4karaOK"
    assert len(pin["commit"]) == 40
    assert all(c in "0123456789abcdef" for c in pin["commit"])
    assert (root / pin["vendor_package"]).is_dir()
    assert root / pin["model_assets_lock"]
    assert (
        len(list(selected("all"))) == len(ASSETS) - 4
    )  # excludes upstream TIFA and Win32 FFmpeg
    assert [x["name"] for x in selected("kim")] == [
        "kim-source.zip",
        "Kim-MelBandRoformer.ckpt",
    ]
    assert len(list(selected("qwen"))) >= 5


def test_spark_settings_use_one_permanent_pipeline_root(tmp_path):
    root = tmp_path / "pipeline"
    settings = spark_settings(root)
    assert settings.tifa_python == root / "runtime/tifa/bin/python"
    assert settings.tifa_source == root / "models/forced_alignment/sources/TIFA"
    assert (
        settings.tifa_model
        == root / "models/forced_alignment/tifa/TIFA-1.0-ST/model.pt"
    )
    assert settings.kim_model == root / "models/separation/Kim/MelBandRoformer.ckpt"
    assert settings.locator_python == root / "runtime/qwen/bin/python"


def test_cuda_probe_keeps_venv_symlink_for_python(monkeypatch, tmp_path):
    """Resolving uv venv/bin/python would silently bypass its site-packages."""
    bin_dir = tmp_path / "venv/bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.symlink_to(sys.executable)
    captured = []

    class FakeProcess:
        returncode = 0

        def __init__(self, argv, **kwargs):
            captured.append(argv[0])
            kwargs["stdout"].write(
                b'LYRIFRAME_GPU={"stage":"cuda","cuda":true,"bf16":false}\n'
            )
            kwargs["stdout"].flush()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(model_runtime.subprocess, "Popen", FakeProcess)
    assert model_runtime.probe_device(python) == {
        "device": "cuda",
        "precision": "fp16",
    }
    assert captured == [str(python)]
