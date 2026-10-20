"""Harmless Python protocol double. It is not Rust formatting or VM acceptance."""

import argparse
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

RECIPE = "a" * 64
IMPLEMENTATION = "b" * 64
UNFORMATTED = b"fn main( ){ }\n"
FORMATTED = b"fn main() { }\n"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def selected(name):
    return name == "rustfmt.toml" or (
        name.startswith("rust/actors/")
        and "/target/" not in name
        and name.endswith((".rs", "/Cargo.toml"))
    )


def policy(mode="normal"):
    root = str(Path(__file__).resolve().parents[1])
    return {
        "command": [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {root!r}); from tests.formatting_fixture import main; main()",
            "--mode",
            mode,
        ],
        "recipe_sha256": RECIPE,
        "implementation_sha256": IMPLEMENTATION,
        "timeout_seconds": 5,
        "termination_grace_seconds": 1,
    }


def source(repo, base):
    names = subprocess.check_output(
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
    ).split(b"\0")
    files = []
    for name in sorted({v.decode() for v in names if v}):
        if not selected(name) or not (repo / name).exists():
            continue
        if (repo / name).is_symlink():
            sys.exit(2)
        data = (repo / name).read_bytes()
        mode = "100755" if (repo / name).stat().st_mode & 0o111 else "100644"
        files.append(
            {
                "path": name,
                "mode": mode,
                "bytes": len(data),
                "sha256": digest(data),
                "content_base64": base64.b64encode(data).decode(),
            }
        )
    present = {entry["path"] for entry in files}
    baseline = subprocess.check_output(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "-z", base]
    ).split(b"\0")
    return {
        "version": 1,
        "scope": "working-snapshot-input",
        "base": base,
        "head": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])
        .decode()
        .strip(),
        "recipe_sha256": RECIPE,
        "files": files,
        "deleted": sorted(
            name.decode()
            for name in baseline
            if name and selected(name.decode()) and name.decode() not in present
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="normal")
    parser.add_argument("--identity", action="store_true")
    parser.add_argument("--snapshot-identity", action="store_true")
    parser.add_argument("--apply-result", type=Path)
    parser.add_argument("--expected-input")
    parser.add_argument("--expected-recipe")
    parser.add_argument("--expected-implementation")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.identity:
        print(
            json.dumps(
                {
                    "version": 1,
                    "scope": "working-snapshot-tool",
                    "recipe_sha256": RECIPE,
                    "implementation_sha256": "c" * 64
                    if args.mode == "tool-drift"
                    else IMPLEMENTATION,
                    "workspace": "rust/actors",
                }
            )
        )
        return
    snapshot = source(args.repo, args.base)
    identity = {
        **snapshot,
        "input_snapshot_sha256": digest(canonical(snapshot)),
        "files": [
            {k: v for k, v in entry.items() if k != "content_base64"}
            for entry in snapshot["files"]
        ],
    }
    if args.snapshot_identity:
        print(json.dumps(identity))
        return
    if args.apply_result:
        # This is a protocol double, not the Stack helper's security implementation.
        # Stack owns canonical scope, path/bounds validation and rollback tests.
        if args.mode in {
            "bad-path",
            "wrong-identity",
            "duplicate",
            "incomplete-cleanup",
            "rollback",
        }:
            sys.exit(2)
        result = json.loads(args.apply_result.read_text())
        if (
            args.expected_input != identity["input_snapshot_sha256"]
            or result["input_snapshot_sha256"] != args.expected_input
            or args.expected_recipe != RECIPE
            or args.expected_implementation != IMPLEMENTATION
        ):
            sys.exit(2)
        for change in result["changed_files"]:
            (args.repo / change["path"]).write_bytes(
                base64.b64decode(change["content_base64"])
            )
        receipt = {
            "version": 1,
            "scope": "working-snapshot-apply",
            "exit_code": 0,
            "base": args.base,
            "head": identity["head"],
            "input_snapshot_sha256": identity["input_snapshot_sha256"],
            "output_snapshot_sha256": result["output_snapshot_sha256"],
            "recipe_sha256": RECIPE,
            "implementation_sha256": IMPLEMENTATION,
            "applied_paths": [c["path"] for c in result["changed_files"]],
            "vm_started": False,
        }
        args.output.write_text(json.dumps(receipt))
        print(json.dumps({"scope": "test-apply-only"}))
        return
    if args.mode == "fail":
        print("Fixture formatter unavailable", file=sys.stderr)
        sys.exit(2)
    if args.mode == "timeout":
        import time

        time.sleep(10)
    changed = []
    for entry in snapshot["files"]:
        if not entry["path"].endswith(".rs"):
            continue
        data = base64.b64decode(entry["content_base64"])
        after = data.replace(UNFORMATTED, FORMATTED)
        if after != data:
            changed.append(
                {
                    "path": entry["path"],
                    "before_sha256": entry["sha256"],
                    "after_sha256": digest(after),
                    "content_base64": base64.b64encode(after).decode(),
                }
            )
            entry.update(
                bytes=len(after),
                sha256=digest(after),
                content_base64=base64.b64encode(after).decode(),
            )
    result = {
        "version": 1,
        "scope": "working-snapshot-format",
        "operation": "format",
        "base": args.base,
        "head": identity["head"],
        "input_snapshot_sha256": identity["input_snapshot_sha256"],
        "output_snapshot_sha256": digest(canonical(snapshot)),
        "recipe_sha256": RECIPE,
        "implementation_sha256": IMPLEMENTATION,
        "exit_code": 0,
        "vm_started": True,
        "cleanup_complete": True,
        "changed_files": changed,
        "timings": {
            "preparation_seconds": 0,
            "boot_seconds": 0,
            "format_seconds": 0,
            "total_seconds": 0,
        },
    }
    if args.mode == "stale":
        (args.repo / "rust/actors/lib.rs").write_text("newer edit\n")
    if args.mode == "bad-path":
        result["changed_files"][0]["path"] = "outside.rs"
    if args.mode == "wrong-identity":
        result["implementation_sha256"] = "c" * 64
    if args.mode == "duplicate":
        result["changed_files"] *= 2
    if args.mode == "incomplete-cleanup":
        result["cleanup_complete"] = False
    args.output.write_text("{" if args.mode == "malformed" else json.dumps(result))
    print(json.dumps({"scope": "test-formatter-only", "acceptance": False}))
