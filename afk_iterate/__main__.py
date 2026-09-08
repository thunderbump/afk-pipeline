import json
import subprocess
import sys
from pathlib import Path

from afk_evidence.iteration import (
    evaluate_policy,
    validate_input,
    validate_result_location,
    validate_sealed_result,
)
from afk_runtime import progress, seal_json, write_json

__all__ = ["main", "validate_sealed_result"]

USAGE = "usage: python3 -m afk_iterate POLICY_JSON RESULT_DIRECTORY"

HELP = f"""{USAGE}

Decide bounded review-response iteration from the latest completed Finding Assessment.

Arguments:
  POLICY_JSON      Path to the iteration-policy JSON file.
  RESULT_DIRECTORY New directory where policy input and output are written.
"""


def main():
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(HELP, end="")
        return 0
    if len(sys.argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2

    input_path = Path(sys.argv[1])
    result_directory = Path(sys.argv[2])
    progress("loading iteration-policy input")
    policy_input = validate_input(json.loads(input_path.read_text()))
    progress("iteration-policy input accepted")
    progress("loading and verifying Finding Assessment evidence")
    policy, lineage, protected_directories = evaluate_policy(policy_input)
    validate_result_location(
        result_directory,
        Path(lineage.assignment["workspace"]),
        protected_directories,
    )

    progress("preparing iteration-policy result directory")
    result_directory.mkdir()
    write_json(result_directory / "input.json", policy_input)
    output = {"schema_version": 1, "outcome": "completed", "policy": policy}
    output_path = result_directory / "output.json"
    seal_json(output_path, output)
    progress(f"sealed completed iteration-policy outcome at {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"afk-iterate: {error}", file=sys.stderr)
        raise SystemExit(2)
