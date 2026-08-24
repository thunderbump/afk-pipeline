"""Trusted host-side preparation of one repository-aware AFK run."""

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from afk_config import attestation_result_root
from afk_coordinate.contract import validate_output as validate_coordinator_output
from afk_plan.contract import validate_catalog, validate_planner_output
from afk_plan.contract import validate_input as validate_planner_input
from afk_plan_accept.contract import validate_policy_output
from afk_preflight.contract import validate_output as validate_preflight_output
from afk_runtime import (
    progress,
    run_command,
    seal_json,
    terminate,
    timestamp,
    write_json,
)

DEFAULT_CONFIG = Path.home() / ".config" / "afk" / "config.json"
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
ASSIGNMENT_PATH_PLACEHOLDER = "{assignment_path}"
PUBLICATION_BUNDLE_PLACEHOLDER = "{bundle_path}"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
MAX_ADMISSION_OUTPUT_BYTES = 64 * 1024


class PreparationError(Exception):
    pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="afk",
        usage=(
            "afk run <bead-id> [--config PATH] | "
            "afk attest CHILD_ID --publication DIRECTORY --subject FIELD=VALUE "
            "--evidence VALUE [--accept] | "
            "afk export <sealed-run> <new-bundle-directory> [--project SLUG --run-id ID]"
        ),
        description="Prepare, execute, attest, or export AFK work.",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    run_parser = subparsers.add_parser("run", help="prepare and execute a Bead")
    run_parser.add_argument("bead_id", metavar="<bead-id>")
    run_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    from afk_attest import add_parser as add_attest_parser

    add_attest_parser(subparsers)
    export_parser = subparsers.add_parser(
        "export", help="export one sealed Run as a portable bundle"
    )
    export_parser.add_argument("source", type=Path, metavar="<sealed-run>")
    export_parser.add_argument(
        "destination", type=Path, metavar="<new-bundle-directory>"
    )
    export_parser.add_argument("--project")
    export_parser.add_argument("--run-id")
    export_parser.add_argument("--bead-id")
    export_parser.add_argument(
        "--schema-version",
        type=int,
        choices=(1, 2),
        default=2,
        help="Publication Bundle schema (default: 2)",
    )
    arguments = parser.parse_args(argv)
    if arguments.operation == "run":
        return run(arguments.bead_id, arguments.config)
    if arguments.operation == "attest":
        from afk_attest import attest

        return attest(arguments)
    if arguments.operation == "export":
        from afk_export import ExportError, ExportUsageError, export_run

        try:
            result = export_run(
                arguments.source,
                arguments.destination,
                arguments.project,
                arguments.run_id,
                arguments.bead_id,
                arguments.schema_version,
            )
        except ExportUsageError:
            print(
                json.dumps(
                    {"schema_version": 1, "outcome": "rejected", "error": "usage"}
                )
            )
            return 2
        except (
            ExportError,
            OSError,
            TypeError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ):
            print(
                json.dumps(
                    {"schema_version": 1, "outcome": "rejected", "error": "invalid_run"}
                )
            )
            return 1
        print(json.dumps(result))
        return 0
    return 2


