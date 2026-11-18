"""Serving video files with HTTP Range support, so the browser streams a preview instead of downloading it."""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

CHUNK = 256 * 1024
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


def serve(path: Path, request: Request, media_type: str = "video/mp4"):
    if not path.is_file():
        raise HTTPException(404, "File not found.")
    size = path.stat().st_size
    header = request.headers.get("range")
    if not header:
        return FileResponse(path, media_type=media_type, headers={"Accept-Ranges": "bytes",
                                                                   "Cache-Control": "private, max-age=3600"})
    m = _RANGE.match(header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        raise HTTPException(416, "Bad range", headers={"Content-Range": f"bytes */{size}"})
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
    else:                                   # suffix range: the last N bytes
        start = max(0, size - int(m.group(2)))
        end = size - 1
    end = min(end, size - 1, start + 8 * 2**20 - 1)    # never more than 8 MB per response
    if start > end or start >= size:
        raise HTTPException(416, "Range not satisfiable", headers={"Content-Range": f"bytes */{size}"})

    def body():
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                data = f.read(min(CHUNK, left))
                if not data:
                    break
                left -= len(data)
                yield data

    return StreamingResponse(body(), status_code=206, media_type=media_type, headers={
        "Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1), "Cache-Control": "private, max-age=3600"})


def file_size(path: Path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
