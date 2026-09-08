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
        opened = []
        seen = set()
        try:
            for root in roots:
                absolute = Path(root).absolute()
                if absolute in seen:
                    continue
                seen.add(absolute)
                opened.append(self._open_root(absolute))
        except Exception:
            for _path, descriptor in opened:
                os.close(descriptor)
            raise
        self.roots = tuple(path for path, _descriptor in opened)
        self._root_descriptors = {path: descriptor for path, descriptor in opened}
        self.identities = {}
        # Retain the first observation for every pathname for the lifetime of
        # this reader. A later open must describe the same file and bytes.
        self._observations = {}

    def close(self):
        descriptors = getattr(self, "_root_descriptors", {})
        self._root_descriptors = {}
        for descriptor in descriptors.values():
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __del__(self):
        self.close()

    def authorize_directory(self, path):
        """Confirm that a referenced directory is within caller authority.

        References found in evidence never enlarge the immutable root set.  The
        directory itself need not exist yet: a later read classifies that as
        unavailable evidence rather than as a malformed trusted root.
        """
        self.relative(path)

    def authorize_file(self, path):
        """Confirm that a referenced file is within caller authority."""
        self.relative(path)

    @staticmethod
    def _open_root(root):
        """Open and pin a trusted root without following any path component."""
        absolute = root.absolute()
        descriptor = None
        try:
            descriptor = os.open(
                absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            for part in absolute.parts[1:]:
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise EvidenceAccessError(
                "trusted evidence root is unavailable or unsafe"
            ) from error
        return absolute, descriptor

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
        try:
            descriptor = os.dup(self._root_descriptors[root])
        except (KeyError, OSError) as error:
            raise EvidenceAccessError("trusted evidence root is unavailable") from error
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
            if (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or len(raw) != before.st_size:
                raise EvidenceAccessError("evidence changed while it was read")
            pathname = str(Path(path).absolute())
            digest = hashlib.sha256(raw).hexdigest()
            observation = (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                digest,
            )
            previous = self._observations.get(pathname)
            if previous is not None and previous != observation:
                raise EvidenceAccessError(f"evidence changed between reads: {path}")
            self._observations[pathname] = observation
            self.identities[pathname] = digest
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
