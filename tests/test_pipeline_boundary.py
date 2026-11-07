"""Public bridge to vendored alignment code and machine-neutral config."""

from pathlib import Path

from ktv_app.timeline import Timeline
from vc_ktv.infra.config import pipeline_settings
from vc_ktv.pipeline import Timeline as PublicTimeline
from vc_ktv.processing.upstream_pipeline import spark_settings


def test_pipeline_settings_preserves_installed_layout(tmp_path, monkeypatch):
    root = tmp_path / "pipeline"
    expected = root / "models"
    installed = pipeline_settings(root)
    assert installed.tifa_source == expected / "forced_alignment/sources/TIFA"
    assert installed.asr_model == expected / "Qwen3-ASR-1.7B"
    assert installed.kim_model == expected / "separation/Kim/MelBandRoformer.ckpt"
    assert installed.tifa_python == root / "runtime/tifa/bin/python"
    assert installed.device == "cuda"
    monkeypatch.setenv("VC_KTV_PIPELINE_ROOT", str(root))
    assert pipeline_settings().tifa_model == installed.tifa_model
    assert (
        spark_settings(root).model_dump() == installed.model_dump()
        if hasattr(installed, "model_dump")
        else spark_settings(root).tifa_model == installed.tifa_model
    )


def test_vendor_timeline_identity_is_preserved():
    assert PublicTimeline is Timeline


def test_vendor_imports_only_through_pipeline_adapter():
    # Guard the architectural boundary without rewriting the vendored engine.
    root = Path(__file__).resolve().parents[1] / "vc_ktv"
    allowed = {
        "pipeline/__init__.py",
        "infra/config.py",
        "processing/upstream_pipeline.py",
    }
    for path in root.rglob("*.py"):
        name = path.relative_to(root).as_posix()
        if name in allowed:
            continue
        assert "from ktv_app." not in path.read_text(encoding="utf-8"), name
