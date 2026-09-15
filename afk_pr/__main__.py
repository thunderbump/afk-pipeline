"""Command entry points for explicit asynchronous PR reviews."""

import argparse
import json
import subprocess
from pathlib import Path

from afk_pr.github import GitHub, identity
from afk_pr.jobs import PHASES, read, settings, status_job, submit, worker


def main(argv=None):
    from afk_run import DEFAULT_CONFIG

    parser = argparse.ArgumentParser(prog="afk")
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser(
        "review", help="submit fixtures and a read-only PR review"
    )
    review.add_argument("pr_url")
    review.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    review.add_argument(
        "--fixtures-only",
        action="store_true",
        help="use existing PR reviewers; always run fixtures",
    )
    review.add_argument(
        "--retry-publication",
        metavar="JOB_ID",
        help="publish retained results without rerunning fixtures or inference",
    )
    context = commands.add_parser(
        "context", help="read complete PR context without posting"
    )
    context.add_argument("pr_url")
    status = commands.add_parser(
        "status", help="read PR results and local review job status"
    )
    status.add_argument("pr_url")
    status.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    background = commands.add_parser("worker", help=argparse.SUPPRESS)
    background.add_argument("directory", type=Path)
    background.add_argument("phase", choices=PHASES)
    args = parser.parse_args(argv)
    try:
        if args.command == "worker":
            worker(args.directory, args.phase)
            return 0
        identity(args.pr_url)
        if args.command == "context":
            result = GitHub().observe(args.pr_url)
        elif args.command == "review" and not args.retry_publication:
            result = submit(args.pr_url, args.config, fixtures_only=args.fixtures_only)
        else:
            config, _, _ = settings(args.config, args.pr_url)
            root = Path(config["run_root"]) / "pr-reviews"
            if args.command == "review":
                import re

                from afk_pr.jobs import retry_publication

                if not re.fullmatch(r"[0-9a-f]{16}", args.retry_publication):
                    raise ValueError("invalid job ID")
                directory = root / args.retry_publication
                if identity(read(directory / "job.json")["pr_url"]) != identity(
                    args.pr_url
                ):
                    raise ValueError("job belongs to another PR")
                retry_publication(directory)
                result = status_job(directory)
            else:
                context = GitHub().observe(args.pr_url)
                jobs = []
                if root.exists():
                    for directory in sorted(root.iterdir()):
                        if (
                            directory.is_dir()
                            and (directory / "job.json").exists()
                            and identity(read(directory / "job.json")["pr_url"])
                            == identity(args.pr_url)
                        ):
                            jobs.append(status_job(directory))
                result = {
                    "pr_url": args.pr_url,
                    "head": context["pull_request"]["head"]["sha"],
                    "checks": context["checks"],
                    "statuses": context["statuses"],
                    "reviews": context["reviews"],
                    "jobs": jobs,
                }
        print(json.dumps(result, indent=2))
        return 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        subprocess.SubprocessError,
    ) as error:
        print(json.dumps({"outcome": "failed", "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
