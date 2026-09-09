"""Replay frozen Review tasks with latest-change or complete-work Git diffs.

Run from this checkout: python3 -m experiments.review_context --help.
Results are experimental evidence, never Coordinator/Review completion records.
"""

import argparse
import copy
import hashlib
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from afk_inference import Capability, InferenceRuntime, PiAdapter, ResponseRejected
from afk_review.contract import validate_review

MAX_BYTES = 16 * 1024 * 1024
CONTEXT_NOTE = (
    "The reviewed_commits range describes reviewed_diff. committed_change retains "
    "the latest recorded implementation change and may cover a narrower range. "
    "Inspect repository files in the current execution workspace; other retained "
    "artifact paths are historical metadata, not additional execution roots."
)


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE
    )


def read_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("experiment input exceeds 16 MiB")
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def diff(repo, before, after):
    value = git(repo, "diff", "--no-ext-diff", "--binary", f"{before}..{after}", "--")
    if len(value.encode()) > MAX_BYTES:
        raise ValueError("experiment diff exceeds 16 MiB")
    return value.rstrip() + "\n" if value.strip() else ""


def prepare_case(repo, item):
    """Check commit/diff consistency without manufacturing new sealed evidence."""
    preparation_path = Path(item["preparation"]).resolve(strict=True)
    invocation_path = Path(item["invocation"]).resolve(strict=True)
    preparation, preparation_hash = read_json(preparation_path)
    invocation, invocation_hash = read_json(invocation_path)
    if invocation.get("purpose") != "review":
        raise ValueError("case must select a retained Review invocation")
    data = invocation["prompt"]["untrusted_task_data"]
    commits = data["reviewed_commits"]
    base = preparation["repository"]["base_commit"]
    for value in (base, commits["before"], commits["after"]):
        if git(repo, "rev-parse", f"{value}^{{commit}}").strip() != value:
            raise ValueError("case commits must be exact available commit identities")
        git(repo, "merge-base", "--is-ancestor", base, value)
    git(repo, "merge-base", "--is-ancestor", commits["before"], commits["after"])
    latest = diff(repo, commits["before"], commits["after"])
    if latest != data["reviewed_diff"]:
        raise ValueError("retained Review diff disagrees with Git objects")
    return {
        "id": item["id"],
        "base": base,
        "head": commits["after"],
        "invocation": invocation,
        "latest_diff": latest,
        "full_diff": diff(repo, base, commits["after"]),
        "input_hashes": {
            str(preparation_path): preparation_hash,
            str(invocation_path): invocation_hash,
        },
    }


def rebind(value, old_workspace, workspace):
    """Rebind exact workspace paths in copied task data, equally in both arms."""
    if isinstance(value, dict):
        return {
            key: rebind(item, old_workspace, workspace) for key, item in value.items()
        }
    if isinstance(value, list):
        return [rebind(item, old_workspace, workspace) for item in value]
    return str(workspace) if value == old_workspace else value


def task_data(case, arm, workspace):
    invocation = case["invocation"]
    data = rebind(
        copy.deepcopy(invocation["prompt"]["untrusted_task_data"]),
        invocation["execution_root"],
        workspace,
    )
    if arm == "full":
        data["reviewed_commits"]["before"] = case["base"]
        data["reviewed_diff"] = case["full_diff"]
    elif arm != "latest":
        raise ValueError("unknown context arm")
    return data


def workspace_state(workspace):
    """Hash files, including ignored/untracked files; exclude Git administration."""
    digest = hashlib.sha256()
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if relative.parts[0] == ".git" or (path.is_dir() and not path.is_symlink()):
            continue
        digest.update(str(relative).encode() + b"\0")
        if path.is_symlink():
            digest.update(b"link:" + str(path.readlink()).encode())
        else:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        digest.update(b"\0")
    return {
        "head": git(workspace, "rev-parse", "HEAD").strip(),
        "files": digest.hexdigest(),
    }


