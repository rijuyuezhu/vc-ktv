"""Single-call adapter from VC KTV media/lyrics to the vendored upstream engine.

This module deliberately contains no own alignment, timing or subtitle algorithm.
Heavy-model setup is performed once outside the request path; AutoPrep owns
the event lifecycle and final media commit.
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ktv_app.aligners.reliable_blocks import align_reliable_blocks
from ktv_app.completion import complete_timeline, remove_estimates
from ktv_app.config import Settings
from ktv_app.lyrics import preprocess
from ktv_app.media import extract
from ktv_app.rendering import RenderOptions, render
from ktv_app.separation import prepare_alignment_audio
from ktv_app.subtitles import write_bundle
from ktv_app.timeline import Style


def spark_settings(root: Path | None = None, tifa_root: Path | None = None) -> Settings:
    """One set of model paths for CLI smoke runs and the AutoPrep worker."""
    root = Path(
        root
        or os.getenv("VC_KTV_PIPELINE_ROOT", "~/.local/share/vc-ktv-pipeline-staging")
    ).expanduser()
    tifa_root = Path(
        tifa_root or os.getenv("VC_KTV_TIFA_ROOT", Path(__file__).resolve().parents[1])
    ).expanduser()
    models = root / "models"
    runtime = root / "runtime"
    return Settings(
        tifa_source=tifa_root / ".runtime/TIFA",
        tifa_python=tifa_root / ".runtime/tifa-env/bin/python",
        tifa_model=tifa_root / ".runtime/models/TIFA-1.0-ST/model.pt",
        kim_source=models / "separation/sources/Kim",
        kim_model=models / "separation/Kim/MelBandRoformer.ckpt",
        separation_python=runtime / "kim/bin/python",
        asr_model=models / "Qwen3-ASR-1.7B",
        locator_python=runtime / "qwen/bin/python",
    )


@dataclass(frozen=True)
class PipelineResult:
    video: Path
    ass: Path
    timeline: Path
    report: Path
    run_metadata: Path

    def as_dict(self) -> dict:
        return {key: str(value) for key, value in vars(self).items()}


def make_karaoke_video(
    source: Path,
    confirmed_lyrics: str,
    work_dir: Path,
    settings: Settings,
    *,
    progress: Callable[[str], None] | None = None,
    style: Style | None = None,
    render_options: RenderOptions | None = None,
    mode: str = "quality",
) -> PipelineResult:
    """Use the full upstream Kim → Qwen ASR → bounded TIFA → ASS/MP4 pipeline.

    The work directory MUST be new and local to this song/attempt. In particular,
    no live event DB state is touched; AutoPrep owns the final
    media commit and event lifecycle.
    """
    source = Path(source).resolve(strict=True)
    if not source.is_file() or not confirmed_lyrics.strip():
        raise ValueError("请输入存在的视频文件和已确认的歌词")
    work_dir = Path(work_dir).resolve()
    if (
        work_dir == source.parent
        or work_dir == source
        or source.is_relative_to(work_dir)
    ):
        raise ValueError("制作目录不能覆盖原视频所在路径")
    work_dir.mkdir(parents=True, exist_ok=False)
    if mode not in ("quality", "fast"):
        raise ValueError("mode must be quality or fast")
    style = style or Style(font="Noto Sans CJK SC", mode="double", auto_complete=True)
    render_options = render_options or RenderOptions()
    emit = progress or (lambda message: None)
    started = time.monotonic()
    status = {"status": "running", "stage": "extract"}
    metadata_path = work_dir / "run.json"

    def stage(name: str, message: str):
        status["stage"] = name
        metadata_path.write_text(
            json.dumps(status, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        emit(message)

    try:
        stage("extract", "读取原视频并提取音频")
        info = extract(source, work_dir, settings)
        lines = preprocess(confirmed_lyrics)
        (work_dir / "confirmed_lyrics.txt").write_text(
            confirmed_lyrics, encoding="utf-8"
        )
        if mode == "fast":
            from ktv_app.aligners.fast import align_fast

            stage("align", "Fast Mode：原音频识别、粗定位与分段 TIFA")
            timeline = align_fast(
                work_dir / "audio.wav",
                lines,
                info["duration"],
                info["audio_offset"],
                work_dir / "alignment",
                settings,
                emit,
            )
            timeline.diagnostics["alignment_audio"] = {"source": "original_audio"}
        else:
            stage("kim", "Kim 分离用于推理的人声")
            audio, evidence = prepare_alignment_audio(
                source, work_dir / "audio.wav", info, work_dir, "kim", settings, emit
            )
            stage("align", "双遍 Qwen3-ASR 定位与 TIFA 安全分块对齐")
            timeline = align_reliable_blocks(
                audio,
                lines,
                info["duration"],
                info["audio_offset"],
                work_dir / "alignment",
                settings,
                emit,
                max_input_seconds=90,
            )
            timeline.diagnostics["alignment_audio"] = evidence
        timeline.origin = info["audio_offset"]
        # Upstream's formal chain discards any old generated estimates before
        # bounded completion and records new estimated provenance explicitly.
        timeline = complete_timeline(remove_estimates(timeline), rolling=True)
        stage("subtitles", "生成 ASS、LRC 与时间轴记录")
        bundle = work_dir / "subtitles"
        write_bundle(timeline, style, bundle)
        stage("render", "将字幕烧录到保留原始音轨的 MP4")
        if mode == "fast" and time.monotonic() - started >= 179:
            raise TimeoutError("Fast Mode 已耗尽三分钟预算，停止渲染")
        final = render(
            source,
            bundle,
            info,
            settings,
            render_options,
            "karaoke.mp4",
            **(
                {"timeout_s": max(1, 180 - (time.monotonic() - started))}
                if mode == "fast"
                else {}
            ),
        )
        if not final.is_file() or final.stat().st_size == 0:
            raise RuntimeError("字幕视频未生成")
        result = PipelineResult(
            video=final,
            ass=bundle / "lyrics.ass",
            timeline=bundle / "timeline.json",
            report=bundle / "export_report.json",
            run_metadata=metadata_path,
        )
        status.update(status="done", stage="done", outputs=result.as_dict())
        emit("字幕视频制作完成")
        return result
    except Exception as exc:
        status.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:600])
        raise
    finally:
        metadata_path.write_text(
            json.dumps(status, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
