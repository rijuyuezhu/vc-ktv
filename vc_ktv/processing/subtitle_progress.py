"""Per-song subtitle stage timing and best-effort progress publication."""

import json
import logging
import sqlite3
import time

log = logging.getLogger(__name__)

STAGES = {
    "读取原视频并提取音频": "extract",
    "Kim 分离用于推理的人声": "kim",
    "双遍 Qwen3-ASR 定位与 TIFA 安全分块对齐": "align",
    "整首 TIFA 对齐完整歌词": "align",
    "整首 TIFA 对齐完整歌词（不分块、不估算）": "align",
    "生成 ASS、LRC 与时间轴记录": "subtitles",
    "将字幕烧录到保留原始音轨的 MP4": "render",
}


class SubtitleProgress:
    def __init__(self, store, song_id: int, stages: dict, started: float):
        self.store = store
        self.song_id = song_id
        self.stages = stages
        self.current_stage: str | None = None
        self.stage_started = started

    def __call__(self, message: str) -> None:
        stage = STAGES.get(message)
        if stage is not None and stage != self.current_stage:
            self._set_stage(stage, persist=True)

    def start_provider_render(self) -> None:
        """Provider timelines start at rendering; caller publishes status normally."""
        self._set_stage("render", persist=False)

    def _set_stage(self, stage: str, *, persist: bool) -> None:
        now = time.monotonic()
        if self.current_stage is not None:
            self.stages[self.current_stage] = round(now - self.stage_started, 2)
        self.current_stage, self.stage_started = stage, now
        if not persist:
            return
        # Telemetry is optional. A failed write cannot spoil a render.
        with self.store.lock:
            try:
                self.store.db.execute(
                    "UPDATE songs SET prep_stage=?,prep_stage_at=?,prep_timing=? "
                    "WHERE id=? AND status='ready' AND prep_state='aligning' "
                    "AND prep_stage!=?",
                    (
                        stage,
                        int(time.time()),
                        json.dumps(self.stages),
                        self.song_id,
                        stage,
                    ),
                )
                self.store.db.commit()
            except sqlite3.Error:
                self.store.db.rollback()
                log.exception(
                    "Failed to save subtitle progress for song #%s", self.song_id
                )

    def finish(self) -> dict:
        if self.current_stage is not None:
            self.stages[self.current_stage] = round(
                time.monotonic() - self.stage_started, 2
            )
            self.current_stage = None
        return self.stages
