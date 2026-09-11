"""Replay retained reviewer findings through the unchanged Assessment contract.

Preparation is the default; --run explicitly enables the bounded live batch.
This driver creates experimental evidence, never pipeline completion records.
"""

import argparse
import copy
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from afk_assess.contract import validate_assessment
from afk_assess.task import ASSESSMENT_INSTRUCTIONS
from afk_inference import Capability, InferenceRuntime, PiAdapter, ResponseRejected
from afk_review.contract import REVIEW_AUDIT, validate_review
from experiments.split_review import (
    CONTEXT_NOTE,
    diff,
    git,
    read_json,
    rebind,
    workspace_state,
    write_json,
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(item):
    """Bind untouched findings to the original candidate and Assessment inputs."""
    for path, expected in item["hashes"].items():
        if digest(path) != expected:
            raise ValueError("frozen input changed")
    invocation, _ = read_json(item["invocation"])
    if (
        invocation["purpose"] != "finding_assessment"
        or invocation["task_contract_version"] != 5
    ):
        raise ValueError("unexpected Assessment task contract")
    if invocation["prompt"]["trusted_task_instructions"] != ASSESSMENT_INSTRUCTIONS:
        raise ValueError(
            "retained Assessment instructions differ from current contract"
        )
    data = copy.deepcopy(invocation["prompt"]["untrusted_task_data"])
    repo = Path(item["repo"]).resolve(strict=True)
    change = data["committed_change"]["change"]["repository"]
    head = change["after"]["head"]
    if (
        head != item["head"]
        or git(repo, "rev-parse", f"{head}^{{commit}}").strip() != head
    ):
        raise ValueError("candidate identity mismatch")
    if data["reviewed_diff"] != diff(repo, change["before"]["head"], head):
        raise ValueError("retained Assessment diff disagrees with Git")
    findings = []
    summaries = []
    observations = []
    for source in item["reviews"]:
        record, _ = read_json(source)
        if (
            record["outcome"] != "succeeded"
            or record["before"]["head"] != head
            or record["after"]["head"] != head
        ):
            raise ValueError("review source is not a successful matching candidate")
        if not record["workspace_unchanged"] or not record["packet_unchanged"]:
            raise ValueError("review source was mutated")
        ids = {record["id"] for record in data["related_work"]}
        review = validate_review(record["review"], repo, head, ids)
        summaries.append(f"{record['arm']}: {review['summary']}")
        for index, finding in enumerate(review["findings"]):
            observations.append(
                {
                    "finding_index": len(findings),
                    "source": source,
                    "source_finding_index": index,
                    "reviewer": record["arm"],
                }
            )
            findings.append(copy.deepcopy(finding))
    if not findings:
        raise ValueError("empty finding sets do not need live replay")
    review = {
        "summary": "\n".join(summaries),
        "findings": findings,
        "audit": REVIEW_AUDIT,
    }
    data["review"] = review
    data["findings"] = findings
    artifacts = {}
    for name, entry in data.get("previous_cycle", {}).items():
        raw = Path(entry["path"]).read_bytes()
        if (
            len(raw) != entry["bytes"]
            or hashlib.sha256(raw).hexdigest() != entry["sha256"]
        ):
            raise ValueError("previous-cycle artifact mismatch")
        artifacts[name] = raw
    return {
        "id": item["id"],
        "repo": repo,
        "head": head,
        "invocation": invocation,
        "data": data,
        "artifacts": artifacts,
        "observations": observations,
    }


def materialize(case, directory, workspace):
    """Copy permitted evidence and rebind paths without adding caller judgments."""
    packet = directory / "packet"
    packet.mkdir()
    data = rebind(
        copy.deepcopy(case["data"]), case["invocation"]["execution_root"], workspace
    )
    for name, raw in case["artifacts"].items():
        (packet / name).write_bytes(raw)
        data["previous_cycle"][name]["path"] = str(packet / name)
    write_json(directory / "task.json", data)
    write_json(directory / "observations.json", case["observations"])
    return data, packet


def run_call(case, directory, workspace, adapter, timeout):
    directory.mkdir()
    data, packet = materialize(case, directory, workspace)
    before = workspace_state(workspace)
    started = time.monotonic()
    record = {"case": case["id"], "before": before, "started_at_unix": time.time()}
    ids = {item["id"] for item in data["related_work"]}

    def validate(text):
        try:
            return validate_assessment(data["review"], json.loads(text), ids)
        except (ValueError, TypeError) as error:
            raise ResponseRejected(str(error)) from error

    try:
        result = InferenceRuntime().invoke(
            purpose="assessment_replay",
            task_contract_version=5,
            trusted_task_instructions=ASSESSMENT_INSTRUCTIONS + "\n\n" + CONTEXT_NOTE,
            untrusted_task_data=data,
            requested_capability=Capability.READ_ONLY,
            execution_root=workspace,
            timeout_seconds=timeout,
            evidence_directory=directory / "inference",
            validator=validate,
            adapter=adapter,
            read_only_evidence=tuple(str(packet / name) for name in case["artifacts"]),
        )
        record.update(outcome=result.outcome, assessment=result.value)
    except Exception as error:  # noqa: BLE001 - retain failed experiment evidence
        record.update(
            outcome="experiment_error", error=f"{type(error).__name__}: {error}"
        )
    record["after"] = workspace_state(workspace)
    record["workspace_unchanged"] = before == record["after"]
    record["packet_unchanged"] = all(
        (packet / name).read_bytes() == raw for name, raw in case["artifacts"].items()
    )
    record["finished_at_unix"] = time.time()
    record["elapsed_seconds"] = time.monotonic() - started
    write_json(directory / "result.json", record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    manifest, manifest_hash = read_json(args.manifest)
    if not isinstance(manifest, list) or not 1 <= len(manifest) <= 12:
        parser.error("expected 1 through 12 frozen finding sets")
    if len({item["id"] for item in manifest}) != len(manifest) or any(
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", item["id"]) for item in manifest
    ):
        parser.error("case IDs must be unique safe names")
    cases = [prepare(item) for item in manifest]
    destination = args.destination.resolve()
    protected = [Path(__file__).resolve().parent.parent, args.manifest.resolve()]
    for item in manifest:
        protected.extend(
            [Path(item["repo"]).resolve(), Path(item["invocation"]).resolve().parent]
        )
        protected.extend(Path(path).resolve().parent for path in item["hashes"])
    if any(
        destination == p or p in destination.parents or destination in p.parents
        for p in protected
    ):
        parser.error(
            "destination must be separate from retained inputs and repositories"
        )
    destination.mkdir()
    write_json(
        destination / "experiment.json",
        {
            "kind": "assessment-replay",
            "manifest_sha256": manifest_hash,
            "runtime_revision": git(
                Path(__file__).resolve().parent.parent, "rev-parse", "HEAD"
            ).strip(),
            "driver_sha256": digest(__file__),
            "model": "gpt-5.6-sol",
            "thinking": "medium",
            "timeout_seconds": 1800,
            "workers": 3,
            "live": args.run,
            "instructions": ASSESSMENT_INSTRUCTIONS + "\n\n" + CONTEXT_NOTE,
            "cases": [
                {
                    "id": c["id"],
                    "head": c["head"],
                    "findings": len(c["data"]["findings"]),
                }
                for c in cases
            ],
        },
    )
    if not args.run:
        for case in cases:
            directory = destination / case["id"]
            directory.mkdir()
            materialize(case, directory, Path("/EXPERIMENT_WORKSPACE"))
        return 0
    (destination / "workspaces").mkdir()
    (destination / "calls").mkdir()
    jobs = []
    for case in cases:
        workspace = destination / "workspaces" / case["id"]
        workspace.mkdir()
        git(workspace, "init", "-q")
        git(workspace, "fetch", "--no-tags", str(case["repo"]), case["head"])
        git(workspace, "checkout", "--detach", "FETCH_HEAD")
        jobs.append((case, destination / "calls" / case["id"], workspace))
    adapter = PiAdapter(model="gpt-5.6-sol", thinking="medium")
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run_call, *job, adapter, 1800) for job in jobs]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            write_json(
                destination / "summary.json", {"complete": False, "calls": results}
            )
            print(
                f"{len(results)}/{len(jobs)} {result['case']}: {result['outcome']}",
                flush=True,
            )
    unchanged = all(
        digest(path) == expected
        for item in manifest
        for path, expected in item["hashes"].items()
    )
    write_json(
        destination / "summary.json",
        {"complete": True, "retained_inputs_unchanged": unchanged, "calls": results},
    )
    return (
        0
        if unchanged
        and all(
            r["outcome"] == "succeeded"
            and r["workspace_unchanged"]
            and r["packet_unchanged"]
            for r in results
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
