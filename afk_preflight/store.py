"""Immutable classification reuse keyed by validated input and adapter policy."""

import errno
import fcntl
import json
import os
import stat
import uuid
from pathlib import Path

from afk_preflight.contract import (
    POLICY_FIELDS,
    canonical,
    classification_key,
    digest,
    validate_classification,
    validate_input,
)

MAX_RECORD_BYTES = 8 * 1024 * 1024


class ClassificationRecordError(ValueError):
    """A stored classification exists but cannot be trusted."""


def resolve(
    store: Path,
    preflight_input: dict[str, object],
    policy: dict[str, object],
    infer,
) -> dict[str, object]:
    """Return the immutable classification for one input and policy key."""
    accepted_input = validate_input(preflight_input)
    accepted_policy = validate_policy(policy)
    input_sha256 = digest(accepted_input)
    key = classification_key(accepted_input, accepted_policy)
    store.mkdir(parents=True, exist_ok=True)
    root = os.open(store, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    record_name = f"{key}.json"
    lock_name = f"{key}.lock"
    try:
        lock = os.open(
            lock_name,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
            dir_fd=root,
        )
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise OSError("classification lock is not a regular file")
            fcntl.flock(lock, fcntl.LOCK_EX)
            record = read_record(root, record_name)
            if record is not None:
                try:
                    validate_record(record, key, input_sha256, accepted_policy)
                    requests = validate_classification(
                        accepted_input, record["classification"]
                    )
                except ClassificationRecordError:
                    raise
                except (KeyError, TypeError, ValueError) as error:
                    raise ClassificationRecordError(
                        "classification record contains an invalid classification"
                    ) from error
                source = "reused"
            else:
                classification = infer()
                requests = validate_classification(accepted_input, classification)
                record = {
                    "schema_version": 1,
                    "key": key,
                    "input_sha256": input_sha256,
                    "policy": accepted_policy,
                    "classification": {
                        "schema_version": 1,
                        "requests": requests,
                    },
                }
                write_record(root, record_name, record)
                source = "inferred"
        finally:
            os.close(lock)
    finally:
        os.close(root)
    return {
        "key": key,
        "source": source,
        "record": record_name,
        "policy": accepted_policy,
        "requests": requests,
    }


def validate_policy(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != POLICY_FIELDS:
        raise ValueError("classification policy has an invalid shape")
    for field in POLICY_FIELDS:
        item = value[field]
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"classification policy {field} must be nonempty text")
    return value


def read_record(root: int, name: str) -> object | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ClassificationRecordError("classification record is unsafe") from error
    try:
        observed = os.fstat(descriptor)
        size = observed.st_size
        if not stat.S_ISREG(observed.st_mode):
            raise ClassificationRecordError(
                "classification record is not a regular file"
            )
        if size > MAX_RECORD_BYTES:
            raise ClassificationRecordError("classification record is oversized")
        chunks = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0 and os.read(descriptor, 1):
            raise ClassificationRecordError("classification record is oversized")
    finally:
        os.close(descriptor)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClassificationRecordError("classification record is malformed") from error


def validate_record(value, key, input_sha256, policy):
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "key",
        "input_sha256",
        "policy",
        "classification",
    }:
        raise ClassificationRecordError("classification record has an invalid shape")
    if (
        value["schema_version"] != 1
        or value["key"] != key
        or value["input_sha256"] != input_sha256
        or value["policy"] != policy
    ):
        raise ClassificationRecordError(
            "classification record does not match its lookup"
        )


def write_record(root: int, name: str, value: object) -> None:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = None
    payload = canonical(value) + b"\n"
    if len(payload) > MAX_RECORD_BYTES:
        raise ValueError("classification record exceeds the storage limit")
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=root,
        )
        while payload:
            written = os.write(descriptor, payload)
            payload = payload[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(
            temporary,
            name,
            src_dir_fd=root,
            dst_dir_fd=root,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=root)
        os.fsync(root)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=root)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
