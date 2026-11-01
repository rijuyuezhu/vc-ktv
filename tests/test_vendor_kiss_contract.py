"""Post-migration vendor cleanup must preserve actual alignment contracts."""

import pytest
from pydantic import ValidationError

from ktv_app.config import Settings
from ktv_app.workflow import AlignmentOptions


def test_alignment_options_still_reject_invalid_time_window():
    assert AlignmentOptions().model_dump()["device"] == "cuda"
    assert AlignmentOptions(vocal_start=1.5, vocal_end=8.0).vocal_end == 8
    with pytest.raises(ValidationError):
        AlignmentOptions(vocal_start=8, vocal_end=1)
    with pytest.raises(ValidationError):
        AlignmentOptions(vocal_separation="none")


def test_runtime_settings_still_use_asset_paths_and_cuda(monkeypatch, tmp_path):
    source = tmp_path / "runtime" / "python"
    monkeypatch.setenv("TIFA_PYTHON", str(source))
    settings = Settings()
    assert settings.tifa_python == source
    assert settings.device == "cuda"
    assert settings.kim_source.name == "Kim"
    assert settings.cpu_threads >= 1
    with pytest.raises(ValueError, match="CUDA"):
        Settings(device="cpu")
