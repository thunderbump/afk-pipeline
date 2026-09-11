"""Replay current complete-work Review packets with combined or independent lenses.

Run from this checkout: python3 -m experiments.split_review --help.
Results are experimental evidence, never Coordinator/Review completion records.
"""

import argparse
import copy
import hashlib
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from afk_inference import Capability, InferenceRuntime, PiAdapter, ResponseRejected
from afk_review.contract import validate_review
from afk_review.task import (
    BEHAVIOR_INSTRUCTIONS,
    DESIGN_INSTRUCTIONS,
    STANDARDS_INSTRUCTIONS,
)

LENSES = {
    "behavior": BEHAVIOR_INSTRUCTIONS,
    "design": DESIGN_INSTRUCTIONS,
    "standards": STANDARDS_INSTRUCTIONS,
}
ARMS = ("combined", *LENSES)

MAX_BYTES = 16 * 1024 * 1024
CONTEXT_NOTE = (
    "This is an experimental review, not completion authority. Inspect only the "
    "candidate execution workspace and the explicitly supplied evidence files. "
    "Other retained paths in task data are historical metadata, not additional "
    "execution roots. Do not consult other experiment calls or caller scoring. "
    "Use the supplied historical Validation results; this replay provides source "
    "and Git history but does not provision ignored dependencies or browser tools. "
    "Do not install tools or modify the candidate. State verification limitations "
    "rather than treating unavailable execution as a demonstrated defect."
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
    """Verify retained identities and every referenced complete-work artifact."""
    preparation_path = Path(item["preparation"]).resolve(strict=True)
    invocation_path = Path(item["invocation"]).resolve(strict=True)
    preparation, preparation_hash = read_json(preparation_path)
    invocation, invocation_hash = read_json(invocation_path)
    if invocation.get("purpose") != "review":
        raise ValueError("case must select a retained Review invocation")
    data = invocation["prompt"]["untrusted_task_data"]
    context = data["work_context"]
    base, head = context["work_base"], context["candidate"]
    if base != preparation["repository"]["base_commit"]:
        raise ValueError("work base differs from preparation")
    if data["reviewed_commits"] != {"before": base, "after": head}:
        raise ValueError("reviewed range differs from complete work context")
    for value in (base, head, context["repair_base"]):
        if git(repo, "rev-parse", f"{value}^{{commit}}").strip() != value:
            raise ValueError("case requires exact available commits")
        git(repo, "merge-base", "--is-ancestor", value, head)
    hashes = {
        str(preparation_path): preparation_hash,
        str(invocation_path): invocation_hash,
    }
    artifacts = {}
    for key, entry in context["files"].items():
        path = Path(entry["path"]).resolve(strict=True)
        raw = path.read_bytes()
        if (
            len(raw) != entry["bytes"]
            or hashlib.sha256(raw).hexdigest() != entry["sha256"]
        ):
            raise ValueError("retained artifact digest mismatch")
        hashes[str(path)] = entry["sha256"]
        artifacts[key] = raw
    for key, before in (("work_diff", base), ("repair_diff", context["repair_base"])):
        if artifacts[key].decode() != diff(repo, before, head):
            raise ValueError("retained diff disagrees with Git objects")
    instructions = invocation["prompt"]["trusted_task_instructions"]
    for packet in LENSES.values():
        if instructions.count(packet) != 1:
            raise ValueError(
                "retained lens instructions differ from this driver revision"
            )
    if item["input_hashes"] != hashes:
        raise ValueError("source inputs differ from frozen manifest")
    return {
        "id": item["id"],
        "repo": str(repo),
        "base": base,
        "head": head,
        "invocation": invocation,
        "artifacts": artifacts,
        "input_hashes": hashes,
    }


def instructions(case, arm):
    value = case["invocation"]["prompt"]["trusted_task_instructions"]
    if arm not in ARMS:
        raise ValueError("unknown review arm")
    if arm != "combined":
        for lens, packet in LENSES.items():
            if lens != arm:
                value = value.replace(packet, "")
        value += f"\n\nThis independent call owns only the {arm} lens. Report findings only under that lens."
    return value + "\n\n" + CONTEXT_NOTE


def rebind(value, old_workspace, workspace):
    """Rebind exact workspace paths in copied task data, equally in both arms."""
    if isinstance(value, dict):
        return {
            key: rebind(item, old_workspace, workspace) for key, item in value.items()
        }
    if isinstance(value, list):
        return [rebind(item, old_workspace, workspace) for item in value]
    return str(workspace) if value == old_workspace else value


def task_data(case, workspace, evidence):
    data = rebind(
        copy.deepcopy(case["invocation"]["prompt"]["untrusted_task_data"]),
        case["invocation"]["execution_root"],
        workspace,
    )
    for key, entry in data["work_context"]["files"].items():
        entry["path"] = str(evidence / key)
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
    evidence = directory / "packet"
    evidence.mkdir()
    for key, raw in case["artifacts"].items():
        (evidence / key).write_bytes(raw)
    data = task_data(case, workspace, evidence)
    related_ids = {item["id"] for item in data["related_work"]}

    def validate(value):
        try:
            review = validate_review(
                json.loads(value), workspace, case["head"], related_ids
            )
            if arm != "combined" and any(
                item["lens"] != arm for item in review["findings"]
            ):
                raise ValueError("split response contains another lens")
            return review
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise ResponseRejected(str(error)) from error

    record = {
        "case": case["id"],
        "arm": arm,
        "repetition": repetition,
        "before": before,
        "started_at_unix": time.time(),
    }
    try:
        result = InferenceRuntime().invoke(
            purpose="split_review_experiment",
            task_contract_version=7,
            read_only_evidence=tuple(str(evidence / key) for key in case["artifacts"]),
            trusted_task_instructions=instructions(case, arm),
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
    record["packet_unchanged"] = all(
        (evidence / key).read_bytes() == raw for key, raw in case["artifacts"].items()
    )
    record["workspace_unchanged"] = record["after"] == before
    record["finished_at_unix"] = time.time()
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    record["input_bytes"] = len(json.dumps(data, ensure_ascii=False).encode())
    write_json(directory / "result.json", record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest", type=Path, help="JSON list of id/repo/preparation/invocation cases"
    )
    parser.add_argument("destination", type=Path, help="new external result directory")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--thinking", default="medium")
    parser.add_argument("--first-repetition", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--repetitions", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=3)
    parser.add_argument("--timeout", type=int, metavar="SECONDS", default=1800)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="check cases and freeze manifest; no workspaces or inference",
    )
    args = parser.parse_args()
    if not 1 <= args.timeout <= 1800:
        parser.error("timeout must be between 1 and 1800 seconds")
    if args.first_repetition + args.repetitions > 4:
        parser.error("this pilot has only repetitions 1 through 3")
    manifest, manifest_hash = read_json(args.manifest)
    if not isinstance(manifest, list) or not 1 <= len(manifest) <= 20:
        parser.error("manifest must contain 1 through 20 cases")
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", item["id"]) for item in manifest):
        parser.error("case ids must be safe lowercase names")
    if len({item["id"] for item in manifest}) != len(manifest):
        parser.error("case ids must be unique")
    cases = [
        prepare_case(Path(item["repo"]).resolve(strict=True), item) for item in manifest
    ]
    destination = args.destination.resolve()
    protected = [Path(__file__).resolve().parent.parent, args.manifest.resolve()]
    protected.extend(Path(item["repo"]).resolve() for item in manifest)
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
        "kind": "split-review-experiment",
        "schema_version": 1,
        "manifest_sha256": manifest_hash,
        "runtime_revision": git(
            Path(__file__).resolve().parent.parent, "rev-parse", "HEAD"
        ).strip(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model": args.model,
        "thinking": args.thinking,
        "repetitions": args.repetitions,
        "first_repetition": args.first_repetition,
        "arms": ARMS,
        "instruction_hashes": {
            case["id"]: {
                arm: hashlib.sha256(instructions(case, arm).encode()).hexdigest()
                for arm in ARMS
            }
            for case in cases
        },
        "workers": args.workers,
        "timeout_seconds": args.timeout,
        "context_note": CONTEXT_NOTE,
        "cases": [
            {key: case[key] for key in ("id", "base", "head", "input_hashes")}
            for case in cases
        ],
    }
    write_json(destination / "experiment.json", metadata)
    # Freeze identical task data and all four instruction variants for inspection.
    for case in cases:
        packet = destination / case["id"]
        packet.mkdir()
        for key, raw in case["artifacts"].items():
            (packet / key).write_bytes(raw)
        write_json(
            packet / "task.json", task_data(case, Path("/EXPERIMENT_WORKSPACE"), packet)
        )
        for arm in ARMS:
            (packet / f"{arm}.txt").write_text(instructions(case, arm))
    if args.prepare_only:
        return 0
    (destination / "workspaces").mkdir()
    (destination / "calls").mkdir()
    jobs = []
    for repetition in range(
        args.first_repetition, args.first_repetition + args.repetitions
    ):
        for index, case in enumerate(cases):
            arms = ARMS if (repetition + index) % 2 else tuple(reversed(ARMS))
            for arm in arms:
                slot = f"slot-{len(jobs) + 1:03d}"
                workspace = destination / "workspaces" / slot
                workspace.mkdir()
                git(workspace, "init", "-q")
                git(workspace, "fetch", "--no-tags", case["repo"], case["head"])
                git(workspace, "checkout", "--detach", "FETCH_HEAD")
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
        hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected
        for case in cases
        for path, expected in case["input_hashes"].items()
    )
    success = retained_unchanged and all(
        record["outcome"] == "succeeded"
        and record["workspace_unchanged"]
        and record["packet_unchanged"]
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
