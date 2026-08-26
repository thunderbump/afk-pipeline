"""Validation contracts for Coordinator input and sealed output."""

from pathlib import Path

from afk_config import validate_inference_setting

# These are the topology facts needed to prove that terminal history could have
# been produced by the Coordinator. Runtime module selection and input building
# deliberately remain in the CLI.
COMPONENT_TOPOLOGY = {
    "attempt": {
        "success": "succeeded",
        "outcomes": {"succeeded", "failed", "timed_out", "interrupted"},
        "next": "validation",
    },
    "validation": {
        "success": "passed",
        "outcomes": {"passed", "failed", "timed_out", "interrupted"},
        "next": "change",
    },
    "change": {
        "success": "completed",
        "outcomes": {"completed"},
        "next": "review",
    },
    "review": {
        "success": "completed",
        "outcomes": {"completed", "failed", "timed_out", "interrupted"},
        "next": "assessment",
    },
    "assessment": {
        "success": "completed",
        "outcomes": {"completed", "failed", "timed_out", "interrupted"},
        "next": "iteration",
    },
    "iteration": {
        "success": "completed",
        "outcomes": {"completed"},
        "next": "response",
    },
    "response": {
        "success": "completed",
        "outcomes": {"completed", "failed", "timed_out", "interrupted"},
        "next": "validation",
    },
}


def validate_request(value):
    """Validate the frozen Coordinator request shared by runners and observers."""
    expected = {
        "schema_version",
        "assignment_path",
        "validation",
        "agent_timeout_seconds",
        "max_responses",
    }
    if isinstance(value, dict) and "inference_roles" in value:
        expected.add("inference_roles")
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
    roles = value.get("inference_roles")
    if roles is not None:
        expected_roles = {"review", "finding_assessment", "feedback_response"}
        if not isinstance(roles, dict) or set(roles) != expected_roles:
            raise ValueError("coordinator inference_roles is malformed")
        for setting in roles.values():
            validate_inference_setting(setting)
    limit = value["max_responses"]
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("max_responses must be a nonnegative integer")
    return value


def positive_integer(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def validate_component_output(component, output):
    """Validate the Coordinator-facing envelope for one module result."""
    specification = COMPONENT_TOPOLOGY[component]
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


def validate_checkpoint(state):
    """Validate Coordinator checkpoint shape and invocation topology."""
    expected = {
        "schema_version",
        "status",
        "next_sequence",
        "next_component",
        "active_invocation",
        "history",
        "terminal",
    }
    if isinstance(state, dict) and "continuation" in state:
        expected.add("continuation")
    if (
        not isinstance(state, dict)
        or set(state) != expected
        or state.get("schema_version") != 1
        or state.get("status") not in {"running", "completed", "failed"}
    ):
        raise ValueError("invalid coordinator checkpoint")
    if "continuation" in state:
        validate_continuation(state["continuation"])
    sequence = state.get("next_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ValueError("invalid coordinator checkpoint")
    component = state.get("next_component")
    if component is not None and component not in COMPONENT_TOPOLOGY:
        raise ValueError("invalid coordinator checkpoint")
    history = state.get("history")
    if not isinstance(history, list):
        raise TypeError("invalid coordinator checkpoint")
    prior_sequence = 0
    for record in history:
        if not valid_invocation_record(record, terminal=True):
            raise ValueError("invalid coordinator checkpoint")
        if record["sequence"] <= prior_sequence or record["sequence"] >= sequence:
            raise ValueError("invalid coordinator checkpoint")
        prior_sequence = record["sequence"]
    active = state.get("active_invocation")
    if active is not None and (
        not valid_invocation_record(active, terminal=False)
        or active["sequence"] != sequence - 1
        or active["sequence"] <= prior_sequence
        or active["component"] != component
    ):
        raise ValueError("invalid coordinator checkpoint")
    if state["status"] == "running":
        if component is None or state["terminal"] is not None:
            raise ValueError("invalid coordinator checkpoint")
    elif active is not None or component is not None:
        raise ValueError("invalid coordinator checkpoint")
    validate_terminal(state["status"], state["terminal"])
    validate_terminal_history(state)
    validate_history_position(state)
    return state


def validate_continuation(value):
    expected = {
        "schema_version",
        "additional_responses",
        "completed_responses",
        "effective_max_responses",
        "prior_output",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != 1
    ):
        raise ValueError("invalid continuation input")
    additional = value["additional_responses"]
    completed = value["completed_responses"]
    effective = value["effective_max_responses"]
    if (
        not isinstance(additional, int)
        or isinstance(additional, bool)
        or additional <= 0
        or not isinstance(completed, int)
        or isinstance(completed, bool)
        or completed < 0
        or effective != completed + additional
        or not isinstance(value["prior_output"], str)
        or not value["prior_output"]
    ):
        raise ValueError("invalid continuation input")
    return value


def valid_invocation_record(record, terminal):
    fields = {"sequence", "component", "directory", "input_from"}
    if terminal:
        fields.add("outcome")
    if not isinstance(record, dict) or set(record) != fields:
        return False
    sequence = record.get("sequence")
    component = record.get("component")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or component not in COMPONENT_TOPOLOGY
        or record.get("directory") != f"{sequence:02d}-{component}"
        or not valid_input_sources(record.get("input_from"))
    ):
        return False
    if not terminal:
        return True
    return record.get("outcome") in {
        *COMPONENT_TOPOLOGY[component]["outcomes"],
        "abandoned",
    }


def valid_input_sources(value):
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(name, str) and name and isinstance(source, str) and source
            for name, source in value.items()
        )
    )


