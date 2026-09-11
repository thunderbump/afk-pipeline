"""Reproduce portable metrics cases using synthetic evidence and real exporters."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import afk_export
from afk_coordinate.contract import expected_input_sources
from afk_metrics.publication import build_publication
from tests import test_export_cli

FIXTURES = Path(__file__).parent / "fixtures/metrics-publication/populated"


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def add_pi(inference, purpose, events):
    test_export_cli.ExportCliTests().add_inference_receipt(inference)
    invocation_path = inference / "invocation.json"
    invocation = json.loads(invocation_path.read_text())
    invocation.update(purpose=purpose)
    write_json(invocation_path, invocation)
    event_path = inference / "attempts/1/events.jsonl"
    event_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    receipt_path = inference / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["hashes"]["invocation_sha256"] = hashlib.sha256(
        invocation_path.read_bytes()
    ).hexdigest()
    receipt["attempts"][0]["artifacts"]["events_sha256"] = hashlib.sha256(
        event_path.read_bytes()
    ).hexdigest()
    write_json(receipt_path, receipt)


def message(identity, usage):
    return {
        "type": "message_end",
        "message": {
            "id": identity,
            "role": "assistant",
            "provider": "synthetic-provider",
            "model": "synthetic-observed",
            "usage": usage,
        },
    }


def append_response_cycle(source, *, no_action):
    """Append one valid synthetic response cycle to a sealed preparer Run."""
    coordinator = source / "coordinator"
    history = json.loads((coordinator / "state.json").read_text())["history"]
    first_iteration = coordinator / "06-iteration/output.json"
    value = json.loads(first_iteration.read_text())
    value["policy"].update(decision="continue", next_response_number=1)
    write_json(first_iteration, value)
    specifications = (
        ("response", None, "completed"),
        ("validation", "02-validation", "passed"),
        ("change", "03-change", "completed"),
        ("review", "04-review", "completed"),
        ("assessment", "05-assessment", "completed"),
        ("iteration", "06-iteration", "completed"),
    )
    for sequence, (component, template, outcome) in enumerate(specifications, 7):
        directory = f"{sequence:02d}-{component}"
        history.append(
            {
                "sequence": sequence,
                "component": component,
                "directory": directory,
                "input_from": expected_input_sources(component, history),
                "outcome": outcome,
            }
        )
        target = coordinator / directory
        if template is not None:
            shutil.copytree(coordinator / template, target)
            continue
        target.mkdir()
        write_json(target / "input.json", {"schema_version": 1})
        write_json(
            target / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "process": None if no_action else {"exit_code": 0, "signal": None},
                "agent": None if no_action else {"status": "completed"},
                "response": {
                    "summary": "No action." if no_action else "Repaired.",
                    "finding_responses": []
                    if no_action
                    else [{"finding_index": 0, "response": "Repaired."}],
                },
                "repository": {"unchanged": no_action},
            },
        )
    final_iteration = coordinator / "12-iteration/output.json"
    value = json.loads(final_iteration.read_text())
    value["policy"].update(decision="stop", completed_responses=1, max_responses=1)
    value["policy"].pop("next_response_number", None)
    write_json(final_iteration, value)
    state = {
        "schema_version": 1,
        "status": "completed",
        "next_sequence": 13,
        "next_component": None,
        "active_invocation": None,
        "history": history,
        "terminal": {"decision": "stop"},
    }
    write_json(coordinator / "state.json", state)
    write_json(
        coordinator / "output.json",
        {
            "schema_version": 1,
            "outcome": "completed",
            "decision": "stop",
            "history": history,
        },
    )
    return history


def review_variant_matrix():
    """Return portable producer-contract cases that require no live inference."""
    duplicate = {
        "lens": "behavior",
        "title": "Synthetic duplicate",
        "details": "The same authentic observation is intentionally retained twice.",
        "locations": [{"path": "README.md", "line": 1}],
        "scope_claim": {
            "kind": "current",
            "rationale": "The synthetic objective owns README.md.",
        },
    }
    return {
        "schema_version": 1,
        "cases": [
            {
                "name": "combined-default",
                "request": {},
                "effective_mode": "combined",
                "invocations": [{"lens": "combined", "findings": []}],
                "aggregate": {"findings": []},
                "assessment_started": True,
            },
            {
                "name": "split-empty-and-duplicates",
                "request": {"review_mode": "split"},
                "effective_mode": "split",
                "invocations": [
                    {"lens": "behavior", "findings": [duplicate, duplicate]},
                    {"lens": "design", "findings": []},
                    {"lens": "standards", "findings": []},
                ],
                "aggregate": {
                    "findings": [duplicate, duplicate],
                    "provenance": [
                        {
                            "finding_index": 0,
                            "lens": "behavior",
                            "source_finding_index": 0,
                        },
                        {
                            "finding_index": 1,
                            "lens": "behavior",
                            "source_finding_index": 1,
                        },
                    ],
                },
                "assessment_started": True,
            },
            {
                "name": "split-partial-failure",
                "request": {"review_mode": "split"},
                "effective_mode": "split",
                "invocations": [
                    {"lens": "behavior", "outcome": "succeeded"},
                    {"lens": "design", "outcome": "timed_out"},
                    {"lens": "standards", "outcome": "not_started"},
                ],
                "aggregate": None,
                "assessment_started": False,
            },
            {
                "name": "split-abandoned-continuation",
                "request": {"review_mode": "split"},
                "effective_mode": "split",
                "invocations": [
                    {"lens": "behavior", "outcome": "abandoned"},
                    {"lens": "design", "outcome": "not_started"},
                    {"lens": "standards", "outcome": "not_started"},
                ],
                "aggregate": None,
                "assessment_started": False,
                "continuation": {
                    "repair": "split",
                    "resume": "split",
                    "exhausted": "split",
                    "reuse_partial_invocations": False,
                },
            },
        ],
    }


def populate_split_review(source, sequence=4, *, failed=False):
    """Build real authenticated split receipts, including duplicate/empty lenses."""
    helper = test_export_cli.ExportCliTests()
    coordinator = source / "coordinator"
    directory = coordinator / f"{sequence:02d}-review"
    for path in (source / "coordinator-request.json", coordinator / "input.json"):
        value = json.loads(path.read_text())
        value["review_mode"] = "split"
        write_json(path, value)
    value = {
        "schema_version": 1,
        "workspace": str(source.parent / "workspace"),
        "change_directory": str(coordinator / f"{sequence - 1:02d}-change"),
        "validation_directory": str(coordinator / f"{sequence - 2:02d}-validation"),
        "timeout_seconds": 60,
        "review_mode": "split",
    }
    write_json(directory / "input.json", value)
    shutil.rmtree(directory / "inference", ignore_errors=True)
    workspace = source.parent / "workspace"
    if not workspace.exists():
        workspace.mkdir()
        (workspace / "README.md").write_text("Synthetic fixture.\n")
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-08-19T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-08-19T00:00:00Z",
        }
        for args in (
            ["init", "-q"],
            ["add", "README.md"],
            [
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
        ):
            subprocess.run(["git", "-C", str(workspace), *args], check=True, env=env)
    head = subprocess.check_output(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True
    ).strip()
    change_path = coordinator / f"{sequence - 1:02d}-change/output.json"
    change = json.loads(change_path.read_text())
    change["change"]["repository"] = {"after": {"head": head}}
    write_json(change_path, change)
    template = json.loads((directory / "output.json").read_text())["review"]
    records = []
    for lens in ("behavior", "design", "standards")[: 2 if failed else 3]:
        inference = directory / "reviewers" / lens / "inference"
        inference.parent.mkdir(parents=True)
        add_pi(inference, "review", [message(lens, {"input": 2, "output": 1})])
        helper.bind_split_review_lens(inference, lens)
        raw = {
            **template,
            "summary": lens,
            "findings": template["findings"] * 2 if lens == "behavior" else [],
        }
        receipt_path = inference / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        timed_out = failed and lens == "design"
        if timed_out:
            receipt["attempts"][0].pop("validation")
            receipt["attempts"][0]["protocol"] = {"status": "timed_out"}
            receipt.update(
                protocol={"status": "timed_out"},
                validation={"status": "not_run"},
                terminal_response=None,
                outcome="timed_out",
            )
        else:
            terminal = json.dumps(raw)
            response = inference / "attempts/1/response.json"
            write_json(response, terminal)
            receipt["terminal_response"] = terminal
            receipt["attempts"][0]["artifacts"]["response_sha256"] = hashlib.sha256(
                response.read_bytes()
            ).hexdigest()
        write_json(receipt_path, receipt)
        records.append(
            {
                "lens": lens,
                "outcome": receipt["outcome"],
                "process": {"returncode": 0},
                "agent": None if timed_out else {"status": "completed"},
                "review": None if timed_out else raw,
                "artifacts": {
                    "events": f"reviewers/{lens}/events.jsonl",
                    "stderr": f"reviewers/{lens}/stderr.log",
                    "inference": f"reviewers/{lens}/inference",
                },
            }
        )
    output = json.loads((directory / "output.json").read_text())
    output.update(
        review_mode="split",
        outcome="timed_out" if failed else "completed",
        agent=None if failed else {"status": "completed"},
        duration_seconds=4,
        review_invocations=records,
        finding_provenance=[]
        if failed
        else [
            {"finding_index": i, "lens": "behavior", "source_finding_index": i}
            for i in range(2)
        ],
        review=None
        if failed
        else {
            **template,
            "summary": "\n".join(
                f"{row['lens'].capitalize()}: {row['review']['summary']}"
                for row in records
            ),
            "findings": template["findings"] * 2,
        },
    )
    write_json(directory / "output.json", output)
    if not failed:
        assessment_path = coordinator / f"{sequence + 1:02d}-assessment/output.json"
        assessment = json.loads(assessment_path.read_text())
        decision = assessment["assessment"]["decisions"][0]
        assessment["assessment"]["decisions"] = [
            {**decision, "finding_index": i} for i in range(2)
        ]
        write_json(assessment_path, assessment)
        iteration_path = coordinator / f"{sequence + 2:02d}-iteration/output.json"
        iteration = json.loads(iteration_path.read_text())
        iteration["policy"]["actionable_findings"] = 2
        write_json(iteration_path, iteration)


def generate_split_cases(root, destination):
    """Export populated split fixtures through the same publication seam consumers use."""
    requests = []
    for name in ("split-completed", "split-partial", "split-continuation"):
        source = test_export_cli.ExportCliTests().sealed_preparer(root / name)
        coordinator = source / "coordinator"
        preparation = json.loads((source / "preparation.json").read_text())
        preparation["run"]["id"] = name
        history = test_export_cli.ExportCliTests().history()
        if name == "split-continuation":
            history = append_response_cycle(source, no_action=False)
        populate_split_review(source, failed=name == "split-partial")
        if name == "split-continuation":
            populate_split_review(source, 10)
            # Preserve one exhausted root and a cumulative continuation referencing
            # the same first Review receipts, without copying those invocations.
            for path in (
                source / "coordinator-request.json",
                coordinator / "input.json",
            ):
                value = json.loads(path.read_text())
                value["max_responses"] = 0
                write_json(path, value)
            path = coordinator / "06-iteration/output.json"
            value = json.loads(path.read_text())
            value["policy"].update(decision="exhausted", max_responses=0)
            value["policy"].pop("next_response_number", None)
            write_json(path, value)
            original = {
                "schema_version": 1,
                "status": "completed",
                "next_sequence": 7,
                "next_component": None,
                "active_invocation": None,
                "history": history[:6],
                "terminal": {"decision": "exhausted"},
            }
            write_json(coordinator / "state.json", original)
            write_json(
                coordinator / "output.json",
                {
                    "schema_version": 1,
                    "outcome": "completed",
                    "decision": "exhausted",
                    "history": history[:6],
                },
            )
            preparation["coordinator"]["decision"] = "exhausted"
            continuation = coordinator / "continuations/01"
            continuation.mkdir(parents=True)
            request = {
                "schema_version": 1,
                "additional_responses": 1,
                "completed_responses": 0,
                "effective_max_responses": 1,
                "prior_output": "../../output.json",
            }
            write_json(continuation / "input.json", request)
            write_json(
                continuation / "state.json",
                {
                    **original,
                    "next_sequence": 13,
                    "history": history,
                    "terminal": {"decision": "stop"},
                    "continuation": request,
                },
            )
            write_json(
                continuation / "output.json",
                {
                    "schema_version": 1,
                    "outcome": "completed",
                    "decision": "stop",
                    "history": history,
                },
            )
        if name == "split-partial":
            history = history[:4]
            history[-1]["outcome"] = "timed_out"
            terminal = {
                "failed_component": "review",
                "component_outcome": "timed_out",
                "exit_code": 1,
            }
            write_json(
                coordinator / "state.json",
                {
                    "schema_version": 1,
                    "status": "failed",
                    "next_sequence": 5,
                    "next_component": None,
                    "active_invocation": None,
                    "history": history,
                    "terminal": terminal,
                },
            )
            write_json(
                coordinator / "output.json",
                {
                    "schema_version": 1,
                    "outcome": "failed",
                    "history": history,
                    **terminal,
                },
            )
            preparation["coordinator"].update(
                status="failed", exit_code=1, outcome="failed", decision=None
            )
        write_json(source / "preparation.json", preparation)
        bundle = destination / f"bundle-{name}"
        afk_export.export_run(source, bundle, schema_version=3)
        requests.append(
            {"source": str(source), "bundle": str(bundle), "selection": "latest"}
        )
    with mock.patch("afk_metrics.publication._source_revision", return_value=None):
        publication = build_publication(
            {"schema_version": 1, "project": "operations-webui", "runs": requests}
        )
    write_json(destination / "split-publication.json", publication)


def generate_baselines(destination):
    """Reproduce the unmeasured v2/v3 intake examples using today's producer."""
    destination.mkdir()
    with tempfile.TemporaryDirectory() as temporary:
        requests = []
        for schema in (2, 3):
            source = test_export_cli.ExportCliTests().sealed_preparer(
                Path(temporary) / str(schema)
            )
            preparation = json.loads((source / "preparation.json").read_text())
            preparation["run"]["id"] = f"synthetic-run-v{schema}"
            write_json(source / "preparation.json", preparation)
            bundle = destination / f"bundle-v{schema}"
            afk_export.export_run(source, bundle, schema_version=schema)
            requests.append(
                {"source": str(source), "bundle": str(bundle), "selection": "latest"}
            )
        with mock.patch("afk_metrics.publication._source_revision", return_value=None):
            publication = build_publication(
                {"schema_version": 1, "project": "operations-webui", "runs": requests}
            )
        write_json(destination / "valid-publication.json", publication)
        publication["runs"][0]["binding"]["workflow_run_sha256"] = "0" * 64
        write_json(destination / "invalid-publication.json", publication)


