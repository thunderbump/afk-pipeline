"""CLI for an explicit local AFK metrics report."""

import argparse
import json
import os
import sys
from pathlib import Path

from .report import build_report


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
            lines.extend(
                [
                    f"  terminal outcome: {outcome['terminal']}",
                    f"  validation: {', '.join(str(x) for x in outcome['validation_results']) or 'unavailable'}",
                    f"  repairs / retries: {outcome['repair_count']} / {outcome['retry_count']}",
                    f"  Run wall span: {run['timing']['run_wall_span_seconds'] if run['timing']['run_wall_span_seconds'] is not None else 'unavailable'} s",
                    f"  inference invocation elapsed: {totals['elapsed_seconds']} s (includes adapter/runtime/tool work; not pure inference latency)",
                    f"  repository Validation: {run['timing']['repository_validation_seconds'] if run['timing']['repository_validation_seconds'] is not None else 'unavailable'} s",
                    "  Change / Iteration timing: unavailable / unavailable",
                    f"  usage: {json.dumps(totals['usage'], sort_keys=True) if totals['usage'] else 'unavailable'}",
                    f"  API cost: {totals['cost']['amount'] if totals['cost']['amount'] is not None else 'unavailable'} (Pi-reported estimate, not billed charges)",
                    "  completion acceptance / integration: unavailable / unavailable",
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
