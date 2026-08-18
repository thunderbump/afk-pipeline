"""Trusted host-side preparation of one repository-aware AFK run."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from afk_runtime import progress, seal_json, timestamp, write_json

DEFAULT_CONFIG = Path.home() / ".config" / "afk" / "config.json"
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class PreparationError(Exception):
    pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="afk",
        usage="afk run <bead-id> [--config PATH]",
        description="Prepare and locally execute one central Bead run.",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    run_parser = subparsers.add_parser("run", help="prepare and execute a Bead")
    run_parser.add_argument("bead_id", metavar="<bead-id>")
    run_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    arguments = parser.parse_args(argv)
    if arguments.operation == "run":
        return run(arguments.bead_id, arguments.config)
    return 2


def run(bead_id, config_path):
    artifact = None
    preparation = None
    try:
        if not SAFE_ID.fullmatch(bead_id):
            raise PreparationError(f"Bead {bead_id!r} is not a safe central Bead ID")
        progress(f"loading Run Preparer configuration for Bead {bead_id}")
        config = load_config(config_path)
        progress(f"reading Bead {bead_id} from configured central workspace")
        bead = read_bead(bead_id, config["beads_workspace"])
        project_slug = ownership(bead_id, bead["labels"])
        project = config["projects"].get(project_slug)
        if project is None:
            raise PreparationError(
                f"Bead {bead_id} project:{project_slug} has no configured project mapping"
            )
        repository, base_commit = resolve_project(bead_id, project_slug, project)
        run_id = new_run_id()
        artifact = destination(config["run_root"], bead_id, run_id)
        worktree = destination(config["worktree_root"], bead_id, run_id)
        ensure_destinations(bead_id, config, artifact, worktree)
        branch = f"afk/{bead_id}/{run_id}"
        if (
            git_result(
                repository, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
            ).returncode
            == 0
        ):
            raise PreparationError(
                f"Bead {bead_id} branch destination {branch!r} already exists"
            )

        artifact.mkdir(parents=True)
        source_record = safe_bead(bead_id, bead)
        assignment = {
            "schema_version": 1,
            "objective": objective(bead),
            "workspace": str(worktree),
            **config["assignment"],
            "source": {"kind": "bead", "id": bead_id},
        }
        request = {
            "schema_version": 1,
            "assignment_path": str(artifact / "assignment.json"),
            "validation": project["validation"],
            **config["coordinator"],
        }
        write_json(artifact / "bead.json", source_record)
        write_json(artifact / "assignment.json", assignment)
        write_json(artifact / "coordinator-request.json", request)
        started = timestamp()
        preparation = {
            "schema_version": 1,
            "run": {"id": run_id, "artifact_root": str(artifact)},
            "bead": {"id": bead_id},
            "project": {"slug": project_slug},
            "repository": {
                "path": str(repository),
                "base_ref": project["base_ref"],
                "base_commit": base_commit,
                "branch": branch,
                "worktree": str(worktree),
            },
            "timestamps": {
                "started_at": started,
                "prepared_at": None,
                "finished_at": None,
            },
            "preparation_status": "preparing",
            "coordinator": {
                "command": [
                    sys.executable,
                    "-m",
                    "afk_coordinate",
                    str(artifact / "coordinator-request.json"),
                    str(artifact / "coordinator"),
                ],
                "directory": "coordinator",
                "result": "coordinator/output.json",
                "status": "not_started",
                "exit_code": None,
                "outcome": None,
            },
            "errors": [],
        }
        seal_json(artifact / "preparation.json", preparation)
        progress(f"creating prepared worktree for Bead {bead_id} at {worktree}")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        added = git_result(
            repository, "worktree", "add", "-b", branch, str(worktree), base_commit
        )
        if added.returncode != 0:
            rollback_worktree(repository, worktree, branch, base_commit)
            (artifact / "coordinator").mkdir()
            fail_preparation(
                preparation,
                "worktree_creation",
                f"could not create isolated worktree for Bead {bead_id}",
            )
            seal_json(artifact / "preparation.json", preparation)
            raise PreparationError(
                f"Bead {bead_id} isolated worktree preparation failed"
            )

        preparation["preparation_status"] = "prepared"
        preparation["timestamps"]["prepared_at"] = timestamp()
        preparation["coordinator"]["status"] = "running"
        seal_json(artifact / "preparation.json", preparation)
        progress(f"starting coordinator for Bead {bead_id}")
        try:
            completed = subprocess.run(
                preparation["coordinator"]["command"],
                cwd=Path(__file__).parent,
                env=worker_environment(),
                check=False,
            )
        except OSError:
            (artifact / "coordinator").mkdir(exist_ok=True)
            preparation["coordinator"].update(
                status="failed", exit_code=None, outcome=None
            )
            preparation["timestamps"]["finished_at"] = timestamp()
            preparation["errors"].append(
                {
                    "category": "coordinator_launch",
                    "message": f"coordinator could not be started for Bead {bead_id}",
                }
            )
            seal_json(artifact / "preparation.json", preparation)
            raise PreparationError(
                f"coordinator could not be started for Bead {bead_id}"
            )
        code = completed.returncode if completed.returncode >= 0 else 1
        output_path = artifact / "coordinator" / "output.json"
        outcome = None
        if output_path.is_file():
            try:
                value = json.loads(output_path.read_text())
                if isinstance(value, dict) and isinstance(value.get("outcome"), str):
                    outcome = value["outcome"]
            except (OSError, json.JSONDecodeError):
                pass
        preparation["coordinator"].update(
            status="completed" if code == 0 else "failed",
            exit_code=code,
            outcome=outcome,
        )
        preparation["timestamps"]["finished_at"] = timestamp()
        seal_json(artifact / "preparation.json", preparation)
        progress(
            f"coordinator terminal outcome for Bead {bead_id}: {outcome or 'unsealed failure'}"
        )
        print(f"artifact root: {artifact}", flush=True)
        return code
    except PreparationError as error:
        if artifact is not None:
            if (
                preparation is not None
                and preparation["preparation_status"] == "preparing"
                and not preparation["errors"]
            ):
                fail_preparation(preparation, "preparation", str(error))
                seal_json(artifact / "preparation.json", preparation)
            print(f"artifact root: {artifact}", flush=True)
        print(f"afk run: Bead {bead_id}: {error}", file=sys.stderr)
        return 2
    except OSError:
        if artifact is not None and preparation is not None:
            try:
                (artifact / "coordinator").mkdir(exist_ok=True)
            except OSError:
                pass
            fail_preparation(
                preparation,
                "filesystem",
                f"filesystem preparation failed for Bead {bead_id}",
            )
            try:
                seal_json(artifact / "preparation.json", preparation)
            except OSError:
                pass
            print(f"artifact root: {artifact}", flush=True)
        print(
            f"afk run: Bead {bead_id}: filesystem preparation failed", file=sys.stderr
        )
        return 2
    except KeyboardInterrupt:
        if artifact is not None and preparation is not None:
            fail_preparation(
                preparation,
                "interrupted",
                f"Bead {bead_id} Run Preparer was interrupted",
            )
            seal_json(artifact / "preparation.json", preparation)
            print(f"artifact root: {artifact}", flush=True)
        print(f"afk run: Bead {bead_id} preparation interrupted", file=sys.stderr)
        return 130


def load_config(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PreparationError(
            f"configuration {path} cannot be read as JSON"
        ) from error
    expected = {
        "schema_version",
        "beads_workspace",
        "run_root",
        "worktree_root",
        "assignment",
        "coordinator",
        "projects",
    }
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or set(value) != expected
    ):
        raise PreparationError(
            f"configuration {path} is malformed (expected schema_version 1)"
        )
    for name in ("beads_workspace", "run_root", "worktree_root"):
        value[name] = absolute_path(value[name], f"configuration {name}")
        if value[name].exists() and not value[name].is_dir():
            raise PreparationError(
                f"configured {name} {value[name]} is not a directory"
            )
    if not value["beads_workspace"].is_dir():
        raise PreparationError(
            f"configured central Beads workspace {value['beads_workspace']} is unavailable"
        )
    validate_assignment_defaults(value["assignment"])
    validate_coordinator(value["coordinator"])
    projects = value["projects"]
    if not isinstance(projects, dict) or not projects:
        raise PreparationError("configuration projects must be a nonempty object")
    for slug, project in projects.items():
        validate_project(slug, project)
    return value


def absolute_path(value, fact):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise PreparationError(f"{fact} must be an absolute path")
    return Path(value).resolve()


def validate_assignment_defaults(value):
    if not isinstance(value, dict) or set(value) != {"command", "timeout_seconds"}:
        raise PreparationError(
            "configuration assignment must contain command and timeout_seconds"
        )
    argv(value["command"], "assignment command")
    positive(value["timeout_seconds"], "assignment timeout_seconds")


def validate_coordinator(value):
    if not isinstance(value, dict) or set(value) != {
        "agent_timeout_seconds",
        "max_responses",
    }:
        raise PreparationError("configuration coordinator is malformed")
    positive(value["agent_timeout_seconds"], "coordinator agent_timeout_seconds")
    if (
        not isinstance(value["max_responses"], int)
        or isinstance(value["max_responses"], bool)
        or value["max_responses"] < 0
    ):
        raise PreparationError(
            "coordinator max_responses must be a nonnegative integer"
        )


def validate_project(slug, value):
    if not isinstance(slug, str) or not SAFE_ID.fullmatch(slug):
        raise PreparationError("configuration contains an invalid project slug")
    if not isinstance(value, dict) or set(value) != {
        "repository",
        "base_ref",
        "validation",
    }:
        raise PreparationError(f"configuration project:{slug} is malformed")
    value["repository"] = absolute_path(
        value["repository"], f"project:{slug} repository"
    )
    if not isinstance(value["base_ref"], str) or not value["base_ref"]:
        raise PreparationError(f"project:{slug} base_ref must be a nonempty string")
    validation = value["validation"]
    if not isinstance(validation, dict) or set(validation) != {
        "command",
        "timeout_seconds",
    }:
        raise PreparationError(f"project:{slug} validation is malformed")
    argv(validation["command"], f"project:{slug} validation command")
    positive(
        validation["timeout_seconds"], f"project:{slug} validation timeout_seconds"
    )


def argv(value, name):
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise PreparationError(f"{name} must be a nonempty argv array")


def positive(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PreparationError(f"{name} must be a positive integer")


def read_bead(bead_id, workspace):
    try:
        completed = subprocess.run(
            ["bd", "show", bead_id, "--json"],
            cwd=workspace,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise PreparationError(
            f"Bead {bead_id} cannot be read from the configured central workspace"
        ) from error
    if completed.returncode != 0:
        raise PreparationError(
            f"Bead {bead_id} was not found in the configured central workspace"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise PreparationError(
            f"Bead {bead_id} returned malformed data from the configured central workspace"
        ) from error
    if isinstance(value, list):
        if len(value) != 1:
            raise PreparationError(
                f"Bead {bead_id} did not resolve to exactly one central record"
            )
        value = value[0]
    if (
        not isinstance(value, dict)
        or value.get("id") != bead_id
        or not isinstance(value.get("title"), str)
        or not value["title"].strip()
    ):
        raise PreparationError(
            f"Bead {bead_id} returned malformed data from the configured central workspace"
        )
    for name in ("description", "design", "acceptance_criteria"):
        if (
            name in value
            and value[name] is not None
            and not isinstance(value[name], str)
        ):
            raise PreparationError(f"Bead {bead_id} field {name} is malformed")
    if not isinstance(value.get("labels"), list) or not all(
        isinstance(label, str) for label in value["labels"]
    ):
        raise PreparationError(f"Bead {bead_id} labels are malformed")
    return value


def ownership(bead_id, labels):
    owners = [
        label.removeprefix("project:")
        for label in labels
        if label.startswith("project:") and label != "project:"
    ]
    if len(owners) != 1 or not SAFE_ID.fullmatch(owners[0]):
        raise PreparationError(
            f"Bead {bead_id} must have exactly one project:<slug> ownership label"
        )
    return owners[0]


def resolve_project(bead_id, slug, project):
    repository = project["repository"]
    probe = (
        git_result(repository, "rev-parse", "--show-toplevel")
        if repository.is_dir()
        else None
    )
    if (
        probe is None
        or probe.returncode != 0
        or Path(probe.stdout.strip()).resolve() != repository
    ):
        raise PreparationError(
            f"Bead {bead_id} project:{slug} repository {repository} is not a valid repository root"
        )
    resolved = git_result(
        repository, "rev-parse", "--verify", f"{project['base_ref']}^{{commit}}"
    )
    if resolved.returncode != 0 or not re.fullmatch(
        r"[0-9a-fA-F]{40,64}", resolved.stdout.strip()
    ):
        raise PreparationError(
            f"Bead {bead_id} project:{slug} base ref {project['base_ref']!r} is unavailable"
        )
    return repository, resolved.stdout.strip().lower()


def safe_bead(bead_id, bead):
    result = {
        "schema_version": 1,
        "source": {"kind": "bead", "id": bead_id},
        "title": bead["title"],
        "labels": bead["labels"],
    }
    for name in ("description", "design", "acceptance_criteria"):
        if bead.get(name) is not None:
            result[name] = bead[name]
    return result


def objective(bead):
    sections = [bead["title"].strip()]
    for field, heading in (
        ("description", "Description"),
        ("design", "Design"),
        ("acceptance_criteria", "Acceptance criteria"),
    ):
        value = bead.get(field)
        if isinstance(value, str) and value.strip():
            sections.append(f"{heading}\n{value.strip()}")
    return "\n\n".join(sections)


def new_run_id():
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{now}-{uuid.uuid4().hex[:8]}"


def destination(root, bead_id, run_id):
    return root / bead_id / run_id


def ensure_destinations(bead_id, config, artifact, worktree):
    run_root = config["run_root"]
    worktree_root = config["worktree_root"]
    if (
        run_root == worktree_root
        or run_root in worktree_root.parents
        or worktree_root in run_root.parents
    ):
        raise PreparationError(
            f"Bead {bead_id} Run and worktree roots overlap unsafely"
        )
    for path, fact in ((artifact, "artifact"), (worktree, "worktree")):
        if os.path.lexists(path):
            raise PreparationError(
                f"Bead {bead_id} {fact} destination {path} already exists"
            )


def git_result(repository, *arguments):
    return subprocess.run(
        ["git", *arguments], cwd=repository, text=True, capture_output=True, check=False
    )


def rollback_worktree(repository, worktree, branch, base_commit):
    git_result(repository, "worktree", "remove", "--force", str(worktree))
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    branch_commit = git_result(
        repository, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"
    )
    if (
        branch_commit.returncode == 0
        and branch_commit.stdout.strip().lower() == base_commit
    ):
        git_result(repository, "branch", "-D", branch)


def fail_preparation(preparation, category, message):
    preparation["preparation_status"] = "failed"
    preparation["timestamps"]["finished_at"] = timestamp()
    preparation["errors"].append({"category": category, "message": message})


def worker_environment():
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "TERM", "TZ"}
    exact = {
        "AFK_REVIEW_AGENT_COMMAND",
        "AFK_ASSESS_AGENT_COMMAND",
        "AFK_RESPOND_AGENT_COMMAND",
    }
    return {
        name: value
        for name, value in os.environ.items()
        if name in allowed
        or name in exact
        or name.startswith(("LC_", "PI_", "OPENAI_", "ANTHROPIC_"))
    }