def generate(destination):
    """Write only to a new directory; preserve the original upstream baselines."""
    destination.mkdir()
    write_json(destination / "review-variants.json", review_variant_matrix())
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        generate_split_cases(root, destination)
        requests = []
        for scenario in ("partial", "unavailable"):
            case = root / scenario
            source = test_export_cli.ExportCliTests().sealed_preparer(case)
            for assignment_path in (
                source / "assignment.json",
                source / "coordinator/assignment.json",
            ):
                assignment = json.loads(assignment_path.read_text())
                assignment.pop("command")
                assignment["worker"] = "inference"
                write_json(assignment_path, assignment)
            # Frozen synthetic paths make source identities independent of the
            # temporary directory. No files at these paths are accessed.
            for file in (
                source / "assignment.json",
                source / "coordinator/assignment.json",
            ):
                file.write_text(file.read_text().replace(str(case), "/synthetic"))
            preparation = source / "preparation.json"
            value = json.loads(preparation.read_text())
            value["run"]["id"] = (
                "populated-partial" if scenario == "partial" else "populated-v3"
            )
            write_json(preparation, value)
            full = {
                "input": 10,
                "output": 3,
                "cacheRead": 2,
                "cacheWrite": 1,
                "totalTokens": 16,
                "reasoning": 2,
            }
            add_pi(
                source / "coordinator/04-review/inference",
                "review",
                [
                    message("measured", {**full, "cost": {"total": 0.02}}),
                    {
                        "type": "compaction_end",
                        "id": "compact",
                        "result": {"usage": {**full, "cost": {"total": 0.01}}},
                    },
                ],
            )
            add_pi(
                source / "coordinator/05-assessment/inference",
                "finding_assessment",
                [message("zero", {**dict.fromkeys(full, 0), "cost": {"total": 0}})],
            )
            add_pi(
                source / "coordinator/01-attempt/inference",
                "attempt",
                [
                    message("partial", {"input": 4}),
                    {"type": "compaction_end", "id": "missing", "result": {}},
                ]
                if scenario == "partial"
                else [{"type": "agent_end"}],
            )
            validation = source / "coordinator/02-validation/output.json"
            value = json.loads(validation.read_text())
            if scenario == "unavailable":
                value["duration_seconds"] = 0
            write_json(validation, value)
            bundle = destination / (
                "bundle-partial" if scenario == "partial" else "bundle-v3"
            )
            afk_export.export_run(source, bundle, schema_version=3)
            if scenario == "partial":
                # Reproduce a command-worker Attempt: history proves that the
                # stage started, while no inference receipt exists. Restore the
                # measured synthetic Attempt before building the main v3 case.
                attempt_inference = source / "coordinator/01-attempt/inference"
                shutil.rmtree(attempt_inference)
                for assignment_path in (
                    source / "assignment.json",
                    source / "coordinator/assignment.json",
                ):
                    assignment = json.loads(assignment_path.read_text())
                    assignment.pop("worker")
                    assignment["command"] = ["agent", "--token", "[redacted-secret]"]
                    write_json(assignment_path, assignment)
                legacy = destination / "producer-only-v2"
                afk_export.export_run(source, legacy, schema_version=2)
                with mock.patch(
                    "afk_metrics.publication._source_revision", return_value=None
                ):
                    legacy_publication = build_publication(
                        {
                            "schema_version": 1,
                            "project": "operations-webui",
                            "runs": [
                                {
                                    "source": str(source),
                                    "bundle": str(legacy),
                                    "selection": "original",
                                }
                            ],
                        }
                    )
                write_json(destination / "producer-only-v2.json", legacy_publication)
                for assignment_path in (
                    source / "assignment.json",
                    source / "coordinator/assignment.json",
                ):
                    assignment = json.loads(assignment_path.read_text())
                    assignment.pop("command")
                    assignment["worker"] = "inference"
                    write_json(assignment_path, assignment)
                add_pi(
                    attempt_inference,
                    "attempt",
                    [
                        message("partial", {"input": 4}),
                        {"type": "compaction_end", "id": "missing", "result": {}},
                    ],
                )
            requests.append(
                {"source": str(source), "bundle": str(bundle), "selection": "original"}
            )
        source = test_export_cli.ExportCliTests().sealed_preparer(root / "abandoned")
        coordinator = source / "coordinator"
        history = test_export_cli.ExportCliTests().history()
        for sequence, outcome in ((7, "abandoned"), (8, "failed")):
            history.append(
                {
                    "sequence": sequence,
                    "component": "response",
                    "directory": f"{sequence:02d}-response",
                    "input_from": {"assessment": "05-assessment"},
                    "outcome": outcome,
                }
            )
        value = json.loads((coordinator / "06-iteration/output.json").read_text())
        value["policy"].update(decision="continue", next_response_number=1)
        write_json(coordinator / "06-iteration/output.json", value)
        (coordinator / "08-response").mkdir()
        write_json(coordinator / "08-response/input.json", {"schema_version": 1})
        write_json(
            coordinator / "08-response/output.json",
            {"schema_version": 1, "outcome": "failed"},
        )
        terminal = {
            "failed_component": "response",
            "component_outcome": "failed",
            "exit_code": 1,
        }
        write_json(
            coordinator / "state.json",
            {
                "schema_version": 1,
                "status": "failed",
                "next_sequence": 9,
                "next_component": None,
                "active_invocation": None,
                "history": history,
                "terminal": terminal,
            },
        )
        write_json(
            coordinator / "output.json",
            dict(schema_version=1, outcome="failed", **terminal, history=history),
        )
        (coordinator / "07-response/inference").mkdir(parents=True)
        value = json.loads((source / "preparation.json").read_text())
        value["run"]["id"] = "populated-abandoned"
        value["coordinator"].update(
            status="failed", exit_code=1, outcome="failed", decision=None
        )
        write_json(source / "preparation.json", value)
        bundle = destination / "bundle-abandoned"
        afk_export.export_run(source, bundle, schema_version=3)
        requests.append(
            {"source": str(source), "bundle": str(bundle), "selection": "original"}
        )
        with mock.patch("afk_metrics.publication._source_revision", return_value=None):
            publication = build_publication(
                {"schema_version": 1, "project": "operations-webui", "runs": requests}
            )
        write_json(destination / "valid-publication.json", publication)

        # These edge cases are full publication-v2 artifacts produced through
        # Export and publication intake, rather than hand-authored count claims.
        coverage_requests = []
        full_usage = {
            "input": 1,
            "output": 1,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 2,
            "reasoning": 0,
        }
        for name, no_action in (
            ("verified-no-action-response", True),
            ("shared-continuation-stage", False),
        ):
            edge = test_export_cli.ExportCliTests().sealed_preparer(root / name)
            for assignment_path in (
                edge / "assignment.json",
                edge / "coordinator/assignment.json",
            ):
                assignment = json.loads(assignment_path.read_text())
                assignment.pop("command")
                assignment["worker"] = "inference"
                write_json(assignment_path, assignment)
            preparation = edge / "preparation.json"
            value = json.loads(preparation.read_text())
            value["run"]["id"] = f"populated-{name}"
            write_json(preparation, value)
            history = append_response_cycle(edge, no_action=no_action)
            coordinator = edge / "coordinator"
            for sequence, purpose in (
                (1, "attempt"),
                (4, "review"),
                (5, "finding_assessment"),
                (10, "review"),
                (11, "finding_assessment"),
            ):
                add_pi(
                    coordinator
                    / f"{sequence:02d}-{history[sequence - 1]['component']}/inference",
                    purpose,
                    [
                        message(
                            f"{name}-{sequence}", {**full_usage, "cost": {"total": 0}}
                        )
                    ],
                )
            if not no_action:
                add_pi(
                    coordinator / "07-response/inference",
                    "feedback_response",
                    [message(f"{name}-7", {**full_usage, "cost": {"total": 0}})],
                )
                original_history = history[:6]
                original_iteration = coordinator / "06-iteration/output.json"
                value = json.loads(original_iteration.read_text())
                value["policy"].update(decision="exhausted", max_responses=0)
                value["policy"].pop("next_response_number", None)
                write_json(original_iteration, value)
                for request_path in (
                    edge / "coordinator-request.json",
                    coordinator / "input.json",
                ):
                    value = json.loads(request_path.read_text())
                    value["max_responses"] = 0
                    write_json(request_path, value)
                original_state = {
                    "schema_version": 1,
                    "status": "completed",
                    "next_sequence": 7,
                    "next_component": None,
                    "active_invocation": None,
                    "history": original_history,
                    "terminal": {"decision": "exhausted"},
                }
                write_json(coordinator / "state.json", original_state)
                write_json(
                    coordinator / "output.json",
                    {
                        "schema_version": 1,
                        "outcome": "completed",
                        "decision": "exhausted",
                        "history": original_history,
                    },
                )
                value = json.loads(preparation.read_text())
                value["coordinator"]["decision"] = "exhausted"
                write_json(preparation, value)
                continuation_input = {
                    "schema_version": 1,
                    "additional_responses": 1,
                    "completed_responses": 0,
                    "effective_max_responses": 1,
                    "prior_output": "../../output.json",
                }
                continuation = coordinator / "continuations/01"
                continuation.mkdir(parents=True)
                write_json(continuation / "input.json", continuation_input)
                write_json(
                    continuation / "state.json",
                    {
                        "schema_version": 1,
                        "status": "completed",
                        "next_sequence": 13,
                        "next_component": None,
                        "active_invocation": None,
                        "history": history,
                        "terminal": {"decision": "stop"},
                        "continuation": continuation_input,
                    },
                )
                write_json(
                    continuation / "output.json",
                    {
                        "schema_version": 1,
                        "outcome": "completed",
                        "decision": "stop",
                        "history": history,
                    },
                )
                # The continuation history retains sequence 4 by reference to
                # its one authenticated coordinator path. It must be projected
                # once for the selected continuation Run.
            bundle = destination / f"bundle-{name}"
            afk_export.export_run(edge, bundle, schema_version=3)
            coverage_requests.append(
                {"source": str(edge), "bundle": str(bundle), "selection": "latest"}
            )
        with mock.patch("afk_metrics.publication._source_revision", return_value=None):
            coverage_publication = build_publication(
                {
                    "schema_version": 1,
                    "project": "operations-webui",
                    "runs": coverage_requests,
                }
            )
        write_json(
            destination / "evidence-coverage-variants.json", coverage_publication
        )
    return publication


if __name__ == "__main__":
    generator = generate_baselines if sys.argv[1:2] == ["--baseline"] else generate
    generator(Path(sys.argv[-1]).resolve())
