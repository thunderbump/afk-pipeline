"""Shared validation for optional AFK configuration sections."""

from pathlib import Path


def attestation_result_root(value):
    """Return the configured existing attestation root or raise ValueError."""
    if not isinstance(value, dict) or set(value) != {"result_root"}:
        raise ValueError("configuration attestation is malformed")
    configured = value["result_root"]
    if not isinstance(configured, str) or not Path(configured).is_absolute():
        raise ValueError("attestation result_root must be an absolute path")
    root = Path(configured).resolve()
    if not root.is_dir():
        raise ValueError("configured attestation result_root must already exist")
    return root
