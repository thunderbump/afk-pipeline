import json
import subprocess
import sys
from pathlib import Path

from afk_assess.contract import subject_state
from afk_attempt.contract import validate_assignment
from afk_coordinate.contract import (
    COMPONENT_TOPOLOGY,
    expected_input_sources,
    validate_checkpoint,
    validate_component_output,
    validate_continuation,
    validate_output,
    validate_request,
)
from afk_iterate.__main__ import validate_sealed_result
from afk_runtime import progress, repository_state, seal_json, write_json
from afk_validate.evidence import validate_repairable_failure

USAGE = (
    "usage: python3 -m afk_coordinate RUN_JSON RUN_DIRECTORY "
    "[--abandon-active | --continue-exhausted ADDITIONAL_RESPONSES "
    "[--abandon-active]]"
)

HELP = f"""{USAGE}

Create or resume one synchronous AFK run from frozen JSON input.

Arguments:
  RUN_JSON         Structured coordinator input.
  RUN_DIRECTORY    New or existing coordinator run directory.
  --abandon-active Assert that an unsealed active worker is gone and retry it.
  --continue-exhausted ADDITIONAL_RESPONSES
                   Add a positive response allowance to an exhausted Run.
"""


def main():
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(HELP, end="")
        return 0
    if len(sys.argv) < 3:
        print(USAGE, file=sys.stderr)
        return 2
    options = sys.argv[3:]
    continuing = len(options) in {2, 3} and options[0] == "--continue-exhausted"
    abandon_active = options == ["--abandon-active"] or (
        continuing and len(options) == 3 and options[2] == "--abandon-active"
    )
    if not (
        not options
        or options == ["--abandon-active"]
        or continuing
        and (len(options) == 2 or abandon_active)
    ):
        print(USAGE, file=sys.stderr)
        return 2
    additional_responses = None
    if continuing:
        try:
            additional_responses = int(options[1])
        except ValueError:
            raise ValueError(
                "ADDITIONAL_RESPONSES must be a positive integer"
            ) from None
        if additional_responses <= 0:
            raise ValueError("ADDITIONAL_RESPONSES must be a positive integer")

    request_path = Path(sys.argv[1])
    run_directory = Path(sys.argv[2])
    progress("loading coordinator input")
    request = validate_request(read_json(request_path))
    if continuing and not run_directory.exists():
        raise ValueError("continuation requires an existing Coordinator Run")
    if abandon_active and not run_directory.exists():
        raise ValueError("there is no active invocation to abandon")
    if run_directory.exists():
        assignment = validate_assignment(read_json(run_directory / "assignment.json"))
    else:
        assignment = validate_assignment(read_json(Path(request["assignment_path"])))
    validate_run_location(run_directory, Path(assignment["workspace"]))
    state_path = run_directory / "state.json"
    terminal_output_path = run_directory / "output.json"
    if run_directory.exists():
        progress("loading existing coordinator checkpoint")
        state = load_checkpoint(run_directory, request, assignment)
        if continuing:
            request, state, state_path, terminal_output_path = start_continuation(
                run_directory,
                request,
                state,
                additional_responses,
                abandon_active,
            )
        if abandon_active and state["active_invocation"] is None:
            raise ValueError("there is no active invocation to abandon")
        if state["status"] != "running":
            return finalize(run_directory, state, terminal_output_path)
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
        seal_json(state_path, state)

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
            seal_json(state_path, state)

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
        component_output_path = result_directory / "output.json"
        if not component_output_path.is_file():
            if active is not None and abandon_active:
                state["history"].append({**active, "outcome": "abandoned"})
                state["active_invocation"] = None
                state["next_component"] = component
                (run_directory / "active-input.json").unlink(missing_ok=True)
                seal_json(state_path, state)
                progress(f"abandoned {component} invocation {directory_name}")
                abandon_active = False
                continue
            progress(f"active {component} invocation requires reconciliation")
            return 1
        output = read_json(component_output_path)
        outcome = validate_component_output(component, output)
        abandon_active = False
        state["history"].append({**state["active_invocation"], "outcome": outcome})
        state["active_invocation"] = None
        if component != "attempt":
            input_path.unlink(missing_ok=True)
        if outcome != COMPONENTS[component]["success"]:
            if (
                component == "validation"
                and outcome == "failed"
                and response_allowance_available(request, state)
                and repairable_validation(
                    result_directory, Path(assignment["workspace"])
                )
            ):
                state["next_component"] = "response"
                seal_json(state_path, state)
                progress(
                    "routing ordinary failed Validation to bounded Feedback Response"
                )
                continue
            return seal_failure(
                run_directory,
                state,
                component,
                outcome,
                exit_code,
                state_path,
                terminal_output_path,
            )
        advance(state, output)
        seal_json(state_path, state)
        progress(f"consumed sealed {component} outcome from {directory_name}")

    return finalize(run_directory, state, terminal_output_path)