def validate_terminal(status, terminal):
    if status == "running":
        if terminal is not None:
            raise ValueError("invalid coordinator checkpoint")
        return
    if status == "completed":
        if (
            not isinstance(terminal, dict)
            or set(terminal) != {"decision"}
            or terminal["decision"] not in {"stop", "exhausted"}
        ):
            raise ValueError("invalid coordinator checkpoint")
        return
    if (
        not isinstance(terminal, dict)
        or set(terminal) != {"failed_component", "component_outcome", "exit_code"}
        or terminal["failed_component"] not in COMPONENT_TOPOLOGY
        or not isinstance(terminal["component_outcome"], str)
        or (
            terminal["exit_code"] is not None
            and (
                not isinstance(terminal["exit_code"], int)
                or isinstance(terminal["exit_code"], bool)
            )
        )
    ):
        raise ValueError("invalid coordinator checkpoint")


def validate_terminal_history(state):
    if state["status"] == "running":
        return
    if not state["history"]:
        raise ValueError("invalid coordinator checkpoint")
    last = state["history"][-1]
    if state["status"] == "completed":
        if last["component"] != "iteration" or last["outcome"] != "completed":
            raise ValueError("invalid coordinator checkpoint")
        return
    terminal = state["terminal"]
    specification = COMPONENT_TOPOLOGY[last["component"]]
    if (
        last["component"] != terminal["failed_component"]
        or last["outcome"] != terminal["component_outcome"]
        or last["outcome"] not in specification["outcomes"]
        or last["outcome"] == specification["success"]
    ):
        raise ValueError("invalid coordinator checkpoint")


def validate_history_position(state):
    expected_component = "attempt"
    prior = []
    for expected_sequence, record in enumerate(state["history"], start=1):
        if (
            record["sequence"] != expected_sequence
            or record["component"] != expected_component
            or record["input_from"] != expected_input_sources(expected_component, prior)
        ):
            raise ValueError("invalid coordinator checkpoint")
        prior.append(record)
        if record["outcome"] == "abandoned":
            continue
        if record["outcome"] != COMPONENT_TOPOLOGY[expected_component]["success"]:
            expected_component = (
                "response"
                if expected_component == "validation" and record["outcome"] == "failed"
                else None
            )
        else:
            expected_component = COMPONENT_TOPOLOGY[expected_component]["next"]

    active = state["active_invocation"]
    if active is not None:
        if (
            active["sequence"] != len(prior) + 1
            or active["component"] != expected_component
            or active["input_from"] != expected_input_sources(expected_component, prior)
            or state["next_sequence"] != active["sequence"] + 1
            or state["next_component"] != expected_component
        ):
            raise ValueError("invalid coordinator checkpoint")
        return

    if state["next_sequence"] != len(prior) + 1:
        raise ValueError("invalid coordinator checkpoint")
    if state["status"] == "running" and state["next_component"] != expected_component:
        raise ValueError("invalid coordinator checkpoint")


def expected_input_sources(component, history):
    if component == "attempt":
        return {"assignment": "assignment.json"}
    if component in {"validation", "change"}:
        source = latest_record(history, "attempt", "response")["directory"]
        if component == "validation":
            return {"workspace": "assignment.json", "change": source}
        return {"source": source}
    if component == "review":
        return {
            "change": latest_record(history, "change")["directory"],
            "validation": latest_record(history, "validation")["directory"],
        }
    if component == "assessment":
        return {"review": latest_record(history, "review")["directory"]}
    if component == "iteration":
        return {"assessment": latest_record(history, "assessment")["directory"]}
    if component == "response":
        if history and history[-1]["component"] == "validation":
            return {"validation": history[-1]["directory"]}
        return {"assessment": latest_record(history, "assessment")["directory"]}
    raise ValueError("invalid coordinator checkpoint")


def latest_record(history, *components):
    for record in reversed(history):
        if (
            record["component"] in components
            and record["outcome"] == COMPONENT_TOPOLOGY[record["component"]]["success"]
        ):
            return record
    raise ValueError("invalid coordinator checkpoint")


def validate_output(output):
    """Validate a sealed terminal output against the Coordinator contract."""
    if not isinstance(output, dict):
        raise TypeError("invalid coordinator output")
    outcome = output.get("outcome")
    if outcome == "completed":
        expected = {"schema_version", "outcome", "decision", "history"}
        terminal = {"decision": output.get("decision")}
        status = "completed"
    elif outcome == "failed":
        expected = {
            "schema_version",
            "outcome",
            "failed_component",
            "component_outcome",
            "exit_code",
            "history",
        }
        terminal = {
            "failed_component": output.get("failed_component"),
            "component_outcome": output.get("component_outcome"),
            "exit_code": output.get("exit_code"),
        }
        status = "failed"
    else:
        raise ValueError("invalid coordinator output")
    history = output.get("history")
    if set(output) != expected or not isinstance(history, list):
        raise ValueError("invalid coordinator output")
    validate_checkpoint(
        {
            "schema_version": output.get("schema_version"),
            "status": status,
            "next_sequence": len(history) + 1,
            "next_component": None,
            "active_invocation": None,
            "history": history,
            "terminal": terminal,
        }
    )
    return output
