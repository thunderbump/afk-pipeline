import json
import subprocess
import sys
from pathlib import Path

from afk_attempt.contract import validate_assignment
from afk_coordinate.contract import (
    COMPONENT_TOPOLOGY,
    expected_input_sources,
    validate_checkpoint,
    validate_output,
)
from afk_runtime import progress, seal_json, write_json

USAGE = "usage: python3 -m afk_coordinate RUN_JSON RUN_DIRECTORY [--abandon-active]"

HELP = f"""{USAGE}

Create or resume one synchronous AFK run from frozen JSON input.

Arguments:
  RUN_JSON         Structured coordinator input.
  RUN_DIRECTORY    New or existing coordinator run directory.
  --abandon-active Assert that an unsealed active worker is gone and retry it.
"""


def main():
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(HELP, end="")
        return 0
    abandon_active = len(sys.argv) == 4 and sys.argv[3] == "--abandon-active"
    if len(sys.argv) not in {3, 4} or (len(sys.argv) == 4 and not abandon_active):
        print(USAGE, file=sys.stderr)
        return 2

    request_path = Path(sys.argv[1])
    run_directory = Path(sys.argv[2])
    progress("loading coordinator input")
    request = validate_request(read_json(request_path))
    if abandon_active and not run_directory.exists():
        raise ValueError("there is no active invocation to abandon")
    if run_directory.exists():
        assignment = validate_assignment(read_json(run_directory / "assignment.json"))
    else:
        assignment = validate_assignment(read_json(Path(request["assignment_path"])))
    validate_run_location(run_directory, Path(assignment["workspace"]))
    if run_directory.exists():
        progress("loading existing coordinator checkpoint")
        state = load_checkpoint(run_directory, request, assignment)
        if abandon_active and state["active_invocation"] is None:
            raise ValueError("there is no active invocation to abandon")
        if state["status"] != "running":
            return finalize(run_directory, state)
    else:
        progress("preparing coordinator run directory")
        run_directory.mkdir()
        write_json(run_directory / "input.json", request)
        write_json(run_directory / "assignment.json", assignment)
        state = {
            "schema_version": 1,
            "status": "running",
            "next_sequence": 1,
            "next_component": "attempt",
            "active_invocation": None,
            "history": [],
            "terminal": None,
        }
        seal_json(run_directory / "state.json", state)

    while state["status"] == "running":
        active = state["active_invocation"]
        if active is None:
            component = state["next_component"]
            sequence = state["next_sequence"]
            directory_name = f"{sequence:02d}-{component}"
            component_input = COMPONENTS[component]["build_input"](
                request, assignment, state, run_directory
            )
            input_from = expected_input_sources(component, state["history"])
            input_path = run_directory / "active-input.json"
            if component == "attempt":
                input_path = run_directory / "assignment.json"
            else:
                write_json(input_path, component_input)
            state["active_invocation"] = {
                "sequence": sequence,
                "component": component,
                "directory": directory_name,
                "input_from": input_from,
            }
            state["next_sequence"] += 1
            seal_json(run_directory / "state.json", state)

            progress(f"starting {component} invocation {directory_name}")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    COMPONENTS[component]["module"],
                    str(input_path),
                    str(run_directory / directory_name),
                ],
                check=False,
            )
            exit_code = completed.returncode
        else:
            component = active["component"]
            sequence = active["sequence"]
            directory_name = active["directory"]
            input_path = run_directory / "active-input.json"
            exit_code = None
            progress(f"reconciling active {component} invocation {directory_name}")
        result_directory = run_directory / directory_name
        output_path = result_directory / "output.json"
        if not output_path.is_file():
            if active is not None and abandon_active:
                state["history"].append({**active, "outcome": "abandoned"})
                state["active_invocation"] = None
                state["next_component"] = component
                (run_directory / "active-input.json").unlink(missing_ok=True)
                seal_json(run_directory / "state.json", state)
                progress(f"abandoned {component} invocation {directory_name}")
                abandon_active = False
                continue
            progress(f"active {component} invocation requires reconciliation")
            return 1
        output = read_json(output_path)
        outcome = validate_component_output(component, output)
        abandon_active = False
        state["history"].append({**state["active_invocation"], "outcome": outcome})
        state["active_invocation"] = None
        if component != "attempt":
            input_path.unlink(missing_ok=True)
        if outcome != COMPONENTS[component]["success"]:
            return seal_failure(
                run_directory,
                state,
                component,
                outcome,
                exit_code,
            )
        advance(state, output)
        seal_json(run_directory / "state.json", state)
        progress(f"consumed sealed {component} outcome from {directory_name}")

    return finalize(run_directory, state)


def validate_request(value):
    expected = {
        "schema_version",
        "assignment_path",
        "validation",
        "agent_timeout_seconds",
        "max_responses",
    }
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("coordinator input must use schema_version 1")
    if set(value) != expected:
        raise ValueError("coordinator input has unexpected fields")
    assignment_path = value["assignment_path"]
    if not isinstance(assignment_path, str) or not Path(assignment_path).is_absolute():
        raise ValueError("assignment_path must be an absolute path")
    validation = value["validation"]
    if not isinstance(validation, dict) or set(validation) != {
        "command",
        "timeout_seconds",
    }:
        raise ValueError("validation must contain command and timeout_seconds")
    command = validation["command"]
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(argument, str) and argument for argument in command)
    ):
        raise ValueError("validation command must be a nonempty argv array")
    positive_integer(value["validation"]["timeout_seconds"], "validation timeout")
    positive_integer(value["agent_timeout_seconds"], "agent timeout")
    limit = value["max_responses"]
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("max_responses must be a nonnegative integer")
    return value