def run(bead_id, config_path):
    artifact = None
    artifact_io = None
    artifact_owned = False
    preparation = None
    terminal_sealed = False
    leases = []
    open_fds = []
    try:
        if not SAFE_ID.fullmatch(bead_id):
            raise PreparationError(f"Bead {bead_id!r} is not a safe central Bead ID")
        progress(f"loading Run Preparer configuration for Bead {bead_id}")
        config = load_config(config_path)
        progress(f"reading Bead {bead_id} from configured central workspace")
        bead = read_bead(bead_id, config["beads_workspace"])
        require_agent_readiness(bead_id, bead["labels"])
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
        ensure_destination_layout(bead_id, config, repository, artifact, worktree)
        # Flat refs cannot collide with bootstrap refs such as afk/<bead-id>.
        branch = f"afk-{bead_id}-{run_id}"
        leases.append(acquire_directory(repository, create=False))
        ensure_branch_available(bead_id, repository, branch)

        # Serialize cooperating preparers through exclusive creation and Git
        # handoff. Open directory descriptors anchor operations if paths move.
        for root in sorted(
            (config["run_root"], config["worktree_root"]), key=lambda path: str(path)
        ):
            leases.append(acquire_directory(root, create=True))
        run_lease = lease_for(leases, config["run_root"])
        worktree_lease = lease_for(leases, config["worktree_root"])
        artifact_parent_fd = prepare_parent(run_lease, bead_id, run_id, "artifact")
        worktree_parent_fd = prepare_parent(worktree_lease, bead_id, run_id, "worktree")
        open_fds.extend((artifact_parent_fd, worktree_parent_fd))

        source_record = safe_bead(bead_id, bead)
        assignment_path = artifact / "assignment.json"
        assignment_defaults = dict(config["assignment"])
        assignment_defaults["command"] = assignment_command(
            assignment_defaults["command"], assignment_path
        )
        assignment = {
            "schema_version": 1,
            "objective": objective(bead),
            "workspace": str(worktree),
            **assignment_defaults,
            "source": {"kind": "bead", "id": bead_id},
        }
        request = {
            "schema_version": 1,
            "assignment_path": str(assignment_path),
            "validation": {
                "command": project["validation"]["command"],
                "timeout_seconds": project["validation"]["timeout_seconds"],
            },
            **config["coordinator"],
        }
        routing_config = config.get("acceptance_routing")
        preflight_request = None
        planner_request = None
        if routing_config is None:
            preflight_request = acceptance_preflight(
                source_record, project["validation"], config["coordinator"]
            )
        else:
            planner_request = acceptance_routing_request(bead_id, bead, routing_config)
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
            **(
                {
                    "preflight": {
                        "command": [
                            sys.executable,
                            "-m",
                            "afk_preflight",
                            str(artifact / "preflight-input.json"),
                            str(artifact / "preflight"),
                            "--classification-store",
                            str(config["classification_store"]),
                        ],
                        "directory": "preflight",
                        "result": "preflight/output.json",
                        "status": "not_started",
                        "exit_code": None,
                        "outcome": None,
                        "decision": None,
                    }
                }
                if preflight_request is not None
                else {
                    "routing": {
                        "planner": {
                            "command": [
                                sys.executable,
                                "-m",
                                "afk_plan",
                                str(artifact / "planner-input.json"),
                                str(artifact / "planner"),
                            ],
                            "directory": "planner",
                            "result": "planner/output.json",
                            "status": "not_started",
                            "exit_code": None,
                            "outcome": None,
                        },
                        "policy": {
                            "command": [
                                sys.executable,
                                "-m",
                                "afk_plan_accept",
                                str(artifact / "policy-input.json"),
                                str(artifact / "policy"),
                            ],
                            "directory": "policy",
                            "result": "policy/output.json",
                            "status": "not_started",
                            "exit_code": None,
                            "outcome": None,
                            "decision": None,
                        },
                    }
                }
            ),
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
                "decision": None,
            },
            "errors": [],
        }
        # Create relative to the open parent. Evidence writes use the owned
        # descriptor and therefore do not traverse that parent a second time.
        try:
            os.mkdir(run_id, dir_fd=artifact_parent_fd)
        except FileExistsError as error:
            raise PreparationError(
                f"Bead {bead_id} artifact destination {artifact} already exists"
            ) from error
        artifact_fd = os.open(run_id, DIRECTORY_FLAGS, dir_fd=artifact_parent_fd)
        open_fds.append(artifact_fd)
        artifact_io = Path(f"/proc/self/fd/{artifact_fd}")
        artifact_owned = True
        if preflight_request is not None:
            (artifact_io / "preflight").mkdir()
        else:
            (artifact_io / "planner").mkdir()
            (artifact_io / "policy").mkdir()
        (artifact_io / "coordinator").mkdir()
        seal_json(artifact_io / "preparation.json", preparation)
        write_json(artifact_io / "bead.json", source_record)
        write_json(artifact_io / "assignment.json", assignment)
        if preflight_request is not None:
            write_json(artifact_io / "preflight-input.json", preflight_request)
        else:
            write_json(artifact_io / "planner-input.json", planner_request)
        write_json(artifact_io / "coordinator-request.json", request)
        progress(f"creating prepared worktree for Bead {bead_id} at {worktree}")
        require_identity(worktree.parent, worktree_parent_fd, "worktree parent")
        added = git_result(
            repository, "worktree", "add", "-b", branch, str(worktree), base_commit
        )
        if added.returncode != 0:
            # A failed git command may have left evidence at the destination, or a
            # concurrent actor may have created it after validation.  Never remove
            # an unproven path; the sealed preparation record is the failure policy.
            fail_preparation(
                preparation,
                "worktree_creation",
                f"could not create isolated worktree for Bead {bead_id}",
            )
            seal_json(artifact_io / "preparation.json", preparation)
            raise PreparationError(
                f"Bead {bead_id} isolated worktree preparation failed"
            )

        try:
            worktree_fd = os.open(run_id, DIRECTORY_FLAGS, dir_fd=worktree_parent_fd)
        except OSError as error:
            fail_preparation(
                preparation,
                "worktree_creation",
                f"could not verify isolated worktree for Bead {bead_id}",
            )
            seal_json(artifact_io / "preparation.json", preparation)
            raise PreparationError(
                f"Bead {bead_id} isolated worktree preparation failed"
            ) from error
        open_fds.append(worktree_fd)
        require_identity(artifact, artifact_fd, "artifact destination")
        require_identity(worktree, worktree_fd, "worktree destination")
        preparation["preparation_status"] = "prepared"
        preparation["timestamps"]["prepared_at"] = timestamp()
        if preflight_request is not None:
            preparation["preflight"]["status"] = "running"
        else:
            preparation["routing"]["planner"]["status"] = "running"
        seal_json(artifact_io / "preparation.json", preparation)
        # Revalidation above is the handoff boundary. Later path replacement by
        # an actor that ignores these locks is outside the local-host contract.
        close_resources(open_fds, leases)
        open_fds.clear()
        leases.clear()
        artifact_io = artifact
        if preflight_request is not None:
            admission_code = execute_preflight(
                bead_id, artifact, preparation, preflight_request
            )
        else:
            admission_code = execute_acceptance_routing(
                bead_id, artifact, preparation, planner_request
            )
        if admission_code != 0:
            # Only a validated completed pause is a publishable Preflight
            # terminal. Failed, malformed, and interrupted gates remain local
            # fail-closed evidence and never reach Coordinator or Admission.
            if preparation["preparation_status"] == "paused":
                terminal_sealed = True
                publish_configured_run(bead_id, artifact, config)
            print(f"artifact root: {artifact}", flush=True)
            return admission_code

        preparation["coordinator"]["status"] = "running"
        seal_json(artifact_io / "preparation.json", preparation)
        progress(f"starting coordinator for Bead {bead_id}")
        # The unchanged coordinator contract owns creation of its run directory.
        # It has been present throughout preparation and is empty at this handoff.
        (artifact / "coordinator").rmdir()
        try:
            completed = subprocess.run(
                preparation["coordinator"]["command"],
                cwd=Path(__file__).parent,
                env=worker_environment(),
                check=False,
            )
        except OSError:
            (artifact_io / "coordinator").mkdir(exist_ok=True)
            preparation["coordinator"].update(
                status="failed", exit_code=None, outcome=None, decision=None
            )
            preparation["timestamps"]["finished_at"] = timestamp()
            preparation["errors"].append(
                {
                    "category": "coordinator_launch",
                    "message": f"coordinator could not be started for Bead {bead_id}",
                }
            )
            seal_json(artifact_io / "preparation.json", preparation)
            raise PreparationError(
                f"coordinator could not be started for Bead {bead_id}"
            )
        code = completed.returncode if completed.returncode >= 0 else 1
        output_path = artifact / "coordinator" / "output.json"
        outcome, decision = coordinator_terminal(output_path)
        preparation["coordinator"].update(
            status=("completed" if code == 0 and outcome == "completed" else "failed"),
            exit_code=code,
            outcome=outcome,
            decision=decision,
        )
        preparation["timestamps"]["finished_at"] = timestamp()
        seal_json(artifact_io / "preparation.json", preparation)
        terminal_sealed = True
        progress(
            f"coordinator terminal decision for Bead {bead_id}: "
            f"{decision or 'unavailable'}"
        )
        publication_succeeded = publish_configured_run(bead_id, artifact, config)
        print(f"artifact root: {artifact}", flush=True)
        terminal_code = terminal_exit_code(code, outcome, decision)
        return 1 if terminal_code == 0 and not publication_succeeded else terminal_code
    except PreparationError as error:
        if artifact_owned:
            if (
                preparation is not None
                and preparation["preparation_status"] == "preparing"
                and not preparation["errors"]
            ):
                fail_preparation(preparation, "preparation", str(error))
                seal_json(artifact_io / "preparation.json", preparation)
            print(f"artifact root: {artifact}", flush=True)
        print(f"afk run: Bead {bead_id}: {error}", file=sys.stderr)
        return 2
    except OSError:
        if artifact_owned:
            if preparation is not None and not terminal_sealed:
                try:
                    (artifact_io / "coordinator").mkdir(exist_ok=True)
                except OSError:
                    pass
                fail_preparation(
                    preparation,
                    "filesystem",
                    f"filesystem preparation failed for Bead {bead_id}",
                )
                try:
                    seal_json(artifact_io / "preparation.json", preparation)
                except OSError:
                    pass
            print(f"artifact root: {artifact}", flush=True)
        print(
            f"afk run: Bead {bead_id}: filesystem preparation failed", file=sys.stderr
        )
        return 2
    except KeyboardInterrupt:
        if artifact_owned:
            if preparation is not None and not terminal_sealed:
                if preparation.get("preflight", {}).get("status") == "running":
                    preparation["preflight"].update(
                        status="failed",
                        exit_code=130,
                        outcome="interrupted",
                        decision="pause",
                    )
                if (
                    preparation.get("routing", {}).get("planner", {}).get("status")
                    == "running"
                ):
                    preparation["routing"]["planner"].update(
                        status="failed", exit_code=130, outcome="interrupted"
                    )
                if (
                    preparation.get("routing", {}).get("policy", {}).get("status")
                    == "running"
                ):
                    preparation["routing"]["policy"].update(
                        status="failed",
                        exit_code=130,
                        outcome="interrupted",
                        decision="needs_clarification",
                    )
                try:
                    (artifact_io / "coordinator").mkdir(exist_ok=True)
                except OSError:
                    pass
                fail_preparation(
                    preparation,
                    "interrupted",
                    f"Bead {bead_id} Run Preparer was interrupted",
                )
                seal_json(artifact_io / "preparation.json", preparation)
            print(f"artifact root: {artifact}", flush=True)
        print(f"afk run: Bead {bead_id} preparation interrupted", file=sys.stderr)
        return 130
    finally:
        close_resources(open_fds, leases)