def start_continuation(
    run_directory,
    request,
    state,
    additional_responses,
    abandon_active=False,
):
    """Create one explicit continuation without changing the original terminal."""
    validate_terminal_pair(state, run_directory / "output.json")
    continuation_root = run_directory / "continuations"
    continuation_directories = existing_continuations(continuation_root)
    prior_output = "../../output.json"
    expected_max_responses = request["max_responses"]
    for index, continuation_directory in enumerate(continuation_directories):
        require_exhausted(
            run_directory,
            state,
            expected_max_responses,
            check_workspace=False,
        )
        continuation_input = validate_continuation(
            read_json(continuation_directory / "input.json")
        )
        continuation_state = validate_checkpoint(
            read_json(continuation_directory / "state.json")
        )
        validate_continuation_link(
            state,
            continuation_state,
            continuation_input,
            prior_output,
        )
        if continuation_state["status"] == "running":
            if index != len(continuation_directories) - 1:
                raise ValueError("continuation lineage has work after a running entry")
            if continuation_input["additional_responses"] != additional_responses:
                raise ValueError(
                    "active continuation ADDITIONAL_RESPONSES does not match"
                )
            if (continuation_directory / "output.json").exists():
                raise ValueError("running continuation has terminal output")
            return continuation_runtime(
                request,
                continuation_state,
                continuation_directory,
                continuation_input,
            )
        validate_terminal_pair(
            continuation_state, continuation_directory / "output.json"
        )
        state = continuation_state
        expected_max_responses = continuation_input["effective_max_responses"]
        prior_output = f"../{continuation_directory.name}/output.json"

    if abandon_active:
        raise ValueError("there is no active invocation to abandon")
    require_exhausted(run_directory, state, expected_max_responses)
    completed_responses = sum(
        record["component"] == "response" and record["outcome"] == "completed"
        for record in state["history"]
    )
    continuation_root.mkdir(exist_ok=True)
    continuation_directory = (
        continuation_root / f"{len(continuation_directories) + 1:02d}"
    )
    continuation_directory.mkdir()
    continuation_input = {
        "schema_version": 1,
        "additional_responses": additional_responses,
        "completed_responses": completed_responses,
        "effective_max_responses": completed_responses + additional_responses,
        "prior_output": prior_output,
    }
    write_json(continuation_directory / "input.json", continuation_input)
    continued_state = {
        **state,
        "status": "running",
        "next_component": "response",
        "terminal": None,
        "continuation": continuation_input,
    }
    validate_checkpoint(continued_state)
    state_path = continuation_directory / "state.json"
    seal_json(state_path, continued_state)
    return continuation_runtime(
        request, continued_state, continuation_directory, continuation_input
    )


def continuation_runtime(request, state, directory, continuation_input):
    continued_request = {
        **request,
        "max_responses": continuation_input["effective_max_responses"],
    }
    return (
        continued_request,
        state,
        directory / "state.json",
        directory / "output.json",
    )


def existing_continuations(root):
    if not root.exists():
        return []
    if not root.is_dir() or root.is_symlink():
        raise ValueError("continuations must be a real directory")
    directories = sorted(root.iterdir())
    expected = [f"{index:02d}" for index in range(1, len(directories) + 1)]
    if any(
        path.name != name or not path.is_dir() or path.is_symlink()
        for path, name in zip(directories, expected, strict=True)
    ):
        raise ValueError("continuation directories are malformed")
    return directories


def validate_continuation_link(
    prior_state,
    continuation_state,
    continuation_input,
    expected_prior_output,
):
    prior_history = prior_state["history"]
    completed_responses = sum(
        record["component"] == "response" and record["outcome"] == "completed"
        for record in prior_history
    )
    if (
        continuation_input["prior_output"] != expected_prior_output
        or continuation_input["completed_responses"] != completed_responses
        or continuation_state.get("continuation") != continuation_input
        or continuation_state["history"][: len(prior_history)] != prior_history
        or len(continuation_state["history"]) < len(prior_history)
        or continuation_state["next_sequence"] < prior_state["next_sequence"]
    ):
        raise ValueError("continuation lineage does not match its predecessor")


def validate_terminal_pair(state, output_path):
    output = validate_output(read_json(output_path))
    if output != output_for(state):
        raise ValueError("terminal output does not match coordinator checkpoint")


