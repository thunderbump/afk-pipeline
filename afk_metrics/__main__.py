"""CLI for an explicit local AFK metrics report."""

import argparse
import json
import os
import sys
from pathlib import Path

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
            cost_text = "unavailable"
            if cost["amount"] is not None:
                cost_text = (
                    f"{cost['amount']} (Pi-reported estimate, not billed charges)"
                )
            lines.extend(
                [
                    f"  Run identity: {', '.join(identity_parts) or 'unavailable'}",
                    f"  adapter / provider / model: {'; '.join(model_parts) or 'unavailable'}",
                    f"  terminal outcome: {outcome['terminal']}",
                    f"  validation: {', '.join(str(x) for x in outcome['validation_results']) or 'unavailable'}",
                    f"  repairs / retries: {outcome['repair_count']} / {outcome['retry_count']}",
                    f"  Run wall span: {_available(run['timing']['run_wall_span_seconds'])} s",
                    f"  inference invocation elapsed: {_available(totals['elapsed_seconds'])} s (includes adapter/runtime/tool work; not pure inference latency)",
                    f"  repository Validation: {_available(run['timing']['repository_validation_seconds'])} s",
                    "  Change / Iteration timing: unavailable / unavailable",
                    f"  usage: {json.dumps(totals['usage'], sort_keys=True) if totals['usage'] else 'unavailable'}",
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
    args = parser.parse_args(argv)
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
