"""Optional systemd supervisor over independent AFK commands."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from afk_orchestrate import driver


def unit(identifier):
    return f"afk-orchestrate-{identifier}"


def launch(path):
    state = driver.read(path)
    subprocess.run(
        [
            "systemd-run",
            "--user",
            "--collect",
            "--quiet",
            f"--unit={unit(state['id'])}",
            "--property=Type=exec",
            "--property=KillMode=control-group",
            f"--working-directory={driver.ROOT}",
            sys.executable,
            "-m",
            "afk_orchestrate",
            "worker",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def worker(path):
    # Holding the lock for the lifetime of the worker excludes a second driver
    # and operator transitions, including during waits and child commands.
    with driver.lock(path):
        state = driver.read(path)
        commands = driver.Commands(state["config"])
        while state["status"] == "running":
            previous = state["stage"]
            driver.step(state, commands)
            driver.write(path, state)
            if state["status"] == "running" and state["stage"] == previous:
                time.sleep(30)


def status(path):
    state = driver.read(path)
    try:
        process = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                f"{unit(state['id'])}.service",
                "--property=ActiveState",
                "--value",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        observation = (
            process.stdout.strip() if process.returncode == 0 else "unavailable"
        )
    except (OSError, subprocess.SubprocessError):
        observation = "unavailable"
    return {"run": state, "directory": str(path.parent), "worker": observation}


def main(argv=None):
    from afk_pr.config import DEFAULT_CONFIG, load_config
    from afk_run import PreparationError

    parser = argparse.ArgumentParser(prog="afk orchestrate")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="start one supervised run per Bead")
    start.add_argument("bead_id")
    start.add_argument("--max-repairs", type=int, choices=range(6), default=5)
    start.add_argument(
        "--no-start", action="store_true", help="save a run for explicit step calls"
    )
    for name in ("status", "step", "resume"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        command.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        if name == "resume":
            command.add_argument("--review-current-head", action="store_true")
    start.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    background = commands.add_parser("worker", help=argparse.SUPPRESS)
    background.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    path = None
    try:
        if args.command == "worker":
            worker(args.path)
            return 0
        config_path = args.config.expanduser().resolve()
        config = load_config(config_path, historical=True)
        root = config["run_root"] / "orchestrations"
        if args.command == "start":
            path, created = driver.create(
                root, args.bead_id, config_path, args.max_repairs
            )
            if created and not args.no_start:
                launch(path)
        else:
            if not driver.JOB.fullmatch(args.run_id):
                raise ValueError("invalid run ID")
            path = root / args.run_id / "state.json"
            if args.command == "step":
                driver.advance(path)
            elif args.command == "resume":
                state = driver.resume(
                    path, review_current_head=args.review_current_head
                )
                if state["status"] == "running":
                    launch(path)
        print(json.dumps(status(path), indent=2))
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        PreparationError,
        subprocess.SubprocessError,
    ) as error:
        result = {"outcome": "failed", "error": str(error)}
        if path is not None:
            result["directory"] = str(path.parent)
        print(json.dumps(result, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
