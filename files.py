"""Photos and files: what techs may attach, HEIC conversion, and the ticket gallery.

Files are stored in Zendesk (attached to the stage's comment), not here. The
portal lists every attachment on the ticket, including ones the customer sent,
and serves them through an authenticated proxy route.
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024  # Zendesk's per-attachment limit
MAX_FILES = 10

# extension -> content type sent to Zendesk
ALLOWED = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "heic": "image/heic",
    "heif": "image/heif",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "pdf": "application/pdf",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
ACCEPT_ATTR = ",".join("." + ext for ext in ALLOWED)
# Browsers can show these inline; everything else is a download link.
INLINE_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
VIDEO_URL_RE = re.compile(r"^https?://[^\s<>\"']{4,500}$")


@dataclass
class Upload:
    filename: str
    data: bytes
    content_type: str


def _safe_name(name: str) -> str:
    base = os.path.basename(name or "file").strip() or "file"
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", base)[:120]


def read_uploads(file_storages) -> tuple[list[Upload], str | None]:
    """Validate uploaded files from a form. Returns (uploads, error message)."""
    picked = [f for f in file_storages if f and f.filename]
    if len(picked) > MAX_FILES:
        return [], f"Attach at most {MAX_FILES} files at a time."
    uploads = []
    for f in picked:
        name = _safe_name(f.filename)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ALLOWED:
            return [], f"“{name}” isn't a supported file type (photos, PDF or Excel)."
        data = f.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            return [], f"“{name}” is over 50 MB."
        if not data:
            continue
        uploads.append(_maybe_convert(Upload(name, data, ALLOWED[ext]), ext))
    return uploads, None


def _maybe_convert(upload: Upload, ext: str) -> Upload:
    """iPhone HEIC photos become JPEG so browsers and customers can open them."""
    if ext not in ("heic", "heif"):
        return upload
    try:
        from PIL import Image
        from pillow_heif import register_heif_opener

        register_heif_opener()
        with Image.open(io.BytesIO(upload.data)) as img:
            out = io.BytesIO()
            img.convert("RGB").save(out, "JPEG", quality=88)
        return Upload(upload.filename.rsplit(".", 1)[0] + ".jpg", out.getvalue(), "image/jpeg")
    except Exception:  # keep the original if conversion isn't possible
        logger.warning("Could not convert %s to JPEG; attaching as-is", upload.filename, exc_info=True)
        return upload


def clean_video_url(raw: str | None) -> str | None | bool:
    """A video link (YouTube, Vimeo, OneDrive...). None if blank, False if invalid."""
    url = (raw or "").strip()
    if not url:
        return None
    return url if VIDEO_URL_RE.match(url) else False


def gallery(comments: list[dict], users: dict) -> list[dict]:
    """Every attachment on the ticket, newest first, with who sent it and whether it's public."""
    items = []
    for c in comments:
        author = (users.get(c.get("author_id")) or {}).get("name") or "Unknown"
        for a in c.get("attachments") or []:
            ctype = (a.get("content_type") or "").lower()
            thumbs = a.get("thumbnails") or []
            items.append(
                {
                    "id": a.get("id"),
                    "name": a.get("file_name") or "file",
                    "content_type": ctype,
                    "size": a.get("size") or 0,
                    "inline": ctype in INLINE_IMAGE_TYPES,
                    "thumb_id": thumbs[0].get("id") if thumbs else None,
                    "public": bool(c.get("public")),
                    "author": author,
                    "created_at": c.get("created_at"),
                }
            )
    items.sort(key=lambda i: i["created_at"] or "", reverse=True)
    return items


_CACHE_SECONDS = 300
_attachment_cache: dict[int, tuple[float, dict[int, dict]]] = {}
_cache_lock = threading.Lock()


def remember(ticket_id: int, comments: list[dict]) -> None:
    """Index a ticket's attachments (and thumbnails) so the file proxy needn't re-list comments."""
    index = {}
    for c in comments:
        for a in c.get("attachments") or []:
            index[a.get("id")] = a
            for t in a.get("thumbnails") or []:
                index[t.get("id")] = {**t, "file_name": t.get("file_name") or a.get("file_name")}
    with _cache_lock:
        _attachment_cache[ticket_id] = (time.monotonic(), index)
        if len(_attachment_cache) > 500:  # keep memory bounded
            oldest = min(_attachment_cache, key=lambda k: _attachment_cache[k][0])
            _attachment_cache.pop(oldest, None)


def cached_attachment(ticket_id: int, attachment_id: int) -> dict | None:
    with _cache_lock:
        hit = _attachment_cache.get(ticket_id)
    if not hit or time.monotonic() - hit[0] > _CACHE_SECONDS:
        return None
    return hit[1].get(attachment_id)


def find_attachment(comments: list[dict], attachment_id: int) -> dict | None:
    """The attachment (or one of its thumbnails) with this id on these comments."""
    for c in comments:
        for a in c.get("attachments") or []:
            if a.get("id") == attachment_id:
                return a
            for t in a.get("thumbnails") or []:
                if t.get("id") == attachment_id:
                    return t
    return None
