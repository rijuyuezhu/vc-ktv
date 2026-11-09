"""Compact large polling snapshots without changing streamed media responses."""

import gzip

from fastapi import Request
from fastapi.responses import JSONResponse


def _accepts_gzip(header: str) -> bool:
    for entry in header.lower().split(","):
        parts = [part.strip() for part in entry.split(";")]
        if parts[0] != "gzip":
            continue
        try:
            quality = next(
                (float(value[2:]) for value in parts[1:] if value.startswith("q=")),
                1.0,
            )
        except ValueError:
            return False
        return quality > 0
    return False


def state_response(request: Request, data: dict) -> JSONResponse:
    """Encode in the sync endpoint's worker thread; gzip only large JSON."""
    response = JSONResponse(data)
    if len(response.body) < 4096:
        return response
    response.headers["vary"] = "Accept-Encoding"
    if not _accepts_gzip(request.headers.get("accept-encoding", "")):
        return response
    compressed = gzip.compress(response.body, compresslevel=1)
    if len(compressed) < len(response.body):
        response.body = compressed
        response.headers["content-encoding"] = "gzip"
        response.headers["content-length"] = str(len(compressed))
        response.headers["vary"] = "Accept-Encoding"
    return response
