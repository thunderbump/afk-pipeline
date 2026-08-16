import json
import subprocess
import sys
from pathlib import Path

from afk_change.evidence import verify_source
from afk_runtime import progress, seal_json, write_json

USAGE = "usage: python3 -m afk_change SOURCE_JSON RESULT_DIRECTORY"

HELP = f"""{USAGE}

Project successful AFK evidence from committed-change source JSON into one result.

Arguments:
  SOURCE_JSON       Path to the committed-change source JSON file.
  RESULT_DIRECTORY  New directory where committed-change input and output are written.
"""


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(HELP, end="")
        return 0
    if len(sys.argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2

    input_path = Path(sys.argv[1])
    result_directory = Path(sys.argv[2])
    progress("loading committed-change input")
    change_input = json.loads(input_path.read_text())
    source = validate_input(change_input)
    progress("committed-change input accepted")
    source_directory = Path(source["directory"])
    progress(f"loading and verifying {source['kind']} evidence")
    lineage = verify_source(source["kind"], source_directory)
    assignment = lineage.assignment
    before = lineage.before
    after = lineage.after

    validate_result_location(
        result_directory, Path(assignment["workspace"]), source_directory
    )

    output = {
        "schema_version": 1,
        "outcome": "completed",
        "change": {
            "objective": assignment["objective"],
            "workspace": assignment["workspace"],
            "repository": {"before": before, "after": after},
            "source": source,
        },
    }
    progress("preparing committed-change result directory")
    result_directory.mkdir()
    write_json(result_directory / "input.json", change_input)
    output_path = result_directory / "output.json"
    seal_json(output_path, output)
    progress(f"sealed completed committed-change outcome at {output_path}")
    return 0


def validate_input(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("committed change must use schema_version 1")
    source = value.get("source")
    if not isinstance(source, dict):
        raise TypeError("committed change source must be an object")
    if source.get("kind") not in {"attempt", "feedback_response"}:
        raise ValueError("committed change source kind is invalid")
    directory = source.get("directory")
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise ValueError("committed change source directory must be an absolute path")
    return {"kind": source["kind"], "directory": directory}


def validate_result_location(result_directory, workspace, source_directory):
    result = result_directory.resolve()
    for protected, message in (
        (workspace.resolve(), "result directory must be outside the source workspace"),
        (
            source_directory.resolve(),
            "result directory must be outside the source evidence directory",
        ),
    ):
        if result == protected or protected in result.parents:
            raise ValueError(message)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"afk-change: {error}", file=sys.stderr)
        raise SystemExit(2)