def run_call(case, arm, repetition, workspace, directory, adapter, timeout):
    directory.mkdir()
    started = time.monotonic()
    before = workspace_state(workspace)
    data = task_data(case, arm, workspace)
    related_ids = {item["id"] for item in data["related_work"]}

    def validate(value):
        try:
            return validate_review(
                json.loads(value), workspace, case["head"], related_ids
            )
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise ResponseRejected(str(error)) from error

    record = {
        "case": case["id"],
        "arm": arm,
        "repetition": repetition,
        "before": before,
    }
    try:
        result = InferenceRuntime().invoke(
            purpose="review_context_experiment",
            task_contract_version=1,
            trusted_task_instructions=(
                case["invocation"]["prompt"]["trusted_task_instructions"]
                + "\n\n"
                + CONTEXT_NOTE
            ),
            untrusted_task_data=data,
            requested_capability=Capability.READ_ONLY,
            execution_root=workspace,
            timeout_seconds=timeout,
            evidence_directory=directory / "inference",
            validator=validate,
            adapter=adapter,
        )
        record.update(outcome=result.outcome, review=result.value)
    except Exception as error:  # noqa: BLE001 - retain failed experimental calls
        record.update(
            outcome="experiment_error", error=f"{type(error).__name__}: {error}"
        )
    record["after"] = workspace_state(workspace)
    record["workspace_unchanged"] = record["after"] == before
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    record["input_bytes"] = len(json.dumps(data, ensure_ascii=False).encode())
    write_json(directory / "result.json", record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest", type=Path, help="JSON list of id/preparation/invocation cases"
    )
    parser.add_argument("destination", type=Path, help="new external result directory")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--thinking", default="medium")
    parser.add_argument("--repetitions", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=3)
    parser.add_argument("--timeout", type=int, choices=range(1, 1801), default=1800)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="check cases and freeze manifest; no workspaces or inference",
    )
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    manifest, manifest_hash = read_json(args.manifest)
    if not isinstance(manifest, list) or not 1 <= len(manifest) <= 20:
        parser.error("manifest must contain 1 through 20 cases")
    if len({item["id"] for item in manifest}) != len(manifest):
        parser.error("case ids must be unique")
    cases = [prepare_case(repo, item) for item in manifest]
    destination = args.destination.resolve()
    protected = [repo, args.manifest.resolve()]
    for item in manifest:
        protected.extend(
            [
                Path(item["preparation"]).resolve().parent,
                Path(item["invocation"]).resolve().parent,
            ]
        )
    for source in protected:
        if (
            destination == source
            or source in destination.parents
            or destination in source.parents
        ):
            parser.error(
                "destination must be separate from repository and retained input trees"
            )
    destination.mkdir()
    metadata = {
        "kind": "review-context-experiment",
        "schema_version": 1,
        "manifest_sha256": manifest_hash,
        "runtime_revision": git(
            Path(__file__).resolve().parent.parent, "rev-parse", "HEAD"
        ).strip(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model": args.model,
        "thinking": args.thinking,
        "repetitions": args.repetitions,
        "workers": args.workers,
        "timeout_seconds": args.timeout,
        "context_note": CONTEXT_NOTE,
        "cases": [
            {key: case[key] for key in ("id", "base", "head", "input_hashes")}
            | {
                "latest_diff_bytes": len(case["latest_diff"].encode()),
                "full_diff_bytes": len(case["full_diff"].encode()),
            }
            for case in cases
        ],
    }
    write_json(destination / "experiment.json", metadata)
    if args.prepare_only:
        return 0
    (destination / "workspaces").mkdir()
    (destination / "calls").mkdir()
    jobs = []
    for repetition in range(1, args.repetitions + 1):
        for index, case in enumerate(cases):
            arms = (
                ("latest", "full") if (repetition + index) % 2 else ("full", "latest")
            )
            for arm in arms:
                slot = f"slot-{len(jobs) + 1:03d}"
                workspace = destination / "workspaces" / slot
                git(repo, "worktree", "add", "--detach", str(workspace), case["head"])
                jobs.append(
                    (case, arm, repetition, workspace, destination / "calls" / slot)
                )
    results = []
    adapter = PiAdapter(model=args.model, thinking=args.thinking)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for job in jobs:
            future = pool.submit(run_call, *job, adapter, args.timeout)
            futures[future] = job
        for future in as_completed(futures):
            record = future.result()
            results.append(record)
            print(
                f"{len(results)}/{len(jobs)} {record['case']} {record['arm']} {record['repetition']}: {record['outcome']}",
                flush=True,
            )
            write_json(
                destination / "summary.json", {"complete": False, "calls": results}
            )
    retained_unchanged = all(
        read_json(path)[1] == expected
        for case in cases
        for path, expected in case["input_hashes"].items()
    )
    success = retained_unchanged and all(
        record["outcome"] == "succeeded" and record["workspace_unchanged"]
        for record in results
    )
    write_json(
        destination / "summary.json",
        {
            "complete": True,
            "retained_inputs_unchanged": retained_unchanged,
            "calls": results,
        },
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
