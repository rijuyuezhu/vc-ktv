"""Quality-preserving optimizations: one TIFA worker, conservative ASR filtering."""

import json
from pathlib import Path

from ktv_app.aligners.asr_timing import (
    time_asr_chunks,
    unanchorable_asr_window,
)
from ktv_app.timeline import Line, Timeline, Unit


def test_chinese_irrelevant_chunks_are_skipped_only_when_no_lyric_pair():
    lines = [Line(id="0", text="林中风雨飘摇", language="zh")]
    assert unanchorable_asr_window({"language": "Chinese", "text": "嗯。"}, lines)
    assert unanchorable_asr_window({"language": "Chinese", "text": "另外一首歌"}, lines)
    assert not unanchorable_asr_window(
        {"language": "Chinese", "text": "林中风雨"}, lines
    )
    assert not unanchorable_asr_window(
        {"language": "Chinese", "text": "嗯。"},
        [Line(id="0", text="嗯", language="zh")],
    )
    assert not unanchorable_asr_window({"language": "Japanese", "text": "嗯。"}, lines)
    assert not unanchorable_asr_window(
        {"language": "Chinese", "text": "嗯。"},
        [Line(id="0", text="まるで", language="ja")],
    )
    assert not unanchorable_asr_window({"language": "Chinese", "text": "嗯。"}, None)


def test_chinese_gating_never_turns_ignored_asr_into_fake_timestamps(
    tmp_path, monkeypatch
):
    from ktv_app.aligners import asr_timing

    monkeypatch.setattr(asr_timing, "crop", lambda *args: None)
    submitted = []

    def infer(self, jobs, duration, work):
        submitted.extend(jobs)
        output = {}
        for job in jobs:
            output[job["id"]] = Timeline(
                duration=40,
                origin=job["origin"],
                lines=[
                    Line(
                        id=f"asr-{job['id']}",
                        text="林中风雨",
                        units=[
                            Unit(
                                id=f"u-{job['id']}",
                                text="林中风雨",
                                source_start=0,
                                source_end=4,
                                start=job["origin"] + 1,
                                end=job["origin"] + 2,
                            )
                        ],
                    )
                ],
            )
        return output

    monkeypatch.setattr(asr_timing.TIFAAdapter, "align_many", infer)
    chunks = [
        {
            "start": i * 9.0,
            "end": i * 9.0 + 9,
            "core_start": i * 9.0,
            "core_end": i * 9.0 + 9,
            "text": text,
            "language": "Chinese",
            "pass_index": pass_index,
        }
        for i, (text, pass_index) in enumerate(
            [
                ("林中风雨", 0),
                ("嗯。", 0),
                ("林中风雨", 1),
                ("噢。", 1),
            ]
        )
    ]
    result = time_asr_chunks(
        "audio.wav",
        dict(chunks=chunks, duration=40, asr_elapsed=1),
        object(),
        tmp_path,
        [Line(id="0", text="林中风雨飘摇", language="zh")],
    )
    assert [job["id"] for job in submitted] == ["0", "2"]
    assert result["tifa_skipped_windows"] == [1, 3]
    assert result["tokens"] and result["verification_tokens"]
    assert all(chunk.get("timing_skipped") for chunk in (chunks[1], chunks[3]))
    assert all(token["text"] == "林中风雨" for token in result["tokens"])
    submitted.clear()
    # The complete fallback reuses the Qwen text but fills every TIFA
    # timing window instead of retaining the selective "skipped" marker.
    full = time_asr_chunks(
        "audio.wav",
        dict(chunks=chunks, duration=40, asr_elapsed=1),
        object(),
        tmp_path / "full",
        None,
    )
    assert [job["id"] for job in submitted] == ["0", "1", "2", "3"]
    assert full["tifa_skipped_windows"] == []
    assert all("timing_skipped" not in chunk for chunk in chunks)


def test_session_uses_original_order_and_propagates_to_block_worker(
    tmp_path, monkeypatch
):
    from ktv_app.aligners import block_execution

    monkeypatch.setattr(block_execution, "crop", lambda *args: None)
    captured = []

    def fake_align(audio, lines, duration, origin, work, settings, event, **kw):
        captured.append(kw.get("tifa_session"))
        for line in lines:
            line.units = [
                Unit(
                    id=line.id + "-u",
                    text=line.text,
                    source_start=0,
                    source_end=len(line.text),
                    start=origin + 1,
                    end=origin + 2,
                )
            ]
        return Timeline(
            duration=duration,
            origin=origin,
            lines=lines,
            diagnostics={"unit_sources": {l.units[0].id: "tifa_global" for l in lines}},
        )

    monkeypatch.setattr(block_execution, "run_anchor_alignment", fake_align)
    lines = [Line(id=str(i), text="测试") for i in range(2)]
    token = object()
    blocks = [
        dict(
            id=str(i),
            indices=[i],
            line_ids=[str(i)],
            core_start=2 + i * 10,
            core_end=12 + i * 10,
        )
        for i in range(2)
    ]
    entries = block_execution.run_blocks(
        tmp_path / "audio.wav",
        lines,
        blocks,
        {},
        {},
        35,
        0,
        35,
        tmp_path,
        object(),
        lambda msg: None,
        tifa_session=token,
    )
    assert len(entries) == 2
    assert captured == [token, token]


def test_tifa_session_stream_is_one_process_not_one_per_job(tmp_path, monkeypatch):

    from ktv_app.aligners import tifa

    class FakeProcess:
        starts = 0

        def __init__(self, *args, **kwargs):
            FakeProcess.starts += 1
            self.stdin = self
            self.closed = False

        def poll(self):
            return None

        def write(self, data):
            if '"request"' in data:
                req = Path(json.loads(data)["request"])
                work = Path(json.loads(req.read_text())["work"])
                (work / "stream_done.json").write_text('{"returncode":0}')

        def flush(self):
            pass

        def close(self):
            self.closed = True

        def wait(self, timeout=None):
            return 0

    class FakeAdapter:
        class Settings:
            tifa_python = "/opt/tifa/python"

        settings = Settings()

        def prepare(self, audio, lines, work):
            work.mkdir(parents=True)
            p = work / "request.json"
            p.write_text(
                json.dumps(
                    {
                        "work": str(work),
                        "source": "/opt/tifa",
                        "model": "/opt/model",
                        "g2p": "/opt/model/g2p",
                    }
                )
            )
            return p, {}

        def collect(self, lines, duration, origin, work, code, log):
            return (code, str(work))

    monkeypatch.setattr(tifa.subprocess, "Popen", FakeProcess)
    with tifa.TIFASession(FakeAdapter(), tmp_path / "shared") as session:
        for i in range(3):
            path = tmp_path / f"attempt-{i}"
            assert session.align("audio.wav", [], 2, 0, path) == (0, str(path))
    assert FakeProcess.starts == 1
