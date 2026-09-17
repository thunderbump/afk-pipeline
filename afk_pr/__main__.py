"""Command entry points for explicit asynchronous PR reviews."""

import argparse
import json
import subprocess
from pathlib import Path

from afk_pr.github import GitHub, identity
from afk_pr.jobs import PHASES, read, status_job, submit, worker


def main(argv=None):
    from afk_pr.config import DEFAULT_CONFIG, load_config
    from afk_run import PreparationError

    parser = argparse.ArgumentParser(prog="afk")
    commands = parser.add_subparsers(dest="command", required=True)
    finishing = commands.add_parser(
        "finish", help="preview or explicitly merge a PR and close a selected Bead"
    )
    finishing.add_argument("pr_url")
    finishing.add_argument("--apply", metavar="PREVIEW_ID")
    finishing.add_argument("--close-bead")
    finishing.add_argument("--method", choices=("merge", "squash", "rebase"))
    finishing.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    evaluation = commands.add_parser(
        "evaluate", help="read-only advisory Bead readiness report"
    )
    evaluation.add_argument("bead_id")
    evaluation.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    create = commands.add_parser(
        "pr", help="implement a central Bead and create one draft PR"
    )
    create.add_argument("bead_id")
    create.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    create.add_argument("--retry-publication", metavar="JOB_ID")
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
    respond = commands.add_parser(
        "respond", help="respond to PR feedback in a background job"
    )
    respond.add_argument("pr_url")
    respond.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    respond.add_argument("--retry-publication", metavar="JOB_ID")
    context = commands.add_parser(
        "context", help="read complete PR context without posting"
    )
    context.add_argument("pr_url")
    status = commands.add_parser(
        "status", help="read PR results and local review job status"
    )
    status.add_argument("pr_url")
    status.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    clean = commands.add_parser(
        "cleanup",
        help="remove successful inactive clone workspaces; retain job evidence",
    )
    clean.add_argument("job_id")
    clean.add_argument("--dry-run", action="store_true")
    clean.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    background = commands.add_parser("worker", help=argparse.SUPPRESS)
    background.add_argument("directory", type=Path)
    background.add_argument("phase", choices=PHASES)
    args = parser.parse_args(argv)
    try:
        if args.command == "finish":
            from afk_pr.finish import finish

            result = finish(
                args.pr_url,
                args.config,
                apply=args.apply,
                close_bead=args.close_bead,
                method=args.method,
            )
            print(json.dumps(result, indent=2))
            return 0 if result["state"] in {"preview", "completed"} else 1
        if args.command == "evaluate":
            from afk_pr.evaluation import evaluate

            result = evaluate(args.bead_id, args.config)
            print(json.dumps(result, indent=2))
            return 0 if result["state"] == "completed" else 1
        if args.command == "worker":
            worker(args.directory, args.phase)
            return 0
        if args.command == "pr":
            from afk_pr.creation import submit_creation

            result = submit_creation(
                args.bead_id, args.config, retry=args.retry_publication
            )
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "cleanup":
            import re

            from afk_pr.lifecycle import cleanup

            if not re.fullmatch(r"[0-9a-f]{16}", args.job_id):
                raise ValueError("invalid job ID")
            config = load_config(args.config, historical=True)
            print(
                json.dumps(
                    cleanup(
                        config["run_root"] / "pr-reviews" / args.job_id,
                        dry_run=args.dry_run,
                    ),
                    indent=2,
                )
            )
            return 0
        identity(args.pr_url)
        if args.command == "context":
            result = GitHub().observe(args.pr_url)
        elif args.command in {"review", "respond"} and not args.retry_publication:
            result = submit(
                args.pr_url,
                args.config,
                fixtures_only=getattr(args, "fixtures_only", False),
                respond=args.command == "respond",
            )
        else:
            config = load_config(args.config, historical=True)
            root = Path(config["run_root"]) / "pr-reviews"
            if args.command in {"review", "respond"}:
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
                            and read(directory / "job.json").get("pr_url")
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
        PreparationError,
        ValueError,
        TypeError,
        RuntimeError,
        KeyError,
        subprocess.SubprocessError,
    ) as error:
        print(json.dumps({"outcome": "failed", "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
