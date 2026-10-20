"""Harmless protocol double. No compiler, Cargo, server or VM is executed."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

CHECKS = [
    "rust-format",
    "rust-dependencies",
    "rust-clippy",
    "rust-tests",
    "native-configure",
    "native-objects",
]
RECIPE = "c" * 64
IMPLEMENTATION = "d" * 64


def policy(mode="normal", endpoint=None):
    root = str(Path(__file__).resolve().parents[1])
    return {
        "command": [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {root!r}); from tests.quickcheck_fixture import main; main()",
            "--mode",
            mode,
            *(["--endpoint", str(endpoint)] if endpoint else []),
        ],
        "recipe_sha256": RECIPE,
        "implementation_sha256": IMPLEMENTATION,
        "timeout_seconds": 5,
    }


def snapshot(repo, base):
    files = {}
    for raw in subprocess.check_output(
        [
            "git",
            "-C",
            str(repo),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ]
    ).split(b"\0"):
        if raw and (repo / raw.decode()).is_file():
            files[raw.decode()] = (repo / raw.decode()).read_bytes()
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return {
        "version": 1,
        "scope": "working-snapshot-quick-input",
        "base": base,
        "head": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])
        .decode()
        .strip(),
        "recipe_sha256": RECIPE,
        "input_snapshot_sha256": identity,
    }, files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="normal")
    parser.add_argument("--endpoint")
    parser.add_argument("--identity", action="store_true")
    parser.add_argument("--snapshot-identity", action="store_true")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--expected-input")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.identity:
        if args.mode == "metadata-sleep":
            time.sleep(10)
        print(
            json.dumps(
                {
                    "version": 1,
                    "scope": "working-snapshot-quick-tool",
                    "recipe_sha256": RECIPE,
                    "implementation_sha256": "e" * 64
                    if args.mode == "tool-drift"
                    else IMPLEMENTATION,
                    "checks": CHECKS,
                    "rust_input_sha256": "e" * 64,
                    "native_input_sha256": "f" * 64,
                }
            )
        )
        return
    before, files = snapshot(args.repo, args.base)
    if args.snapshot_identity:
        print(json.dumps(before))
        return
    if args.mode == "socket":
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(args.endpoint)
            connection.sendall(
                json.dumps({"pid": os.getpid(), "group": os.getpgrp()}).encode()
            )
            connection.recv(1)
    if args.mode == "sleep":
        time.sleep(10)
    code = (
        2
        if args.mode == "infra"
        else 1
        if any(b"BROKEN" in data for data in files.values())
        else 0
    )
    failed = ["native-objects"] if code == 1 else []
    missing = ["native-objects"] if code == 2 else []
    completed = [name for name in CHECKS if name not in failed + missing]
    result = {
        **before,
        "scope": "working-snapshot-quickcheck",
        "operation": "quick",
        "advisory": True,
        "exit_code": code,
        "output_snapshot_sha256": before["input_snapshot_sha256"],
        "implementation_sha256": IMPLEMENTATION,
        "rust_input_sha256": "e" * 64,
        "native_input_sha256": "f" * 64,
        "vm_started": True,
        "cleanup_complete": args.mode != "no-cleanup",
        "selected_checks": CHECKS,
        "completed_checks": completed,
        "failed_checks": failed,
        "unavailable_checks": missing,
        "skipped_checks": [],
        "checks": [
            {
                "name": name,
                "status": "failed"
                if name in failed
                else "unavailable"
                if name in missing
                else "completed",
                "reason": "trusted protocol fixture",
                "seconds": 0,
                "diagnostic": "sample.cpp:1: fixture mutable-reference error"
                if name in failed
                else "",
            }
            for name in CHECKS
        ],
    }
    if args.mode == "stale":
        (args.repo / "sample.cpp").write_text("newer edit\n")
    args.output.write_text("{" if args.mode == "malformed" else json.dumps(result))
    print(json.dumps({"version": 1, "advisory": True, "scope": "fixture-only"}))
    sys.exit(code)
