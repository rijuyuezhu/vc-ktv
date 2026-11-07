"""Temporary compatibility entry point; migrate callers to application modules."""

import httpx

from .api.event_admin import EventCreate, PlaybackSettings
from .api.guest_auth import JoinEvent, RegisterUser, UserLogin
from .application.common import (
    MAX_ACTIVE_SONGS,
    MAX_USER_SONGS,
    PinnedFileResponse,
    bilibili_lookup,
    get_bvid,
    probe_media,
    vcpedia_find,
)
from .application.factory import create_app
from .application.schemas import (
    Finish,
    GuestLyricChoice,
    Login,
    Lyrics,
    PlaybackMode,
    RenderRequest,
    TVPair,
    TVRemoteCommand,
    TVRemoteReport,
)
from .core.store import Store, time_now
from .karaoke import render_video
from .lyric_selection import search_lyric_candidates

__all__ = [
    "create_app",
    "get_bvid",
    "probe_media",
    "PinnedFileResponse",
    "bilibili_lookup",
    "vcpedia_find",
    "MAX_ACTIVE_SONGS",
    "MAX_USER_SONGS",
    "GuestLyricChoice",
    "Login",
    "Finish",
    "PlaybackMode",
    "TVPair",
    "TVRemoteCommand",
    "TVRemoteReport",
    "Lyrics",
    "RenderRequest",
    "EventCreate",
    "PlaybackSettings",
    "JoinEvent",
    "RegisterUser",
    "UserLogin",
    "Store",
    "time_now",
    "render_video",
    "search_lyric_candidates",
    "httpx",
]
