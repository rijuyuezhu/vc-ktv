"""The optional TIFA installer must never mutate existing production runtimes."""

import json
import subprocess
import sys

import pytest

from scripts import setup_tifa_runtime as tifa


def source_tree(root):
    source = root / "models/forced_alignment/sources/TIFA"
    source.mkdir(parents=True)
    (source / "infer.py").write_text("# test source\n")
    return source


def test_tifa_dependency_snapshot_is_fully_pinned_without_torch():
    packages = [
        line.strip()
        for line in tifa.REQUIREMENTS.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert len(packages) == 79
    names = [line.split("==")[0] for line in packages]
    assert all(line.count("==") == 1 for line in packages)
    assert len(names) == len(set(names))
    assert not {"torch", "torchaudio"} & set(names)
    assert {"g2pflow", "requests", "locket", "cffi", "mpmath"} <= set(names)
    assert "mpmath==1.3.0" in packages


def test_installer_reports_actual_package_error(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(
            2, ["uv", "pip"], stderr="required CUDA dependency unavailable"
        )

    monkeypatch.setattr(tifa.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="required CUDA dependency unavailable"):
        tifa.run(["uv", "pip", "install"])


def test_existing_tifa_runtime_is_not_touched(tmp_path, monkeypatch):
    source_tree(tmp_path)
    target = tmp_path / "runtime/tifa"
    target.mkdir(parents=True)
    sentinel = target / "sentinel"
    sentinel.write_text("keep")

    def forbidden(*args, **kwargs):
        raise AssertionError("Do not inspect or reinstall a running TIFA venv")

    monkeypatch.setattr(tifa, "inspect_source", forbidden)
    with pytest.raises(FileExistsError, match="Refusing to modify"):
        tifa.setup(tmp_path, tmp_path / "cuda-python")
    assert sentinel.read_text() == "keep"


def test_missing_assets_or_cuda_source_never_create_runtime(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError, match="run setup_spark_assets"):
        tifa.setup(tmp_path, tmp_path / "missing-python")
    source_tree(tmp_path)
    monkeypatch.setattr(tifa.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tifa.platform, "machine", lambda: "aarch64")
    with pytest.raises(FileNotFoundError, match="CUDA torch Python not found"):
        tifa.setup(tmp_path, tmp_path / "missing-python")
    assert not (tmp_path / "runtime/tifa").exists()


def test_cuda_python_venv_symlink_is_not_resolved(tmp_path, monkeypatch):
    source_tree(tmp_path)
    python = tmp_path / "cuda-venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    monkeypatch.setattr(tifa.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tifa.platform, "machine", lambda: "aarch64")

    def verify(path):
        assert path == python  # Following the symlink loses pyvenv.cfg.
        raise RuntimeError("source path verified")

    monkeypatch.setattr(tifa, "inspect_source", verify)
    with pytest.raises(RuntimeError, match="source path verified"):
        tifa.setup(tmp_path, python)
    assert not (tmp_path / "runtime/tifa").exists()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"device": False}, "directly contain working"),
        ({"direct": False}, "indirect .pth"),
        ({"torch": "2.13.0"}, "CUDA source must"),
        ({"python": [3, 11]}, "Python 3.12"),
    ],
)
def test_source_cuda_requires_pinned_direct_wheels(
    tmp_path, monkeypatch, override, message
):
    python = tmp_path / "python"
    python.touch()
    record = {
        "python": [3, 12],
        "site": str(tmp_path / "cuda-site"),
        "torch": tifa.TORCH_VERSION,
        "audio": tifa.AUDIO_VERSION,
        "cuda": "13.0",
        "device": True,
        "direct": True,
    }
    record.update(override)
    monkeypatch.setattr(tifa, "run", lambda *args, **kw: json.dumps(record))
    with pytest.raises(RuntimeError, match=message):
        tifa.inspect_source(python)


def test_failed_fresh_install_rolls_back_only_new_runtime(tmp_path, monkeypatch):
    source_tree(tmp_path)
    torch_site = tmp_path / "direct-cuda-torch"
    torch_site.mkdir()
    monkeypatch.setattr(tifa.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tifa.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(tifa, "inspect_source", lambda _: torch_site)

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["uv", "venv"]:
            target = tmp_path / "runtime/tifa"
            target.mkdir(parents=True)
            (target / "partial").touch()
            return ""
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(tifa, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        tifa.setup(tmp_path, tmp_path / "cuda-python")
    assert calls[0][:2] == ["uv", "venv"]
    assert calls[1][:3] == ["uv", "pip", "install"]
    assert "--no-deps" in calls[1]
    assert not (tmp_path / "runtime/tifa").exists()
    assert torch_site.is_dir()