def coordinator_terminal(output_path):
    """Return only value-safe terminal facts from a valid sealed output."""
    try:
        output = validate_coordinator_output(json.loads(output_path.read_text()))
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None, None
    decision = output.get("decision") if output["outcome"] == "completed" else None
    return output["outcome"], decision


def preflight_terminal(output_path, preflight_input):
    """Return only value-safe routing facts from a valid sealed Preflight output."""
    try:
        output = validate_preflight_output(
            json.loads(output_path.read_text()), preflight_input
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None, "pause", []
    return output["outcome"], output["decision"], output["requests"]


def execute_preflight(bead_id, artifact, preparation, preflight_input):
    """Run and record the complete pre-Coordinator acceptance gate."""
    progress(f"starting acceptance-evidence preflight for Bead {bead_id}")
    (artifact / "preflight").rmdir()
    try:
        preflight_code, interrupted = run_foreground(
            preparation["preflight"]["command"], Path(__file__).parent
        )
    except OSError:
        (artifact / "preflight").mkdir(exist_ok=True)
        preparation["preflight"].update(
            status="failed", exit_code=None, outcome=None, decision="pause"
        )
        fail_preparation(
            preparation,
            "preflight_launch",
            f"acceptance-evidence preflight could not be started for Bead {bead_id}",
        )
        seal_json(artifact / "preparation.json", preparation)
        raise PreparationError(
            f"acceptance-evidence preflight could not be started for Bead {bead_id}"
        )

    outcome, decision, requests = preflight_terminal(
        artifact / "preflight" / "output.json", preflight_input
    )
    if interrupted:
        outcome = "interrupted"
        decision = "pause"
        requests = []
    preparation["preflight"].update(
        status=(
            "completed"
            if not interrupted and preflight_code == 0 and outcome == "completed"
            else "failed"
        ),
        exit_code=130 if interrupted else preflight_code,
        outcome=outcome,
        decision=decision,
    )
    for classified in requests:
        progress(
            f"preflight request {classified['index']}: "
            f"{classified['category']} -> {classified['route']}; "
            f"request={json.dumps(classified['request'])}"
        )
    progress(f"preflight terminal decision for Bead {bead_id}: {decision}")
    if not interrupted and preflight_code == 0 and outcome == "completed":
        if decision == "proceed":
            return 0
        preparation["preparation_status"] = "paused"
        preparation["timestamps"]["finished_at"] = timestamp()
        seal_json(artifact / "preparation.json", preparation)
        return 1

    preparation["preparation_status"] = "failed"
    preparation["timestamps"]["finished_at"] = timestamp()
    preparation["errors"].append(
        {
            "category": "interrupted" if interrupted else "preflight",
            "message": (
                f"acceptance-evidence preflight "
                f"{'was interrupted' if interrupted else 'failed'} for Bead {bead_id}"
            ),
        }
    )
    seal_json(artifact / "preparation.json", preparation)
    return 130 if interrupted else 1


def execute_acceptance_routing(bead_id, artifact, preparation, planner_input):
    """Run Planner and deterministic policy as the only semantic admission path."""
    progress(f"starting Acceptance Routing for Bead {bead_id}")
    planner = preparation["routing"]["planner"]
    (artifact / "planner").rmdir()
    try:
        planner_code, interrupted = run_foreground(
            planner["command"], Path(__file__).parent
        )
    except OSError:
        (artifact / "planner").mkdir(exist_ok=True)
        planner.update(status="failed", exit_code=None, outcome=None)
        return fail_routing(
            bead_id, artifact, preparation, "planner_launch", None, False
        )
    planner_output = planner_terminal(
        artifact / "planner" / "output.json", planner_input
    )
    planner.update(
        status=(
            "completed"
            if not interrupted and planner_code == 0 and planner_output is not None
            else "failed"
        ),
        exit_code=130 if interrupted else planner_code,
        outcome=(
            "interrupted"
            if interrupted
            else (planner_output["outcome"] if planner_output is not None else None)
        ),
    )
    if interrupted or planner_code != 0 or planner_output is None:
        return fail_routing(
            bead_id,
            artifact,
            preparation,
            "interrupted" if interrupted else "planner",
            130 if interrupted else 1,
            interrupted,
        )

    policy_input = {
        "schema_version": 2,
        "planner_input": planner_input,
        **(
            {"routing": planner_output["routing"]}
            if planner_output["plan"] is None
            else {"plan": planner_output["plan"]}
        ),
    }
    write_json(artifact / "policy-input.json", policy_input)
    policy = preparation["routing"]["policy"]
    policy["status"] = "running"
    seal_json(artifact / "preparation.json", preparation)
    (artifact / "policy").rmdir()
    try:
        policy_code, interrupted = run_foreground(
            policy["command"], Path(__file__).parent
        )
    except OSError:
        (artifact / "policy").mkdir(exist_ok=True)
        policy.update(status="failed", exit_code=None, outcome=None, decision=None)
        return fail_routing(
            bead_id, artifact, preparation, "policy_launch", None, False
        )
    policy_output = policy_terminal(
        artifact / "policy" / "output.json", planner_input, policy_input
    )
    decision = policy_output.get("decision") if policy_output is not None else None
    policy.update(
        status=(
            "completed" if not interrupted and policy_output is not None else "failed"
        ),
        exit_code=130 if interrupted else policy_code,
        outcome=(
            "interrupted"
            if interrupted
            else (policy_output["outcome"] if policy_output is not None else None)
        ),
        decision=("needs_clarification" if interrupted else decision),
    )
    if interrupted or policy_output is None or policy_code not in {0, 1}:
        return fail_routing(
            bead_id,
            artifact,
            preparation,
            "interrupted" if interrupted else "policy",
            130 if interrupted else 1,
            interrupted,
        )

    progress(f"Acceptance Routing decision for Bead {bead_id}: {decision}")
    if policy_code == 0 and decision == "direct":
        return 0

    preparation["preparation_status"] = "routed" if decision == "accepted" else decision
    preparation["timestamps"]["finished_at"] = timestamp()
    seal_json(artifact / "preparation.json", preparation)
    return 1


def planner_terminal(path, planner_input):
    """Revalidate the Planner's canonical route before policy handoff."""
    try:
        return validate_planner_output(planner_input, json.loads(path.read_text()))
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def policy_terminal(path, planner_input, policy_input):
    """Accept only canonical success or the bounded v2 non-admission union."""
    try:
        return validate_policy_output(
            planner_input, policy_input, json.loads(path.read_text())
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def fail_routing(bead_id, artifact, preparation, category, code, interrupted):
    preparation["preparation_status"] = "failed"
    preparation["timestamps"]["finished_at"] = timestamp()
    preparation["errors"].append(
        {
            "category": category,
            "message": (
                f"Acceptance Routing "
                f"{'was interrupted' if interrupted else 'failed'} for Bead {bead_id}"
            ),
        }
    )
    seal_json(artifact / "preparation.json", preparation)
    return 130 if interrupted else (code if isinstance(code, int) else 1)


def run_foreground(command, cwd):
    """Run one visible child and let Ctrl-C reach its durable shutdown path."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=worker_environment(),
        start_new_session=True,
    )
    try:
        return normalize_exit_code(process.wait()), False
    except KeyboardInterrupt:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            terminate(process)
        return 130, True


def normalize_exit_code(exit_code):
    return exit_code if exit_code >= 0 else 1


def terminal_exit_code(coordinator_code, outcome, decision):
    """Treat exhausted work and invalid successful output as unsuccessful runs."""
    if coordinator_code != 0:
        return coordinator_code
    if outcome == "completed" and decision == "stop":
        return 0
    return 1


def publish_configured_run(bead_id, artifact, config):
    """Publish one already-sealed terminal Run when the seam is configured."""
    if config.get("publication") is None:
        return True
    progress(f"publishing terminal Run for Bead {bead_id}")
    publication = publish_terminal_run(artifact, config["publication"])
    progress(
        f"publication outcome for Bead {bead_id}: "
        f"{publication['admission_outcome'] or publication['error_category']}"
    )
    return publication["status"] == "succeeded"


def publish_terminal_run(source, config):
    """Export one sealed Run, invoke Admission, and seal private result evidence."""
    from afk_export import ExportError, ExportUsageError, export_run

    started_at = timestamp()
    exit_code = None
    admission_outcome = None
    error_category = None
    stdout_path = source / "publication.stdout"
    stderr_path = source / "publication.stderr"
    try:
        with tempfile.TemporaryDirectory(prefix="afk-publication-") as temporary:
            bundle = Path(temporary) / "bundle"
            try:
                exported = export_run(source, bundle, schema_version=2)
            except (
                ExportError,
                ExportUsageError,
                OSError,
                TypeError,
                ValueError,
                KeyError,
            ):
                error_category = "export_failed"
            else:
                exit_code, admission_outcome, error_category = invoke_admission(
                    bundle,
                    exported["identity"],
                    config,
                    stdout_path,
                    stderr_path,
                )
    except OSError:
        error_category = "temporary_storage"

    if not stdout_path.exists():
        stdout_path.write_text("")
    if not stderr_path.exists():
        stderr_path.write_text("")
    result = {
        "schema_version": 1,
        "status": "succeeded" if error_category is None else "failed",
        "admission_outcome": admission_outcome,
        "started_at": started_at,
        "finished_at": timestamp(),
        "process": {"exit_code": exit_code},
        "error_category": error_category,
    }
    seal_json(source / "publication.json", result)
    return result


def invoke_admission(bundle, expected_identity, config, stdout_path, stderr_path):
    """Run one Admission adapter and classify only its versioned result."""
    command = replace_argument(
        config["command"], PUBLICATION_BUNDLE_PLACEHOLDER, str(bundle)
    )
    try:
        facts = run_command(
            command,
            Path(__file__).parent,
            config["timeout_seconds"],
            stdout_path,
            stderr_path,
        )
    except OSError:
        return None, None, "publication_io"
    exit_code = facts["exit_code"]
    if facts["interrupted"]:
        return exit_code, None, "admission_interrupted"
    if facts["timed_out"]:
        return exit_code, None, "admission_timeout"
    if facts["error"]:
        return exit_code, None, "admission_launch"
    try:
        if stdout_path.stat().st_size > MAX_ADMISSION_OUTPUT_BYTES:
            return exit_code, None, "admission_protocol"
        stdout = stdout_path.read_text()
    except (OSError, UnicodeDecodeError):
        return exit_code, None, "admission_protocol"
    admission_outcome, error_category = admission_terminal(
        stdout, exit_code, expected_identity
    )
    return exit_code, admission_outcome, error_category


def admission_terminal(stdout, exit_code, expected_identity):
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "admission_protocol"
    outcome = value.get("outcome") if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None, "admission_protocol"
    if exit_code == 0 and outcome in {"accepted", "replayed"}:
        if value.get("identity") != expected_identity:
            return None, "admission_protocol"
        return outcome, None
    if exit_code != 0 and outcome in {"conflict", "rejected"}:
        return outcome, "admission_rejected"
    return None, "admission_protocol"


def load_config(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PreparationError(
            f"configuration {path} cannot be read as JSON"
        ) from error
    core = {
        "schema_version",
        "beads_workspace",
        "run_root",
        "worktree_root",
        "assignment",
        "coordinator",
        "projects",
    }
    legacy = core | {"classification_store"}
    capability = core | {"acceptance_routing"}
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or frozenset(value)
        not in {
            frozenset(base | optional)
            for base in (legacy, capability)
            for optional in (
                set(),
                {"publication"},
                {"attestation"},
                {"publication", "attestation"},
            )
        }
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
    if "classification_store" in value:
        value["classification_store"] = absolute_path(
            value["classification_store"], "configuration classification_store"
        )
        if (
            value["classification_store"].exists()
            and not value["classification_store"].is_dir()
        ):
            raise PreparationError(
                f"configured classification_store {value['classification_store']} is not a directory"
            )
    if "acceptance_routing" in value:
        validate_acceptance_routing(value["acceptance_routing"])
    validate_assignment_defaults(value["assignment"])
    validate_coordinator(value["coordinator"])
    if "publication" in value:
        validate_publication(value["publication"])
    if "attestation" in value:
        validate_attestation(value["attestation"])
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
    if value["command"].count(ASSIGNMENT_PATH_PLACEHOLDER) != 1 or any(
        ASSIGNMENT_PATH_PLACEHOLDER in item and item != ASSIGNMENT_PATH_PLACEHOLDER
        for item in value["command"]
    ):
        raise PreparationError(
            "assignment command must contain exactly one {assignment_path} argument"
        )
    if value["command"][0] == ASSIGNMENT_PATH_PLACEHOLDER:
        raise PreparationError(
            "assignment command {assignment_path} cannot be the executable"
        )
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


def validate_acceptance_routing(value):
    if not isinstance(value, dict) or set(value) != {"catalog", "timeout_seconds"}:
        raise PreparationError("configuration acceptance_routing is malformed")
    positive(value["timeout_seconds"], "acceptance_routing timeout_seconds")
    try:
        value["catalog"] = validate_catalog(value["catalog"], 2)
    except (TypeError, ValueError) as error:
        raise PreparationError(
            "configuration acceptance_routing catalog is malformed"
        ) from error


def validate_publication(value):
    if not isinstance(value, dict) or set(value) != {"command", "timeout_seconds"}:
        raise PreparationError("configuration publication is malformed")
    argv(value["command"], "publication command")
    if value["command"].count(PUBLICATION_BUNDLE_PLACEHOLDER) != 1 or any(
        PUBLICATION_BUNDLE_PLACEHOLDER in item
        and item != PUBLICATION_BUNDLE_PLACEHOLDER
        for item in value["command"]
    ):
        raise PreparationError(
            "publication command must contain exactly one {bundle_path} argument"
        )
    if value["command"][0] == PUBLICATION_BUNDLE_PLACEHOLDER:
        raise PreparationError("publication {bundle_path} cannot be the executable")
    positive(value["timeout_seconds"], "publication timeout_seconds")


def validate_attestation(value):
    try:
        value["result_root"] = attestation_result_root(value)
    except (TypeError, ValueError) as error:
        raise PreparationError(str(error)) from error


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
        "evidence",
        "timeout_seconds",
    }:
        raise PreparationError(f"project:{slug} validation is malformed")
    argv(validation["command"], f"project:{slug} validation command")
    if (
        not isinstance(validation["evidence"], str)
        or not validation["evidence"].strip()
        or len(validation["evidence"]) > 2000
    ):
        raise PreparationError(
            f"project:{slug} validation evidence must be bounded nonempty text"
        )
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


def require_agent_readiness(bead_id, labels):
    """Enforce the narrow triage admission contract before repository work."""
    agent_count = labels.count("ready-for-agent")
    human_count = labels.count("ready-for-human")
    if agent_count == 1 and human_count == 0:
        return
    if human_count and agent_count:
        reason = "ready-for-agent conflicts with ready-for-human"
    elif human_count:
        reason = "ready-for-human is present"
    elif agent_count == 0:
        reason = "ready-for-agent is missing"
    else:
        reason = "ready-for-agent must appear exactly once"
    raise PreparationError(
        f"Bead {bead_id} is not ready for an agent ({reason}); "
        "update its triage labels to exactly one ready-for-agent and no "
        "ready-for-human, then retry"
    )


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


def acceptance_preflight(bead, validation, coordinator):
    request = {
        "schema_version": 1,
        "source": bead["source"],
        "title": bead["title"],
        "acceptance_criteria": bead.get("acceptance_criteria"),
        "evidence_catalog": [
            {
                "category": "repository_validation",
                "route": "repository validation",
                "can_prove": (
                    f"{validation['evidence']} Exact argv: "
                    f"{json.dumps(validation['command'])}."
                ),
            },
            {
                "category": "pipeline_evidence",
                "route": "AFK committed change and Review",
                "can_prove": (
                    "Committed implementation facts, Git transition facts, and "
                    "findings from AFK Review."
                ),
            },
            {
                "category": "operator_external",
                "route": "operator handoff",
                "can_prove": (
                    "Host, deployment, live service, HTTP, and other checks outside "
                    "the prepared repository Run."
                ),
            },
        ],
        "timeout_seconds": coordinator["agent_timeout_seconds"],
    }
    if any(len(item["can_prove"]) > 4000 for item in request["evidence_catalog"]):
        raise PreparationError("configured acceptance-evidence catalog is too large")
    return request


def acceptance_routing_request(bead_id, bead, routing):
    """Freeze the exact capability catalog and source fields used for admission."""
    try:
        return validate_planner_input(
            {
                "schema_version": 2,
                "parent": {
                    "id": bead_id,
                    "title": bead["title"],
                    "description": bead.get("description") or "",
                    "acceptance_criteria": bead.get("acceptance_criteria"),
                    "labels": bead["labels"],
                },
                "catalog": routing["catalog"],
                "timeout_seconds": routing["timeout_seconds"],
            }
        )
    except (TypeError, ValueError) as error:
        raise PreparationError(
            f"Bead {bead_id} cannot be represented for Acceptance Routing"
        ) from error


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


def assignment_command(command, assignment_path):
    """Replace one validated argv element; no shell interpolation is involved."""
    return replace_argument(command, ASSIGNMENT_PATH_PLACEHOLDER, str(assignment_path))


def replace_argument(command, placeholder, value):
    """Replace one previously validated argv placeholder without a shell."""
    return [value if item == placeholder else item for item in command]


def ensure_branch_available(bead_id, repository, branch):
    """Reject exact, ancestor, or descendant collisions in Git's ref namespace."""
    target = f"refs/heads/{branch}"
    listed = git_result(repository, "for-each-ref", "--format=%(refname)", "refs/heads")
    if listed.returncode != 0:
        raise PreparationError(
            f"Bead {bead_id} branch namespace could not be inspected"
        )
    refs = listed.stdout.splitlines()
    if any(
        ref == target or ref.startswith(f"{target}/") or target.startswith(f"{ref}/")
        for ref in refs
    ):
        raise PreparationError(
            f"Bead {bead_id} branch destination {branch!r} conflicts with an existing branch"
        )


def ensure_destination_layout(bead_id, config, repository, artifact, worktree):
    run_root = config["run_root"]
    worktree_root = config["worktree_root"]
    classification_store = config.get("classification_store")
    if (
        run_root == worktree_root
        or run_root in worktree_root.parents
        or worktree_root in run_root.parents
    ):
        raise PreparationError(
            f"Bead {bead_id} Run and worktree roots overlap unsafely"
        )
    if classification_store is not None:
        for other, fact in (
            (run_root, "Run root"),
            (worktree_root, "worktree root"),
            (repository, "selected repository"),
            (config["beads_workspace"], "central Beads workspace"),
        ):
            if (
                classification_store == other
                or classification_store in other.parents
                or other in classification_store.parents
            ):
                raise PreparationError(
                    f"Bead {bead_id} classification store overlaps the {fact} unsafely"
                )
    for root, path, fact in (
        (run_root, artifact, "artifact"),
        (worktree_root, worktree, "worktree"),
    ):
        if root == repository or repository in root.parents:
            raise PreparationError(
                f"Bead {bead_id} {fact} root {root} is inside the selected repository"
            )
        if path == repository or repository in path.parents:
            raise PreparationError(
                f"Bead {bead_id} {fact} destination {path} is inside the selected repository"
            )


def acquire_directory(path, create):
    if create:
        path.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, DIRECTORY_FLAGS)
    except OSError as error:
        raise PreparationError(f"configured directory {path} is unsafe") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        require_identity(path, descriptor, "configured directory")
    except Exception:
        os.close(descriptor)
        raise
    return (path, descriptor)


def lease_for(leases, path):
    return next(lease for lease in leases if lease[0] == path)


def prepare_parent(lease, bead_id, run_id, fact):
    root, root_fd = lease
    try:
        os.mkdir(bead_id, dir_fd=root_fd)
    except FileExistsError:
        pass
    try:
        parent_fd = os.open(bead_id, DIRECTORY_FLAGS, dir_fd=root_fd)
    except OSError as error:
        raise PreparationError(
            f"Bead {bead_id} {fact} parent {root / bead_id} is unsafe"
        ) from error
    try:
        require_identity(root / bead_id, parent_fd, f"{fact} parent")
        try:
            os.stat(run_id, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return parent_fd
        raise PreparationError(
            f"Bead {bead_id} {fact} destination {root / bead_id / run_id} already exists"
        )
    except Exception:
        os.close(parent_fd)
        raise


def require_identity(path, descriptor, fact):
    try:
        visible = os.stat(path, follow_symlinks=False)
        opened = os.fstat(descriptor)
    except OSError as error:
        raise PreparationError(f"{fact} {path} changed during preparation") from error
    if not os.path.isdir(path) or (visible.st_dev, visible.st_ino) != (
        opened.st_dev,
        opened.st_ino,
    ):
        raise PreparationError(f"{fact} {path} changed during preparation")


def close_resources(descriptors, leases):
    while descriptors:
        descriptor = descriptors.pop()
        try:
            os.close(descriptor)
        except OSError:
            pass
    while leases:
        _, descriptor = leases.pop()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass


def git_result(repository, *arguments):
    return subprocess.run(
        ["git", *arguments], cwd=repository, text=True, capture_output=True, check=False
    )


def fail_preparation(preparation, category, message):
    preparation["preparation_status"] = "failed"
    preparation["timestamps"]["finished_at"] = timestamp()
    preparation["errors"].append({"category": category, "message": message})


def worker_environment():
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "TERM", "TZ"}
    exact = {
        "AFK_PLAN_AGENT_COMMAND",
        "AFK_PREFLIGHT_AGENT_COMMAND",
        "AFK_REVIEW_AGENT_COMMAND",
        "AFK_ASSESS_AGENT_COMMAND",
        "AFK_RESPOND_AGENT_COMMAND",
    }
    return {
        name: value
        for name, value in os.environ.items()
        if name in allowed or name in exact or name.startswith("LC_")
    }
