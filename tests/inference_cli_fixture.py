"""Install a fake Pi executable while preserving the production runtime path."""

import shlex
import sys
from pathlib import Path


def install_pi(bin_directory: Path, fixture: Path, scenario: str) -> None:
    path = bin_directory / "pi"
    command = " ".join(
        shlex.quote(item) for item in (sys.executable, str(fixture), scenario)
    )
    path.write_text(f"#!/bin/sh\nexec {command}\n")
    path.chmod(0o755)
