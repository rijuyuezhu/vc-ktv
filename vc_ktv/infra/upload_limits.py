"""Bound untrusted lyric files before parsing them in application memory."""

from fastapi import UploadFile

from vc_ktv.application.errors import bail

MAX_LRC_BYTES = 1024 * 1024
MAX_TEXTGRID_BYTES = 8 * 1024 * 1024


async def read_lyric_upload(file: UploadFile, max_bytes: int, label: str) -> bytes:
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        bail(413, f"{label} 文件不能超过 {max_bytes // (1024 * 1024)} MiB")
    return content
