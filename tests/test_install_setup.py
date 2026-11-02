"""Offline installation contracts: no production config, network or model downloads."""

import os
import subprocess

from fastapi.testclient import TestClient

from scripts import check_install
from scripts.setup_spark_runtimes import tifa_python
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
