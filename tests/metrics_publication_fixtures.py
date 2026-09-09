"""Reproduce portable metrics cases using synthetic evidence and real exporters."""

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

import afk_export
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


def generate(destination):
    """Write only to a new directory; preserve the original upstream baselines."""
    destination.mkdir()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        requests = []
        for schema in (2, 3):
            case = root / str(schema)
            source = test_export_cli.ExportCliTests().sealed_preparer(case)
            # Frozen synthetic paths make source identities independent of the
            # temporary directory. No files at these paths are accessed.
            for file in (
                source / "assignment.json",
                source / "coordinator/assignment.json",
            ):
                file.write_text(file.read_text().replace(str(case), "/synthetic"))
            preparation = source / "preparation.json"
            value = json.loads(preparation.read_text())
            value["run"]["id"] = f"populated-v{schema}"
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
                ],
            )
            (source / "planner").mkdir()
            add_pi(
                source / "planner/inference",
                "acceptance_planning",
                [{"type": "agent_end"}],
            )
            validation = source / "coordinator/02-validation/output.json"
            value = json.loads(validation.read_text())
            if schema == 3:
                value["duration_seconds"] = 0
            write_json(validation, value)
            bundle = destination / f"bundle-v{schema}"
            afk_export.export_run(source, bundle, schema_version=schema)
            requests.append(
                {"source": str(source), "bundle": str(bundle), "selection": "original"}
            )
        source = test_export_cli.ExportCliTests().sealed_preparer(root / "abandoned")
        coordinator = source / "coordinator"
        history = test_export_cli.ExportCliTests().history()[:2]
        history[1]["outcome"] = "failed"
        history.append(
            {
                "sequence": 3,
                "component": "response",
                "directory": "03-response",
                "input_from": {"validation": "02-validation"},
                "outcome": "abandoned",
            }
        )
        terminal = {
            "failed_component": "validation",
            "component_outcome": "failed",
            "exit_code": 1,
        }
        write_json(
            coordinator / "state.json",
            {
                "schema_version": 1,
                "status": "failed",
                "next_sequence": 4,
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
        value = json.loads((coordinator / "02-validation/output.json").read_text())
        value["outcome"] = "failed"
        write_json(coordinator / "02-validation/output.json", value)
        (coordinator / "03-response/inference").mkdir(parents=True)
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
    return publication


if __name__ == "__main__":
    generate(Path(sys.argv[1]).resolve())