def require_exhausted(
    run_directory,
    state,
    expected_max_responses,
    check_workspace=True,
):
    if state["status"] != "completed" or state["terminal"] != {"decision": "exhausted"}:
        raise ValueError("only an exhausted Coordinator Run can be continued")
    iteration = latest(state, "iteration")
    iteration_directory = run_directory / iteration["directory"]
    iteration_input, policy, lineage = validate_sealed_result(
        read_json(iteration_directory / "input.json"),
        read_json(iteration_directory / "output.json"),
    )
    if (
        policy["max_responses"] != expected_max_responses
        or policy["decision"] != "exhausted"
    ):
        raise ValueError("exhausted continuation requires matching Iteration evidence")
    if check_workspace:
        assessment_output = read_json(
            Path(iteration_input["assessment_directory"]) / "output.json"
        )
        assessed_state = subject_state(assessment_output["repository"]["after"])
        if (
            subject_state(repository_state(Path(lineage.assignment["workspace"])))
            != assessed_state
        ):
            raise ValueError("workspace must match the assessed repository state")


def load_checkpoint(run_directory, request, assignment):
    if read_json(run_directory / "input.json") != request:
        raise ValueError("resume input does not match the accepted coordinator input")
    if read_json(run_directory / "assignment.json") != assignment:
        raise ValueError("resume Assignment does not match the frozen Assignment")
    state = read_json(run_directory / "state.json")
    return validate_checkpoint(state)


def validate_run_location(run_directory, workspace):
    result = run_directory.resolve()
    source = workspace.resolve()
    if result == source or source in result.parents:
        raise ValueError("run directory must be outside the source workspace")


def attempt_input(_request, assignment, _state, _run_directory):
    return assignment


def response_allowance_available(request, state):
    completed = sum(
        record["component"] == "response" and record["outcome"] == "completed"
        for record in state["history"]
    )
    return completed < request["max_responses"]


def repairable_validation(result_directory, workspace):
    try:
        observed = repository_state(workspace)
        validate_repairable_failure(result_directory, workspace, observed)
    except (OSError, TypeError, ValueError, KeyError, subprocess.SubprocessError):
        return False
    return True


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
        inference=role_inference(request, "review"),
        change_directory=str((run_directory / change).resolve()),
        validation_directory=str((run_directory / validation).resolve()),
    )


def assessment_input(request, assignment, state, run_directory):
    review = latest(state, "review")["directory"]
    return stage_input(
        assignment["workspace"],
        request["agent_timeout_seconds"],
        inference=role_inference(request, "finding_assessment"),
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
    if state["history"][-1]["component"] == "validation":
        validation = state["history"][-1]["directory"]
        source = latest(state, "attempt", "response")
        kind = "attempt" if source["component"] == "attempt" else "feedback_response"
        return stage_input(
            assignment["workspace"],
            request["agent_timeout_seconds"],
            inference=role_inference(request, "feedback_response"),
            validation_directory=str((run_directory / validation).resolve()),
            source={
                "kind": kind,
                "directory": str((run_directory / source["directory"]).resolve()),
            },
            objective=assignment["objective"],
        )
    assessment = latest(state, "assessment")["directory"]
    return stage_input(
        assignment["workspace"],
        request["agent_timeout_seconds"],
        inference=role_inference(request, "feedback_response"),
        assessment_directory=str((run_directory / assessment).resolve()),
    )


def role_inference(request, role):
    roles = request.get("inference_roles")
    return None if roles is None else roles[role]


def stage_input(workspace, timeout, **evidence):
    return {
        "schema_version": 1,
        "workspace": workspace,
        **{name: value for name, value in evidence.items() if value is not None},
        "timeout_seconds": timeout,
    }


def latest(state, *components):
    return next(
        item
        for item in reversed(state["history"])
        if item["component"] in components
        and item["outcome"] == COMPONENTS[item["component"]]["success"]
    )


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
    state_path,
    output_path,
):
    state["status"] = "failed"
    state["next_component"] = None
    state["terminal"] = {
        "failed_component": component,
        "component_outcome": outcome,
        "exit_code": exit_code,
    }
    seal_json(state_path, state)
    return finalize(run_directory, state, output_path)


def output_for(state):
    if state["status"] not in {"completed", "failed"}:
        raise ValueError("cannot finalize a running coordinator checkpoint")
    return {
        "schema_version": 1,
        "outcome": "completed" if state["status"] == "completed" else "failed",
        **state["terminal"],
        "history": state["history"],
    }


def finalize(run_directory, state, output_path=None):
    output = output_for(state)
    exit_code = 0 if state["status"] == "completed" else 1
    validate_output(output)
    output_path = output_path or run_directory / "output.json"
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
