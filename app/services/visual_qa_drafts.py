"""Private, task-scoped storage for unapproved Visual QA reference drafts.

Draft images are proposals only. They are deliberately separate from the
committed PROJECT.yaml reference loader and cannot be consumed by TestRunner.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from pathlib import Path
from urllib.parse import unquote

from app.services.visual_qa_evidence import VisualEvidenceError, _png_dimensions, ensure_private_directory


MAX_DRAFT_PNG_BYTES = 8 * 1024 * 1024
MAX_DESIGN_NOTE_CHARS = 500
DRAFT_VIEWPORTS = frozenset({"430x932", "768x1024", "1366x768", "1586x992", "1920x1080"})
_DRAFT_REF = re.compile(r"visual-qa-drafts/([0-9a-f]{32})\.png\Z")
_UPLOAD_PATH = re.compile(r"/api/tasks/[0-9]+/workspaces/[0-9]+/visual-reference-drafts\Z")


class VisualDraftError(ValueError):
    """Invalid or unsafe uploaded draft image."""


class VisualDraftUploadBodyLimit:
    """Bound the entire multipart request before Starlette spools its file."""

    def __init__(self, app, *, max_bytes: int = MAX_DRAFT_PNG_BYTES + 64 * 1024):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http" or scope.get("method") != "POST"
                or not _UPLOAD_PATH.fullmatch(unquote(scope.get("path", "")))):
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > self.max_bytes:
                    await self._too_large(scope, send)
                    return
            except ValueError:
                await self._too_large(scope, send)
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._too_large(scope, send)
                return
            if not message.get("more_body", False):
                break
        data = bytes(body)
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": data, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _too_large(scope, send):
        body = b'{"detail":"Visual reference upload exceeds the request limit"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})


def _open_private_directory(path: Path) -> int:
    """Open every path component as a directory without following symlinks."""
    path = Path(path).absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def validate_visual_draft_png(data: bytes, viewport: str) -> tuple[int, int, str]:
    if viewport not in DRAFT_VIEWPORTS:
        raise VisualDraftError("Unsupported Visual QA viewport")
    if not isinstance(data, bytes) or not data or len(data) > MAX_DRAFT_PNG_BYTES:
        raise VisualDraftError("PNG is empty or exceeds the upload limit")
    try:
        width, height = _png_dimensions(data, "uploaded draft")
    except (VisualEvidenceError, OSError, ValueError) as exc:
        raise VisualDraftError("Upload must be a complete, valid PNG") from exc
    if f"{width}x{height}" != viewport:
        raise VisualDraftError("PNG dimensions must exactly match the selected viewport")
    return width, height, hashlib.sha256(data).hexdigest()


def write_visual_draft(state_dir: Path, data: bytes, viewport: str) -> tuple[str, str, int, int]:
    width, height, digest = validate_visual_draft_png(data, viewport)
    root = Path(state_dir) / "visual-qa-drafts"
    try:
        ensure_private_directory(root)
        root_fd = _open_private_directory(root)
    except (OSError, VisualEvidenceError) as exc:
        raise VisualDraftError("Private draft storage is unavailable") from exc
    name = f"{uuid.uuid4().hex}.png"
    fd = None
    created = False
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=root_fd)
        created = True
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
        os.fchmod(fd, 0o600)
    except OSError as exc:
        if created:
            try:
                os.unlink(name, dir_fd=root_fd)
            except OSError:
                pass
        raise VisualDraftError("Private draft storage write failed") from exc
    finally:
        if fd is not None:
            os.close(fd)
        os.close(root_fd)
    return f"visual-qa-drafts/{name}", digest, width, height


def delete_visual_draft(state_dir: Path, content_ref: str) -> None:
    match = _DRAFT_REF.fullmatch(content_ref or "")
    if not match:
        return
    root = Path(state_dir) / "visual-qa-drafts"
    try:
        root_fd = _open_private_directory(root)
        try:
            os.unlink(f"{match.group(1)}.png", dir_fd=root_fd)
        finally:
            os.close(root_fd)
    except FileNotFoundError:
        pass


def read_visual_draft(state_dir: Path, content_ref: str, expected_digest: str,
                      viewport: str, expected_size: int | None = None) -> bytes:
    match = _DRAFT_REF.fullmatch(content_ref or "")
    if not match or viewport not in DRAFT_VIEWPORTS:
        raise VisualDraftError("Draft image is unavailable")
    root = Path(state_dir) / "visual-qa-drafts"
    root_fd = None
    fd = None
    try:
        root_fd = _open_private_directory(root)
        fd = os.open(f"{match.group(1)}.png", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=root_fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_DRAFT_PNG_BYTES:
            raise VisualDraftError("Draft image is unavailable")
        chunks = bytearray()
        while len(chunks) <= MAX_DRAFT_PNG_BYTES:
            chunk = os.read(fd, min(1024 * 1024, MAX_DRAFT_PNG_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        after = os.fstat(fd)
        before_id = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        after_id = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        data = bytes(chunks)
        if before_id != after_id or len(data) != before.st_size or len(data) > MAX_DRAFT_PNG_BYTES:
            raise VisualDraftError("Draft image changed while reading")
        width, height = _png_dimensions(data, "draft image")
        if f"{width}x{height}" != viewport:
            raise VisualDraftError("Draft dimensions do not match the saved viewport")
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_digest or (expected_size is not None and expected_size != len(data)):
            raise VisualDraftError("Draft image integrity check failed")
        return data
    except (OSError, VisualEvidenceError) as exc:
        raise VisualDraftError("Draft image is unavailable") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if root_fd is not None:
            os.close(root_fd)