def load_checkpoint(run_directory, request, assignment):
    if read_json(run_directory / "input.json") != request:
        raise ValueError("resume input does not match the accepted coordinator input")
    if read_json(run_directory / "assignment.json") != assignment:
        raise ValueError("resume Assignment does not match the frozen Assignment")
    state = read_json(run_directory / "state.json")
    return validate_checkpoint(state)


def positive_integer(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def validate_run_location(run_directory, workspace):
    result = run_directory.resolve()
    source = workspace.resolve()
    if result == source or source in result.parents:
        raise ValueError("run directory must be outside the source workspace")


def attempt_input(_request, assignment, _state, _run_directory):
    return assignment


def validation_input(request, assignment, state, _run_directory):
    return {
        "schema_version": 1,
        "workspace": assignment["workspace"],
        **request["validation"],
    }


def change_input(_request, _assignment, state, run_directory):
    source = latest(state, "attempt", "response")
    kind = "attempt" if source["component"] == "attempt" else "feedback_response"
    return {
        "schema_version": 1,
        "source": {
            "kind": kind,
            "directory": str((run_directory / source["directory"]).resolve()),
        },
    }


def review_input(request, assignment, state, run_directory):
    change = latest(state, "change")["directory"]
    validation = latest(state, "validation")["directory"]
    return stage_input(
        assignment["workspace"],
        request["agent_timeout_seconds"],
        change_directory=str((run_directory / change).resolve()),
        validation_directory=str((run_directory / validation).resolve()),
    )


def assessment_input(request, assignment, state, run_directory):
    review = latest(state, "review")["directory"]
    return stage_input(
        assignment["workspace"],
        request["agent_timeout_seconds"],
        review_directory=str((run_directory / review).resolve()),
    )


def iteration_input(request, _assignment, state, run_directory):
    assessment = latest(state, "assessment")["directory"]
    return {
        "schema_version": 1,
        "assessment_directory": str((run_directory / assessment).resolve()),
        "max_responses": request["max_responses"],
    }


def response_input(request, assignment, state, run_directory):
    assessment = latest(state, "assessment")["directory"]
    return stage_input(
        assignment["workspace"],
        request["agent_timeout_seconds"],
        assessment_directory=str((run_directory / assessment).resolve()),
    )


def stage_input(workspace, timeout, **evidence):
    return {
        "schema_version": 1,
        "workspace": workspace,
        **evidence,
        "timeout_seconds": timeout,
    }


def latest(state, *components):
    return next(
        item
        for item in reversed(state["history"])
        if item["component"] in components
        and item["outcome"] == COMPONENTS[item["component"]]["success"]
    )


def validate_component_output(component, output):
    specification = COMPONENTS[component]
    if (
        not isinstance(output, dict)
        or output.get("schema_version") != 1
        or output.get("outcome") not in specification["outcomes"]
    ):
        raise ValueError(f"invalid {component} output")
    if component == "iteration" and output["outcome"] == specification["success"]:
        policy = output.get("policy")
        if not isinstance(policy, dict) or policy.get("decision") not in {
            "stop",
            "exhausted",
            "continue",
        }:
            raise ValueError("invalid iteration output")
    return output["outcome"]


def advance(state, output):
    component = state["history"][-1]["component"]
    successor = COMPONENTS[component]["next"]
    if component == "iteration":
        decision = output["policy"]["decision"]
        if decision == "continue":
            successor = "response"
        else:
            state["status"] = "completed"
            state["next_component"] = None
            state["terminal"] = {"decision": decision}
            return
    state["next_component"] = successor


COMPONENT_RUNTIME = {
    "attempt": {
        "module": "afk_attempt",
        "build_input": attempt_input,
    },
    "validation": {
        "module": "afk_validate",
        "build_input": validation_input,
    },
    "change": {
        "module": "afk_change",
        "build_input": change_input,
    },
    "review": {
        "module": "afk_review",
        "build_input": review_input,
    },
    "assessment": {
        "module": "afk_assess",
        "build_input": assessment_input,
    },
    "iteration": {
        "module": "afk_iterate",
        "build_input": iteration_input,
    },
    "response": {
        "module": "afk_respond",
        "build_input": response_input,
    },
}


COMPONENTS = {
    name: {**COMPONENT_TOPOLOGY[name], **runtime}
    for name, runtime in COMPONENT_RUNTIME.items()
}


def seal_failure(
    run_directory,
    state,
    component,
    outcome,
    exit_code,
):
    state["status"] = "failed"
    state["next_component"] = None
    state["terminal"] = {
        "failed_component": component,
        "component_outcome": outcome,
        "exit_code": exit_code,
    }
    seal_json(run_directory / "state.json", state)
    return finalize(run_directory, state)


def finalize(run_directory, state):
    if state["status"] == "completed":
        output = {
            "schema_version": 1,
            "outcome": "completed",
            **state["terminal"],
            "history": state["history"],
        }
        exit_code = 0
    elif state["status"] == "failed":
        output = {
            "schema_version": 1,
            "outcome": "failed",
            **state["terminal"],
            "history": state["history"],
        }
        exit_code = 1
    else:
        raise ValueError("cannot finalize a running coordinator checkpoint")

    validate_output(output)
    output_path = run_directory / "output.json"
    if output_path.exists():
        if read_json(output_path) != output:
            raise ValueError("terminal output does not match coordinator checkpoint")
    else:
        seal_json(output_path, output)
    progress(f"sealed {state['status']} coordinator outcome at {output_path}")
    return exit_code


def read_json(path):
    return json.loads(path.read_text())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"afk-coordinate: {error}", file=sys.stderr)
        raise SystemExit(2)
