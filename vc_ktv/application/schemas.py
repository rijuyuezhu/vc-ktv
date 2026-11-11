"""HTTP request schemas shared across application routers."""

from typing import Literal

from pydantic import BaseModel, Field


class TVPair(BaseModel):
    pair: str


class GuestLyricChoice(BaseModel):
    text: str = Field(min_length=10)
    ticket: str = Field(min_length=10, max_length=256)
    source: str = Field(min_length=2, max_length=20)
    timing_mode: Literal["align", "provider"] = "align"
    timed_lrc: str = Field(default="")
    source_lrc_raw: str = Field(default="")
    timing_ticket: str = ""
    offset_ms: int = Field(default=0)
    selection_text: str = Field(default="")
    selection_ticket: str = ""
    selected_lines: list[int] | None = Field(default=None)


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    username: str | None = Field(default=None, max_length=24)


class Finish(BaseModel):
    song_id: int
    reason: str = ""


class PlaybackMode(BaseModel):
    mode: Literal["browser", "tv", "manual"]


class TVRemoteCommand(BaseModel):
    action: Literal[
        "pause",
        "resume",
        "restart",
        "skip",
        "replay_next",
        "volume",
        "offset",
        "opacity",
        "smooth",
    ]
    song_id: int | None = Field(default=None, ge=1)
    value: float | bool | None = None


class TVRemoteReport(BaseModel):
    ack_seq: int = Field(ge=0)
    active_id: int | None = None
    paused: bool = True
    position: float = Field(default=0, ge=0)
    duration: float | None = Field(default=None, ge=0)
    volume: int = Field(default=100, ge=0, le=100)
    offset: int = Field(default=0, ge=-600, le=600)
    opacity: int = Field(default=15, ge=0, le=100)
    smooth: bool = False
    started: bool = False
    overlay: bool = False
    error: str = Field(default="", max_length=150)


class Lyrics(BaseModel):
    text: str = Field()


class RenderRequest(BaseModel):
    lead: float = Field(default=1.8, ge=0, le=5)
