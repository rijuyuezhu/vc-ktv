"""Single-call adapter from VC KTV media/lyrics to the vendored upstream engine.

This module deliberately contains no own alignment, timing or subtitle algorithm.
Heavy-model setup is performed once outside the request path; AutoPrep owns
the event lifecycle and final media commit.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ktv_app.aligners.reliable_blocks import align_reliable_blocks
from ktv_app.completion import complete_timeline, remove_estimates
from ktv_app.config import Settings
from ktv_app.lyrics import preprocess
from ktv_app.media import extract, probe
from ktv_app.rendering import RenderOptions, render
from ktv_app.separation import prepare_alignment_audio
from ktv_app.subtitles import write_bundle
from ktv_app.timeline import Style


def spark_settings(root: Path | None = None) -> Settings:
    """Model assets and dedicated runtimes live in VCKTV's permanent data root."""
    root = Path(
        root or os.getenv("VC_KTV_PIPELINE_ROOT", "~/.local/share/vc-ktv/pipeline")
    ).expanduser()
    models = root / "models"
    runtime = root / "runtime"
    return Settings(
        tifa_source=models / "forced_alignment/sources/TIFA",
        tifa_python=runtime / "tifa/bin/python",
        tifa_model=models / "forced_alignment/tifa/TIFA-1.0-ST/model.pt",
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
    """Run the selected alignment mode, preserving current subtitles and MP4 output.

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
    if mode not in ("global-kim", "global", "quality", "fast"):
        raise ValueError("mode must be global-kim, global, quality or fast")
    style = style or Style(font="Noto Sans CJK SC", mode="double", auto_complete=True)
    render_options = render_options or RenderOptions()
    emit = progress or (lambda message: None)
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
        recovery = []

        def record_failure(stage_name, error):
            recovery.append(
                {"stage": stage_name, "error": f"{type(error).__name__}: {error}"[:350]}
            )

        def rough(reason):
            emit("精确对齐不可用，使用估计时间轴保留全部歌词")
            from .rough_timing import approximate_timeline

            fallback = approximate_timeline(
                confirmed_lyrics,
                info["duration"],
                audio=work_dir / "audio.wav",
                reason=str(reason),
            )
            fallback.diagnostics["fallback_attempts"] = list(recovery)
            fallback.origin = info["audio_offset"]
            return fallback

        try:
            info = extract(source, work_dir, settings)
        except Exception as error:
            # Valid source video can often still be captioned even if audio
            # extraction or waveform generation is unavailable.
            record_failure("extract", error)
            info = probe(source, settings)
        (work_dir / "confirmed_lyrics.txt").write_text(
            confirmed_lyrics, encoding="utf-8"
        )
        try:
            lines = preprocess(confirmed_lyrics, confirmed=True)
        except Exception as error:
            record_failure("preprocess", error)
            lines = None
        if recovery:
            timeline = rough(recovery[-1]["error"])
        elif mode in ("global", "global-kim"):
            from .global_alignment import (
                UNKNOWN_LATIN,
                align_full_song,
                pronunciation_lyrics,
            )
            from .rough_timing import approximate_timeline

            align_audio = source
            # Known scat syllables use precise mappings; pre-convert only
            # remaining Latin vocals before attempting any full-song TIFA.
            normalized, _, _ = pronunciation_lyrics(confirmed_lyrics)
            generic_pronunciation = bool(UNKNOWN_LATIN.search(normalized))
            align_options = {"unknown_vocables": True} if generic_pronunciation else {}
            failures = []
            timeline_audio = {"source": "original_audio"}
            if mode == "global-kim":
                stage("kim", "Kim 分离用于推理的人声")
                try:
                    align_audio, timeline_audio = prepare_alignment_audio(
                        source,
                        work_dir / "audio.wav",
                        info,
                        work_dir,
                        "kim",
                        settings,
                        lambda message: emit(
                            message.replace("开始 ASR", "开始整首 TIFA").replace(
                                "进入歌词识别", "进入整首 TIFA"
                            )
                        ),
                    )
                except Exception as error:
                    failures.append({"stage": "kim", "error": str(error)[:350]})
                    emit("人声分离不可用，尝试直接对原音频做整首歌词对齐")
            if generic_pronunciation:
                emit("检测到未收录的拉丁字母唱词，已预处理为近似中文读音")
            stage("align", "整首 TIFA 对齐完整歌词")
            try:
                timeline = align_full_song(
                    align_audio,
                    confirmed_lyrics,
                    info["duration"],
                    info["audio_offset"],
                    work_dir / "alignment",
                    settings,
                    **align_options,
                )
            except Exception as error:
                failures.append({"stage": "tifa", "error": str(error)[:350]})
                timeline = None
                if Path(align_audio) != source:
                    # If the isolated vocals are difficult for the model,
                    # preserve whole-song acoustic alignment on original mix.
                    emit("人声版 TIFA 无法完成，改用原始混合音频重试全曲对齐")
                    try:
                        timeline = align_full_song(
                            source,
                            confirmed_lyrics,
                            info["duration"],
                            info["audio_offset"],
                            work_dir / "alignment-mix",
                            settings,
                            **align_options,
                        )
                        timeline_audio = {"source": "original_mix_fallback"}
                    except Exception as retry_error:
                        failures.append(
                            {
                                "stage": "tifa-mix",
                                "error": str(retry_error)[:350],
                            }
                        )
                if timeline is None:
                    emit("整首 TIFA 对齐路径均不可用，最终以粗时间轴保留全部歌词")
                    timeline = approximate_timeline(
                        confirmed_lyrics,
                        info["duration"],
                        audio=align_audio
                        if Path(align_audio).suffix.lower() == ".wav"
                        else work_dir / "audio.wav",
                        reason=failures[-1]["error"],
                    )
            if failures:
                timeline.diagnostics["fallback_attempts"] = failures
                (work_dir / "fallback_report.json").write_text(
                    json.dumps(failures, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            timeline.diagnostics["alignment_audio"] = timeline_audio
        elif mode == "fast":
            from ktv_app.aligners.fast import align_fast

            stage("align", "Fast Mode：原音频识别、粗定位与分段 TIFA")
            try:
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
            except Exception as error:
                record_failure("fast-align", error)
                timeline = rough(error)
        else:
            stage("kim", "Kim 分离用于推理的人声")
            try:
                audio, evidence = prepare_alignment_audio(
                    source,
                    work_dir / "audio.wav",
                    info,
                    work_dir,
                    "kim",
                    settings,
                    emit,
                )
            except Exception as error:
                record_failure("kim", error)
                audio = work_dir / "audio.wav"
                evidence = {"source": "original_mix_fallback"}
                emit("Kim 人声分离失败，改用原音轨尝试 ASR/TIFA")
            stage("align", "双遍 Qwen3-ASR 定位与 TIFA 安全分块对齐")
            try:
                timeline = align_reliable_blocks(
                    audio,
                    lines,
                    info["duration"],
                    info["audio_offset"],
                    work_dir / "alignment",
                    settings,
                    emit,
                    max_input_seconds=None,  # Source-backed cut points; no hard duration cutoff.
                )
                timeline.diagnostics["alignment_audio"] = evidence
            except Exception as error:
                record_failure("asr-tifa", error)
                timeline = rough(error)
                timeline.diagnostics["alignment_audio"] = evidence
            if (
                recovery
                and timeline.diagnostics.get("pipeline") != "last_resort_rough_timing"
            ):
                timeline.diagnostics["fallback_attempts"] = list(recovery)
        timeline.origin = info["audio_offset"]
        # Preserve PR62 rough timestamps, never remove them as "old estimates".
        if timeline.diagnostics.get("pipeline") == "last_resort_rough_timing":
            style = style.model_copy(update={"auto_complete": True})
        elif mode not in ("global", "global-kim"):
            try:
                timeline = complete_timeline(remove_estimates(timeline), rolling=True)
            except Exception as error:
                record_failure("completion", error)
                timeline = rough(error)
                style = style.model_copy(update={"auto_complete": True})
        else:
            # Full-song mode never runs rolling completion. Display original
            # model timings, with any local best-effort label repair recorded.
            style = style.model_copy(
                update={
                    "auto_complete": bool(
                        timeline.diagnostics.get("best_effort")
                        or timeline.diagnostics.get("pipeline")
                        == "last_resort_rough_timing"
                    )
                }
            )
        stage("subtitles", "生成 ASS、LRC 与时间轴记录")
        bundle = work_dir / "subtitles"
        try:
            write_bundle(timeline, style, bundle)
        except Exception as error:
            if timeline.diagnostics.get("pipeline") == "last_resort_rough_timing":
                raise  # Even plain captions could not be written.
            record_failure("subtitles", error)
            timeline = rough(error)
            style = style.model_copy(update={"auto_complete": True})
            write_bundle(timeline, style, bundle)
        stage("render", "将字幕烧录到保留原始音轨的 MP4")
        # Rendering has no fixed wall-clock cap; on an actual encoder failure,
        # retry once with a lower-resource preset.
        try:
            final = render(
                source, bundle, info, settings, render_options, "karaoke.mp4"
            )
        except Exception as error:
            record_failure("render", error)
            emit("字幕烧录失败，改用较低资源的编码参数再试一次")
            safe_render = RenderOptions(preset="ultrafast", crf=28, compact_video=False)
            final = render(source, bundle, info, settings, safe_render, "karaoke.mp4")
            # The subtitle export precedes rendering; also record any retry.
            report_path = bundle / "export_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["fallback_attempts"] = list(recovery)
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
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
        if recovery:
            (work_dir / "fallback_report.json").write_text(
                json.dumps(recovery, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        status.update(
            status="done",
            stage="done",
            alignment_quality=(
                "rough"
                if timeline.diagnostics.get("pipeline") == "last_resort_rough_timing"
                else "repaired"
                if timeline.diagnostics.get("best_effort")
                else "model"
            ),
            fallback_attempts=recovery,
            outputs=result.as_dict(),
        )
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
