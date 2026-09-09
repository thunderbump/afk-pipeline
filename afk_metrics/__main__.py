"""CLI for an explicit local AFK metrics report."""

import argparse
import json
import os
import sys
from pathlib import Path

from .publication import PublicationError, publish
from .report import build_report


def _available(value):
    return value if value is not None else "unavailable"


def _human(report):
    lines = [
        "AFK retained Run metrics comparison",
        "===================================",
        "",
    ]
    for index, run in enumerate(report["runs"], 1):
        lines.append(f"Run {index}: {run['source_identity']}")
        lines.append(f"  integrity: {run['integrity']['status']}")
        if run["integrity"]["status"] == "verified":
            outcome = run["outcome"]
            totals = run["inference"]["totals"]
            evidence_coverage = run["inference"].get(
                "evidence_coverage",
                {"status": "unavailable", "expected": 0, "measured": 0},
            )
            identity = run.get("run_identity") or {}
            identity_parts = [
                f"{name}={identity[name]}"
                for name in ("run_id", "bead_id", "project")
                if identity.get(name) is not None
            ]
            model_parts = []
            for invocation in run["inference"]["invocations"]:
                observed = invocation.get("observed_identities") or [{}]
                for event_identity in observed:
                    values = []
                    if invocation.get("adapter") is not None:
                        values.append(f"adapter={invocation['adapter']}")
                    for name in ("provider", "model"):
                        value = event_identity.get(name) or invocation.get(name)
                        if value is not None:
                            values.append(f"{name}={value}")
                    label = ", ".join(values) if values else "identity unavailable"
                    if label not in model_parts:
                        model_parts.append(label)
            cost = totals["cost"]
            usage_coverage = totals.get("usage_coverage", "unavailable")
            cost_status = cost.get("status", "unavailable")
            cost_text = f"unavailable (status: {cost_status})"
            if cost["amount"] is not None:
                cost_text = (
                    f"{cost['amount']} (status: {cost_status}; "
                    "Pi-reported estimate, not billed charges)"
                )
            timing = run["timing"]
            repository_validation_coverage = timing.get(
                "repository_validation_coverage", "unavailable"
            )
            response_validator_coverage = timing.get(
                "response_validator_coverage", "unavailable"
            )
            lines.extend(
                [
                    f"  Run identity: {', '.join(identity_parts) or 'unavailable'}",
                    f"  adapter / provider / model: {'; '.join(model_parts) or 'unavailable'}",
                    f"  terminal outcome: {outcome['terminal']}",
                    f"  Coordinator decision: {_available(outcome.get('coordinator_decision'))}",
                    f"  validation: {', '.join(str(x) for x in outcome['validation_results']) or 'unavailable'}",
                    f"  repairs / retries: {outcome['repair_count']} / {outcome['retry_count']}",
                    f"  Run wall span: {_available(timing['run_wall_span_seconds'])} s",
                    f"  inference evidence: {evidence_coverage['status']} ({evidence_coverage['measured']}/{evidence_coverage['expected']} expected stages measured)",
                    f"  inference invocation elapsed: {_available(totals['elapsed_seconds'])} s (includes adapter/runtime/tool work; not pure inference latency)",
                    f"  response validation: {_available(timing.get('response_validator_seconds'))} s (coverage: {response_validator_coverage}; inference response validator, not repository testing)",
                    f"  repository Validation: {_available(timing.get('repository_validation_seconds'))} s (coverage: {repository_validation_coverage})",
                    "  Change / Iteration timing: unavailable / unavailable",
                    f"  usage ({usage_coverage} coverage): {json.dumps(totals['usage'], sort_keys=True) if totals['usage'] else 'unavailable'}",
                    f"  compaction usage (separate aggregate; {usage_coverage} coverage): {json.dumps(totals.get('compaction_usage', {}), sort_keys=True) if totals.get('compaction_usage') else 'unavailable'}",
                    f"  API cost: {cost_text}",
                    f"  completion acceptance / integration: {_available(outcome['completion_acceptance'])} / {_available(outcome['integration_status'])}",
                ]
            )
        lines.append("")
    if report["comparisons"]:
        lines.append("Comparisons")
        for item in report["comparisons"]:
            status = (
                "matched frozen conditions"
                if item["equivalent_frozen_conditions"]
                else "; ".join(item["warnings"])
            )
            lines.append(
                f"  {item['left']} vs {item['right']}: {status}; {item['ranking']}"
            )
    lines.extend(
        [
            "",
            "These observational metrics do not prove semantic quality or lower complexity.",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    legacy_report = any(
        arg == "--destination" or arg.startswith("--destination=") for arg in arguments
    )
    if arguments and arguments[0] == "publish" and not legacy_report:
        parser = argparse.ArgumentParser(
            prog="python3 -m afk_metrics publish",
            description="publish bound AFK metrics snapshots",
        )
        parser.add_argument("input_json", type=Path)
        parser.add_argument("destination_json", type=Path)
        args = parser.parse_args(arguments[1:])
        try:
            publication = publish(args.input_json, args.destination_json)
        except PublicationError as error:
            print(str(error), file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "destination": str(args.destination_json),
                    "runs": len(publication["runs"]),
                }
            )
        )
        return 0

    parser = argparse.ArgumentParser(
        description="report optional metrics from retained AFK Runs"
    )
    parser.add_argument(
        "sources", nargs="+", type=Path, help="prepared Run evidence directories"
    )
    parser.add_argument(
        "--destination",
        required=True,
        type=Path,
        help="new directory for summary.json and comparison.txt",
    )
    args = parser.parse_args(arguments)
    destination = args.destination
    destination_resolved = destination.resolve()
    for source in args.sources:
        source_resolved = source.resolve()
        if (
            destination_resolved == source_resolved
            or source_resolved in destination_resolved.parents
        ):
            print(
                "metrics destination must be separate from source Runs", file=sys.stderr
            )
            return 2
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print("metrics destination already exists", file=sys.stderr)
        return 2
    report = build_report(args.sources)
    # Reports are ordinary caller-owned output, deliberately separate from and
    # never sealed into source evidence.
    temporary = destination / ".summary.json.tmp"
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, destination / "summary.json")
    temporary = destination / ".comparison.txt.tmp"
    temporary.write_text(_human(report))
    os.replace(temporary, destination / "comparison.txt")
    print(
        json.dumps(
            {
                "schema_version": 1,
                "destination": str(destination),
                "runs": len(report["runs"]),
            }
        )
    )
    return (
        1
        if any(run["integrity"]["status"] != "verified" for run in report["runs"])
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
