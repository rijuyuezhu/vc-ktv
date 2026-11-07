"""Offline installation contracts: no production config, network or model downloads."""

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from scripts import check_install, setup_spark_assets
from scripts.setup_spark_runtimes import main as runtime_main
from scripts.setup_spark_runtimes import require_cuda, setup, tifa_python
from vc_ktv.application.factory import create_app


def test_setup_initializes_isolated_config_and_base_app(tmp_path, monkeypatch):
    config = tmp_path / "config"
    data = tmp_path / "data"
    subprocess.run(
        ["bash", "scripts/setup.sh"],
        check=True,
        env={**os.environ, "VC_KTV_CONFIG": str(config)},
        capture_output=True,
    )
    assert (config / "admin-password").stat().st_mode & 0o077 == 0
    assert (config / "token-secret").stat().st_mode & 0o077 == 0
    monkeypatch.setenv("VC_KTV_CONFIG", str(config))
    monkeypatch.setenv("VC_KTV_DATA", str(data))
    monkeypatch.setenv("VC_KTV_AUTOPREPARE", "0")
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["ok"] is True
        assert client.get("/").status_code == 200
        assert client.get("/admin/").status_code == 200


def test_doctor_distinguishes_base_from_gpu(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "admin-password").write_text("test-only")
    (config / "token-secret").write_text("test-only")
    monkeypatch.setattr(check_install.shutil, "which", lambda _: "/fake/bin")
    monkeypatch.setattr(
        check_install,
        "probe",
        lambda cmd: (
            " .. subtitles "
            if cmd[-1] == "-filters"
            else "libx264"
            if cmd[-1] == "-encoders"
            else "Noto Sans CJK SC"
        ),
    )
    assert all(ok for ok, _ in check_install.checks("base", config, tmp_path))
    assert not all(ok for ok, _ in check_install.checks("gpu", config, tmp_path))


def test_runtime_root_respects_cli_override(tmp_path):
    assert tifa_python(tmp_path) == tmp_path / "runtime/tifa/bin/python"
    custom = tmp_path / "other-python"
    assert tifa_python(tmp_path, custom) == custom


def test_cuda_runtime_must_be_available_before_creating_overlay(tmp_path, monkeypatch):
    shared = tmp_path / "tifa-python"
    shared.touch()
    monkeypatch.setattr(
        "scripts.setup_spark_runtimes.python_output", lambda *_: "False"
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("No overlay should be created for missing CUDA")

    monkeypatch.setattr("scripts.setup_spark_runtimes.subprocess.run", forbidden)
    with pytest.raises(RuntimeError, match="CUDA torch unavailable"):
        setup("kim", tmp_path, shared)
    assert not (tmp_path / "runtime/kim").exists()


def test_cuda_runtime_probe_requires_true(monkeypatch, tmp_path):
    monkeypatch.setattr("scripts.setup_spark_runtimes.python_output", lambda *_: "True")
    require_cuda(tmp_path / "python")


def test_asset_download_group_uses_pipeline_root_env(monkeypatch, tmp_path):
    targets = []
    monkeypatch.setenv("VC_KTV_PIPELINE_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["setup_spark_assets", "kim"])
    monkeypatch.setattr(
        setup_spark_assets,
        "download",
        lambda item, root: targets.append(root / item["target"]) or "cached",
    )
    setup_spark_assets.main()
    assert len(targets) == 2
    assert all(tmp_path in target.parents for target in targets)


def test_runtime_default_root_uses_pipeline_root_env(tmp_path):
    result = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "from scripts.setup_spark_runtimes import PIPELINE_ROOT; print(PIPELINE_ROOT)",
        ],
        env={**os.environ, "VC_KTV_PIPELINE_ROOT": str(tmp_path)},
        text=True,
    )
    assert result.strip() == str(tmp_path)


def test_runtime_cli_preserves_venv_python_symlink(tmp_path, monkeypatch):
    python = tmp_path / "tifa/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    passed = []
    monkeypatch.setattr(
        "scripts.setup_spark_runtimes.setup", lambda *args: passed.append(args)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "setup_spark_runtimes",
            "kim",
            "--root",
            str(tmp_path),
            "--tifa-python",
            str(python),
        ],
    )
    runtime_main()
    assert passed == [("kim", tmp_path, python)]


def test_qwen_pins_runtime_transformers_imports():
    from pathlib import Path

    requirements = (
        Path(__file__).resolve().parents[1]
        / "third_party/autoassrenderer/qwen-requirements.txt"
    ).read_text()
    names = {line.split("==", 1)[0] for line in requirements.splitlines()}
    assert {"regex", "safetensors"} <= names


def test_assets_restart_when_upstream_does_not_support_byte_ranges(
    tmp_path, monkeypatch
):
    import hashlib

    content = b"correct pinned payload" * 100
    item = {
        "name": "pinned.bin",
        "target": "models/pinned.bin",
        "url": "https://example.test/unsupported-ranges",
        "sha256": hashlib.sha256(content).hexdigest(),
        "kind": "file",
    }
    staged = tmp_path / "downloads/pinned.bin"
    staged.parent.mkdir()
    staged.write_bytes(content[:10])
    calls = []

    def fake_curl(cmd, *, check):
        calls.append(cmd)
        assert check is False
        output = tmp_path / "downloads/pinned.bin"
        if "--continue-at" in cmd:
            assert output.read_bytes() == content[:10]
            return subprocess.CompletedProcess(cmd, 33)
        assert not output.exists()
        output.write_bytes(content)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(setup_spark_assets.subprocess, "run", fake_curl)
    assert setup_spark_assets.download(item, tmp_path) == "downloaded"
    assert len(calls) == 2
    assert "--continue-at" in calls[0] and "--continue-at" not in calls[1]
    assert (tmp_path / item["target"]).read_bytes() == content


def test_assets_resumes_when_upstream_supports_byte_ranges(tmp_path, monkeypatch):
    import hashlib

    content = b"real model bytes" * 100
    item = {
        "name": "model.bin",
        "target": "models/model.bin",
        "url": "https://example.test/range-enabled",
        "sha256": hashlib.sha256(content).hexdigest(),
        "kind": "file",
    }
    staged = tmp_path / "downloads/model.bin"
    staged.parent.mkdir()
    staged.write_bytes(content[:37])
    calls = []

    def fake_curl(cmd, *, check):
        calls.append(cmd)
        assert "--continue-at" in cmd
        with staged.open("ab") as stream:
            stream.write(content[37:])
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(setup_spark_assets.subprocess, "run", fake_curl)
    assert setup_spark_assets.download(item, tmp_path) == "downloaded"
    assert len(calls) == 1
    assert (tmp_path / item["target"]).read_bytes() == content


def test_assets_discard_invalid_checksum_before_fallback(tmp_path, monkeypatch):
    import hashlib

    content = b"correct pinned model" * 50
    item = {
        "name": "model.bin",
        "target": "models/model.bin",
        "url": "https://huggingface.co/example/test",
        "sha256": hashlib.sha256(content).hexdigest(),
        "kind": "file",
    }
    calls = []

    def fake_curl(cmd, *, check):
        calls.append(cmd)
        out = tmp_path / "downloads/model.bin"
        assert "--continue-at" not in cmd
        out.write_bytes(b"bad model" if len(calls) == 1 else content)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(setup_spark_assets.subprocess, "run", fake_curl)
    assert setup_spark_assets.download(item, tmp_path) == "downloaded"
    assert len(calls) == 2
    assert "hf-mirror.com" in calls[0][-1]
    assert "huggingface.co" in calls[1][-1]
    assert (tmp_path / item["target"]).read_bytes() == content
