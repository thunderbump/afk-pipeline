"""Bounded, no-follow local evidence access shared by all proof readers."""

import hashlib
import json
import os
import stat
from pathlib import Path

MAX_JSON_BYTES = 1024 * 1024
MAX_RELATED_WORK_BYTES = 256 * 1024
MAX_VALIDATION_LOG_BYTES = 25 * 1024 * 1024


class EvidenceAccessError(ValueError):
    """A reference is malformed, unsafe, or outside caller authority."""


class EvidenceUnavailable(Exception):
    def __init__(self, reason, identity):
        self.reason, self.identity = reason, identity


class EvidenceReader:
    """Per-call reader that opens every component without following symlinks."""

    def __init__(self, roots):
        self.roots = tuple(self._safe_root(Path(root)) for root in roots)
        self.identities = {}

    @staticmethod
    def _safe_root(root):
        absolute = root.absolute()
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            try:
                facts = current.lstat()
            except OSError as error:
                raise EvidenceAccessError(
                    "trusted evidence root is unavailable"
                ) from error
            if stat.S_ISLNK(facts.st_mode):
                raise EvidenceAccessError("trusted evidence root contains a symlink")
        if not absolute.is_dir():
            raise EvidenceAccessError("trusted evidence root is not a directory")
        return absolute

    def relative(self, path):
        absolute = Path(path).absolute()
        for root in self.roots:
            try:
                return root, absolute.relative_to(root)
            except ValueError:
                pass
        raise EvidenceAccessError(
            f"evidence reference escapes trusted roots: {absolute}"
        )

    def bytes(self, path, limit, *, missing_unavailable=True):
        root, relative = self.relative(path)
        if not relative.parts:
            raise EvidenceAccessError("evidence reference is not a file")
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened = [descriptor]
        try:
            for part in relative.parts[:-1]:
                if part in ("", ".", ".."):
                    raise EvidenceAccessError("unsafe evidence path")
                try:
                    descriptor = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError as error:
                    if missing_unavailable:
                        raise EvidenceUnavailable(
                            "missing evidence", str(path)
                        ) from error
                    raise
                except OSError as error:
                    raise EvidenceAccessError(
                        f"unsafe evidence path: {path}"
                    ) from error
                opened.append(descriptor)
            try:
                file_descriptor = os.open(
                    relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            except FileNotFoundError as error:
                if missing_unavailable:
                    raise EvidenceUnavailable("missing evidence", str(path)) from error
                raise
            except OSError as error:
                raise EvidenceAccessError(f"unsafe evidence file: {path}") from error
            opened.append(file_descriptor)
            before = os.fstat(file_descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise EvidenceAccessError(f"evidence is not a regular file: {path}")
            if before.st_size > limit:
                raise EvidenceUnavailable(
                    "evidence exceeds proof-read limit", str(path)
                )
            chunks, remaining = [], limit + 1
            while remaining:
                chunk = os.read(file_descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(file_descriptor)
            if len(raw) > limit:
                raise EvidenceUnavailable(
                    "evidence exceeds proof-read limit", str(path)
                )
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or len(raw) != before.st_size:
                raise EvidenceAccessError("evidence changed while it was read")
            self.identities[str(Path(path).absolute())] = hashlib.sha256(
                raw
            ).hexdigest()
            return raw
        finally:
            for item in reversed(opened):
                os.close(item)

    def json(self, path):
        raw = self.bytes(path, MAX_JSON_BYTES)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvidenceAccessError(f"malformed JSON evidence: {path}") from error
