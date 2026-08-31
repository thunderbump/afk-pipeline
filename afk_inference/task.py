"""Provider-neutral value object for a role-owned inference task contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .runtime import Capability


@dataclass(frozen=True)
class TaskContract:
    """One fully selected domain task, ready for the Inference Runtime."""

    purpose: str
    contract_version: int
    trusted_instructions: str
    untrusted_data: Any
    capability: Capability
    validator: Callable[[object], Any]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.contract_version, int)
            or isinstance(self.contract_version, bool)
            or self.contract_version <= 0
        ):
            raise ValueError("task contract_version must be a positive integer")
        if not callable(self.validator):
            raise TypeError("task validator must be callable")
