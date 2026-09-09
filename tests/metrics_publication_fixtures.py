"""Reproduce portable metrics cases using synthetic evidence and real exporters."""

import hashlib
import json
import shutil
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


def generate(destination):
    """Write only to a new directory; preserve the original upstream baselines."""
    destination.mkdir()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
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
    generate(Path(sys.argv[1]).resolve())
