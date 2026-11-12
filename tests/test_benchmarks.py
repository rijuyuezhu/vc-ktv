"""Portable benchmark package must remain easy to run and validate settings."""

import json
import math
import subprocess
import sys

import pytest

from benchmarks.party import Config, validate_config


@pytest.mark.parametrize(
    "config",
    [
        Config(users=0),
        Config(steps=-1),
        Config(check_every=0),
        Config(duration_min=120, duration_max=30),
        Config(p_order=-1),
        Config(
            p_cancel=0,
            p_order=0,
            p_join=0,
            p_lyrics=0,
            p_skip_lyrics=0,
            p_edit_lyrics=0,
            p_worker_complete=0,
            p_playback=0,
            p_pin=0,
            p_replay=0,
            p_switch_policy=0,
            p_toggle_orders=0,
        ),
        Config(p_bad_timing=2),
        Config(p_cancel=math.inf),
        Config(p_order=math.nan),
        Config(p_netease=math.nan),
        Config(p_burst=-0.1),
    ],
)
def test_invalid_probability_parameters_are_rejected(config):
    with pytest.raises(ValueError):
        validate_config(config)


def test_valid_benchmark_config():
    validate_config(Config(seed=1, users=2, steps=0, p_netease=0.5))


def test_probabilistic_module_cli_creates_reproducible_summary(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks.party",
            "--seed",
            "23",
            "--users",
            "6",
            "--steps",
            "25",
            "--max-orders",
            "30",
            "--p-toggle-orders",
            "0",
            "--output",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=35,
        check=True,
    )
    assert "SUMMARY" in proc.stdout
    summary = json.loads((tmp_path / "seed-23.json").read_text())
    assert summary["seed"] == 23
    assert summary["config"]["steps"] == 25
    assert summary["fair_reference_checks"] >= 2


def test_browser_benchmark_help_does_not_need_optional_playwright():
    proc = subprocess.run(
        [sys.executable, "-m", "benchmarks.lyric_ui", "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "--offsets" in proc.stdout
    assert "--output" in proc.stdout
