"""Offline installation contracts: no production config, network or model downloads."""

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from scripts import check_install, setup_spark_assets
from scripts.setup_spark_runtimes import require_cuda, setup, tifa_python
from vc_ktv.app import create_app


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
