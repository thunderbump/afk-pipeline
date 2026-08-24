"""Small stateful Beads command fixture used only through the publisher CLI."""

import json
import sys
import time
from pathlib import Path


def value(arguments, flag):
    return arguments[arguments.index(flag) + 1]


state_path = Path(sys.argv[1])
arguments = sys.argv[2:]
state = json.loads(state_path.read_text())
command = arguments[0]

if command == "show":
    ids = [item for item in arguments[1:] if not item.startswith("-")]
    issues = [state["parent"]] + state["children"]
    selected = [issue for issue in issues if issue["id"] in ids]
    print(json.dumps(selected))
elif command == "list":
    print(json.dumps(state["children"]))
elif command == "create":
    state["create_attempts"] = state.get("create_attempts", 0) + 1
    if state.get("sleep_create_attempt") == state["create_attempts"]:
        state_path.write_text(json.dumps(state))
        time.sleep(30)
    if state.get("fail_create_attempt") == state["create_attempts"]:
        state["fail_create_attempt"] = None
        state_path.write_text(json.dumps(state))
        print("injected create failure", file=sys.stderr)
        raise SystemExit(1)
    issue_id = f"central-child-{len(state['children']) + 1}"
    labels = value(arguments, "--labels").split(",")
    issue = {
        "id": issue_id,
        "title": arguments[1],
        "description": value(arguments, "--description"),
        "acceptance_criteria": value(arguments, "--acceptance"),
        "external_ref": value(arguments, "--external-ref"),
        "issue_type": value(arguments, "--type"),
        "priority": int(value(arguments, "--priority")),
        "status": "open",
        "labels": labels,
        "dependencies": [
            {
                "id": value(arguments, "--parent"),
                "dependency_type": "parent-child",
            }
        ],
    }
    state["children"].append(issue)
    state_path.write_text(json.dumps(state))
    print(json.dumps(issue))
elif command == "update":
    if state.pop("fail_next_update", False):
        state_path.write_text(json.dumps(state))
        print("injected update failure", file=sys.stderr)
        raise SystemExit(1)
    issue = next(item for item in state["children"] if item["id"] == arguments[1])
    issue["description"] = value(arguments, "--description")
    state_path.write_text(json.dumps(state))
    print(json.dumps([issue]))
elif command == "dep" and arguments[1] == "add":
    issue = next(item for item in state["children"] if item["id"] == arguments[2])
    dependency = {"id": arguments[3], "dependency_type": value(arguments, "--type")}
    if dependency not in issue["dependencies"]:
        issue["dependencies"].append(dependency)
    state_path.write_text(json.dumps(state))
    print(json.dumps(issue))
elif command == "comments" and arguments[1] == "add":
    issue = next(item for item in state["children"] if item["id"] == arguments[2])
    issue.setdefault("comments", []).append(arguments[3])
    state_path.write_text(json.dumps(state))
    print(json.dumps(issue))
elif command == "comments":
    issue = next(item for item in state["children"] if item["id"] == arguments[1])
    print(json.dumps(issue.get("comments", [])))
elif command == "close":
    if state.pop("fail_next_close", False):
        state_path.write_text(json.dumps(state))
        print("injected close failure", file=sys.stderr)
        raise SystemExit(1)
    issue = next(item for item in state["children"] if item["id"] == arguments[1])
    issue["status"] = "closed"
    state_path.write_text(json.dumps(state))
    print(json.dumps(issue))
else:
    print(f"unsupported fake bd command: {arguments}", file=sys.stderr)
    raise SystemExit(2)
