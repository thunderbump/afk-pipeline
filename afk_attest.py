"""Caller-side human attestation and retry-safe child reconciliation."""

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from afk_complete.contract import load_result, validate_subject
from afk_config import attestation_result_root
from afk_plan_publish.__main__ import (
    Beads,
    dependency_pairs,
    validate_child,
    validate_parent,
)
from afk_plan_publish.__main__ import load_request as load_publication_request
from afk_plan_publish.contract import (
    external_reference,
    load_accepted_plan,
    validate_published_output,
)
from afk_run import DEFAULT_CONFIG, SAFE_ID
from afk_runtime import timestamp


class AttestationError(Exception):
    def __init__(self, message, category="validation"):
        super().__init__(message)
        self.category = category


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "attest",
        help="attest one published human child and close only that child",
        usage=(
            "afk attest CHILD_ID --publication DIRECTORY --subject FIELD=VALUE "
            "--evidence VALUE [--accept] [--config PATH]"
        ),
    )
    parser.add_argument("child_id", metavar="CHILD_ID")
    parser.add_argument("--publication", required=True, type=Path)
    parser.add_argument(
        "--subject", required=True, action="append", metavar="FIELD=VALUE"
    )
    parser.add_argument("--evidence", required=True, action="append", metavar="VALUE")
    parser.add_argument("--accept", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


def attest(arguments):
    lock_holder = []
    try:
        return _attest(arguments, lock_holder)
    finally:
        while lock_holder:
            os.close(lock_holder.pop())


def _attest(arguments, lock_holder):
    attempt = None
    started_at = None
    started = None
    adapter = None
    try:
        scope = load_scope(arguments)
        preview(scope, arguments.evidence)
        if not arguments.accept:
            if not sys.stdin.isatty():
                raise AttestationError(
                    "interactive confirmation requires a terminal; use --accept for noninteractive callers",
                    "confirmation",
                )
            try:
                answer = input("Approve this exact scope? [yes/no] ")
            except EOFError as error:
                raise AttestationError(
                    "confirmation requires input; use --accept for noninteractive callers",
                    "confirmation",
                ) from error
            if answer.strip().lower() not in {"y", "yes"}:
                print("Attestation declined; no result or Beads mutation was created.")
                return 1

        # The Completion Record check, attachment, and close are separate Beads
        # operations. Serialize them across the Beads workspace so that distinct
        # attempts (including attempts with different evidence or result roots)
        # cannot concurrently attest the same child from stale observations.
        # This is deliberately broader than a per-attempt lock: attempt
        # identities include subject and evidence.
        descriptor = acquire_reconciliation_lock(scope["beads_workspace"])
        lock_holder.append(descriptor)
        attempt_path, record, attempt_descriptor = open_attempt(
            scope, arguments.evidence
        )
        lock_holder.append(attempt_descriptor)
        attempt = attempt_path
        started_at = timestamp()
        started = time.monotonic()
        if completed_attempt(scope, record, attempt, attempt_descriptor):
            print(f"attestation result: {attempt}")
            return 0
        adapter = Beads(trusted_beads_request(scope))
        reconcile(scope, record, attempt, attempt_descriptor, adapter)
        finish(
            attempt,
            attempt_descriptor,
            scope,
            record,
            adapter,
            started_at,
            started,
            "completed",
            "attested",
            None,
        )
        print(f"attestation result: {attempt}")
        return 0
    except AttestationError as error:
        if attempt is not None:
            finish(
                attempt,
                attempt_descriptor,
                scope,
                record,
                adapter,
                started_at,
                started,
                "failed",
                "open",
                error.category,
                str(error),
            )
            print(f"attestation result: {attempt}")
            code = 1
        else:
            code = 2
        print(f"afk attest: {error}", file=sys.stderr)
        return code
    except KeyboardInterrupt:
        if attempt is not None:
            finish(
                attempt,
                attempt_descriptor,
                scope,
                record,
                adapter,
                started_at,
                started,
                "interrupted",
                "open",
                "interrupted",
                "attestation reconciliation was interrupted",
            )
            print(f"attestation result: {attempt}")
        print("afk attest: interrupted", file=sys.stderr)
        return 130
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        if attempt is not None:
            finish(
                attempt,
                attempt_descriptor,
                scope,
                record,
                adapter,
                started_at,
                started,
                "failed",
                "open",
                "beads_operation" if adapter is not None else "validation",
                str(error),
            )
            print(f"attestation result: {attempt}")
            code = 1
        else:
            code = 2
        print(f"afk attest: {error}", file=sys.stderr)
        return code


def load_scope(arguments):
    if not SAFE_ID.fullmatch(arguments.child_id):
        raise AttestationError("CHILD_ID is invalid")
    publication = arguments.publication
    if not publication.is_absolute() or not publication.is_dir():
        raise AttestationError("--publication must be an absolute existing directory")
    publication = publication.resolve()
    config = load_config(arguments.config)
    publisher_request = load_publication_request(publication / "input.json")
    if publisher_request["beads_workspace"] != config["beads_workspace"]:
        raise AttestationError(
            "publication does not target the configured Beads workspace"
        )
    planner_input, acceptance = load_accepted_plan(
        publisher_request["acceptance_directory"]
    )
    output = validate_published_output(
        json.loads((publication / "output.json").read_text()),
        planner_input["parent"]["id"],
        acceptance,
    )
    mapping = next(
        (item for item in output["children"] if item["bead_id"] == arguments.child_id),
        None,
    )
    if mapping is None:
        raise AttestationError("child is not in the explicit publication")
    child = next(
        item
        for item in acceptance["plan"]["children"]
        if item["local_id"] == mapping["local_id"]
    )
    if child["execution"] != "human" or child["evidence_route"] != "human_attestation":
        raise AttestationError("child does not support human attestation")
    subject = subject_values(arguments.subject)
    required = set(child["handoff"]["subject_fields"])
    if set(subject) != required:
        raise AttestationError("subject fields do not match the frozen handoff")
    root = config["result_root"]
    protected = (
        publisher_request["acceptance_directory"],
        publication,
        config["beads_workspace"],
    )
    if any(overlaps(root, item) for item in protected):
        raise AttestationError("attestation result root overlaps protected evidence")
    return {
        "publication": publication,
        "publisher_request": publisher_request,
        "planner_input": planner_input,
        "acceptance": acceptance,
        "publication_output": output,
        "mapping": mapping,
        "child": child,
        "subject": subject,
        "beads_workspace": config["beads_workspace"],
        "result_root": root,
    }


def load_config(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AttestationError(
            f"configuration {path} cannot be read as JSON"
        ) from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("beads_workspace"), str)
        or "attestation" not in value
    ):
        raise AttestationError("configuration has no valid attestation result root")
    beads = Path(value["beads_workspace"])
    if not beads.is_absolute() or not beads.is_dir():
        raise AttestationError("configured Beads workspace is unavailable")
    try:
        root = attestation_result_root(value["attestation"])
    except ValueError as error:
        raise AttestationError(str(error)) from error
    return {"beads_workspace": beads.resolve(), "result_root": root}


def subject_values(values):
    subject = {}
    for value in values:
        field, separator, item = value.partition("=")
        if not separator or field in subject:
            raise AttestationError("each --subject must be one unique FIELD=VALUE")
        subject[field] = item
    try:
        return validate_subject(subject, "subject")
    except ValueError as error:
        raise AttestationError(str(error)) from error


def preview(scope, evidence):
    child = scope["child"]
    value = {
        "parent": scope["planner_input"]["parent"]["id"],
        "plan_digest": scope["acceptance"]["plan"]["plan_sha256"],
        "child": scope["mapping"]["bead_id"],
        "authority": child["handoff"]["authority"],
        "criteria": child["criteria"],
        "subject": scope["subject"],
        "evidence": evidence,
    }
    print("Frozen human-attestation scope:")
    print(json.dumps(value, indent=2))


def open_attempt(scope, evidence):
    identity = {
        "publication": str(scope["publication"]),
        "child": scope["mapping"]["bead_id"],
        "plan": scope["acceptance"]["plan"]["plan_sha256"],
        "subject": scope["subject"],
        "evidence": evidence,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    attempt = scope["result_root"] / f"{scope['mapping']['bead_id']}-{digest[:16]}"
    try:
        attempt.mkdir()
    except FileExistsError:
        pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(attempt, flags)
    except OSError as error:
        raise AttestationError(
            "existing attestation attempt is not a safe directory"
        ) from error
    if attempt.resolve().parent != scope["result_root"]:
        os.close(descriptor)
        raise AttestationError("attestation attempt escapes its configured root")
    try:
        reject_symlink_tree(attempt)

        try:
            request = read_json_at(descriptor, "request.json")
        except FileNotFoundError:
            request = None
        except json.JSONDecodeError:
            # An interrupted initial write is safe to replace only while no
            # later artifact can have relied on it.
            if any(
                path.name not in {"request.json", "request.json.tmp"}
                for path in attempt.iterdir()
            ):
                raise AttestationError(
                    "existing attestation attempt has a corrupt request"
                )
        if request is not None:
            if (
                not isinstance(request, dict)
                or request.get("identity") != identity
                or not isinstance(request.get("record"), dict)
            ):
                if any(
                    path.name not in {"request.json", "request.json.tmp"}
                    for path in attempt.iterdir()
                ):
                    raise AttestationError(
                        "existing attestation attempt conflicts with this approval"
                    )
                request = None
            else:
                return attempt, request["record"], descriptor
        child = scope["child"]
        record = {
            "schema_version": 1,
            "child": scope["mapping"]["bead_id"],
            "parent_plan": scope["acceptance"]["plan"]["plan_sha256"],
            "outcome": "satisfied",
            "producer": {
                "kind": "human_attestation",
                "identity": child["handoff"]["authority"],
            },
            "criteria": child["criteria"],
            "subject": scope["subject"],
            "evidence": evidence,
            "accepted_at": timestamp(),
        }
        seal_json_at(
            descriptor,
            "request.json",
            {"schema_version": 1, "identity": identity, "record": record},
        )
        return attempt, record, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def reject_symlink_tree(root):
    """Reject pre-existing indirections before any artifact is read or written."""
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise AttestationError(
                        "attestation attempt contains an unsafe symbolic link"
                    )
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))


def read_json_at(directory, name):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory)
    with os.fdopen(descriptor, encoding="utf-8") as source:
        return json.load(source)


def seal_json_at(directory, name, value):
    """Atomically replace one artifact without following existing file links."""
    temporary = f"{name}.tmp"
    try:
        os.unlink(temporary, dir_fd=directory)
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(value, destination, indent=2)
            destination.write("\n")
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        raise
    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)


def attempt_view(directory):
    """Expose an anchored attempt directory to existing path-based validators."""
    return Path(f"/proc/self/fd/{directory}")


def acquire_reconciliation_lock(beads_workspace):
    """Serialize Beads reconciliation without adding a mutable lock artifact."""
    descriptor = os.open(beads_workspace, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def trusted_beads_request(scope):
    """Build the mutation adapter from host authority, never Publisher evidence."""
    command = ["bd"]
    override = os.environ.get("AFK_ATTEST_BEADS_COMMAND")
    if override is not None:
        try:
            command = json.loads(override)
        except json.JSONDecodeError as error:
            raise AttestationError("AFK_ATTEST_BEADS_COMMAND is invalid") from error
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise AttestationError("AFK_ATTEST_BEADS_COMMAND is invalid")
    return {
        "command": command,
        "beads_workspace": scope["beads_workspace"],
        "timeout_seconds": 30,
    }


def completed_attempt(scope, record, attempt, attempt_descriptor):
    """Recognize an exact successful result before consulting mutable Beads state."""
    try:
        output = read_json_at(attempt_descriptor, "output.json")
    except FileNotFoundError:
        return False
    except (OSError, json.JSONDecodeError) as error:
        raise AttestationError("existing attestation result is malformed") from error
    if not isinstance(output, dict):
        raise AttestationError("existing attestation result is malformed")
    if output.get("outcome") != "completed" or output.get("decision") != "attested":
        return False
    expected = {
        "source": {"kind": "bead", "id": scope["mapping"]["bead_id"]},
        "plan_sha256": scope["acceptance"]["plan"]["plan_sha256"],
        "record": record,
        "completion": "completion/output.json",
        "error_category": None,
        "error": None,
    }
    if any(output.get(name) != value for name, value in expected.items()):
        raise AttestationError("existing successful attestation does not match")
    try:
        load_result(
            attempt_view(attempt_descriptor) / "completion",
            scope["child"],
            scope["mapping"]["bead_id"],
            scope["acceptance"]["acceptance_sha256"],
            scope["acceptance"]["plan"]["plan_sha256"],
            scope["publisher_request"]["acceptance_directory"],
            scope["publication"],
            scope["subject"],
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AttestationError(
            "existing successful attestation has invalid Completion evidence"
        ) from error
    return True


def reconcile(scope, record, attempt, attempt_descriptor, adapter):
    parent_id = scope["planner_input"]["parent"]["id"]
    parent = adapter.one("show", parent_id, "--json", "--readonly")
    try:
        validate_parent(parent, scope["planner_input"]["parent"])
    except ValueError as error:
        raise AttestationError(str(error), "current_state") from error
    if parent.get("status") not in {"open", "in_progress"}:
        raise AttestationError("frozen parent is no longer active", "current_state")
    child_id = scope["mapping"]["bead_id"]
    issue = adapter.one("show", child_id, "--json", "--readonly")
    child = scope["child"]
    plan = scope["acceptance"]["plan"]
    try:
        validate_child(
            issue,
            parent_id,
            plan,
            child,
            external_reference(plan["plan_sha256"], child["local_id"]),
        )
    except ValueError as error:
        raise AttestationError(str(error), "current_state") from error
    required_dependencies = {parent_id} | {
        next(
            item["bead_id"]
            for item in scope["publication_output"]["children"]
            if item["local_id"] == local_id
        )
        for local_id in child["depends_on"]
    }
    controlled = {
        pair
        for pair in dependency_pairs(issue)
        if pair[1] in {"parent-child", "blocks"}
    }
    required_relationships = {(parent_id, "parent-child")} | {
        (item, "blocks") for item in required_dependencies - {parent_id}
    }
    if controlled != required_relationships:
        raise AttestationError(
            "current child dependencies do not match the frozen Plan", "current_state"
        )
    blockers = required_dependencies - {parent_id}
    if blockers:
        observed = adapter.many("show", *sorted(blockers), "--json", "--readonly")
        if {
            item.get("id") for item in observed if item.get("status") == "closed"
        } != blockers:
            raise AttestationError(
                "child dependencies are not ready", "dependency_readiness"
            )

    raw_comments = adapter.many("comments", child_id, "--json", "--readonly")
    comments = []
    for item in raw_comments:
        text = (
            item
            if isinstance(item, str)
            else item.get("text")
            if isinstance(item, dict)
            else None
        )
        if not isinstance(text, str):
            raise AttestationError(
                "current child comments are malformed", "current_state"
            )
        comments.append(text)
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
    status = issue.get("status")
    attached = encoded in comments
    for comment in comments:
        try:
            candidate = json.loads(comment)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(candidate, dict)
            and candidate.get("schema_version") == 1
            and candidate.get("child") == child_id
            and candidate.get("parent_plan") == plan["plan_sha256"]
            and comment != encoded
        ):
            raise AttestationError(
                "child has a conflicting Completion Record", "conflict"
            )
    if status == "closed" and not attached:
        raise AttestationError(
            "child is already closed without this Completion Record", "conflict"
        )
    if status not in {"open", "in_progress", "closed"}:
        raise AttestationError("child is not in a supported state", "current_state")

    anchored_attempt = attempt_view(attempt_descriptor)
    completion = anchored_attempt / "completion"
    completion_stage = anchored_attempt / "completion.in-progress"
    if completion.is_symlink() or completion_stage.is_symlink():
        raise AttestationError(
            "Completion Record result path is not a safe directory",
            "completion_validation",
        )
    # afk_complete requires a new result directory. A directory without its
    # atomically sealed output is only an interrupted validator run, not proof
    # that validation completed.
    if completion.exists() and not (completion / "output.json").is_file():
        retain_partial_completion(attempt_descriptor, "completion")
    if not completion.exists():
        retain_partial_completion(attempt_descriptor, "completion.in-progress")
        seal_json_at(
            attempt_descriptor,
            "completion.json",
            {
                "schema_version": 1,
                "acceptance_directory": str(
                    scope["publisher_request"]["acceptance_directory"]
                ),
                "publication_directory": str(scope["publication"]),
                "expected_subject": scope["subject"],
                "record": record,
            },
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "afk_complete",
                str(anchored_attempt / "completion.json"),
                str(completion_stage),
            ],
            cwd=Path(__file__).parent,
            text=True,
            capture_output=True,
            check=False,
            pass_fds=(attempt_descriptor,),
        )
        seal_json_at(
            attempt_descriptor,
            "validator-process.json",
            {
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
        )
        if completed.returncode != 0:
            raise AttestationError(
                "Completion Record validation failed", "completion_validation"
            )
        if not (completion_stage / "output.json").is_file():
            raise AttestationError(
                "Completion Record validation did not seal a result",
                "completion_validation",
            )
        os.rename(
            "completion.in-progress",
            "completion",
            src_dir_fd=attempt_descriptor,
            dst_dir_fd=attempt_descriptor,
        )
    try:
        output = load_result(
            completion,
            child,
            child_id,
            scope["acceptance"]["acceptance_sha256"],
            plan["plan_sha256"],
            scope["publisher_request"]["acceptance_directory"],
            scope["publication"],
            scope["subject"],
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AttestationError(
            "sealed Completion Record validation is invalid", "completion_validation"
        ) from error
    encoded = json.dumps(output["record"], sort_keys=True, separators=(",", ":"))
    if not attached:
        adapter.one("comments", "add", child_id, encoded, "--json")
    if status != "closed":
        try:
            adapter.one("close", child_id, "--json")
        except (
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            if not observed_attested_close(adapter, child_id, encoded):
                raise


def observed_attested_close(adapter, child_id, encoded):
    """Resolve an ambiguous close response from fresh read-only observations."""
    issue = adapter.one("show", child_id, "--json", "--readonly")
    comments = adapter.many("comments", child_id, "--json", "--readonly")
    return issue.get("status") == "closed" and any(
        item == encoded or isinstance(item, dict) and item.get("text") == encoded
        for item in comments
    )


def retain_partial_completion(attempt_descriptor, name):
    path = attempt_view(attempt_descriptor) / name
    if path.is_symlink():
        raise AttestationError(
            "partial Completion Record result is not a directory",
            "completion_validation",
        )
    if not path.exists():
        return
    if not path.is_dir():
        raise AttestationError(
            "partial Completion Record result is not a directory",
            "completion_validation",
        )
    for sequence in range(1, 1001):
        destination = f"{name}.abandoned-{sequence:03d}"
        try:
            os.stat(destination, dir_fd=attempt_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(
                name,
                destination,
                src_dir_fd=attempt_descriptor,
                dst_dir_fd=attempt_descriptor,
            )
            return
    raise AttestationError(
        "partial Completion Record retention limit was reached",
        "completion_validation",
    )


def finish(
    attempt,
    attempt_descriptor,
    scope,
    record,
    adapter,
    started_at,
    started,
    outcome,
    decision,
    category,
    message=None,
):
    seal_json_at(
        attempt_descriptor, "stdout.log.json", adapter.stdout if adapter else []
    )
    seal_json_at(
        attempt_descriptor, "stderr.log.json", adapter.stderr if adapter else []
    )
    output = {
        "schema_version": 1,
        "outcome": outcome,
        "decision": decision,
        "source": {"kind": "bead", "id": scope["mapping"]["bead_id"]},
        "started_at": started_at,
        "finished_at": timestamp(),
        "duration_seconds": round(time.monotonic() - started, 3)
        if started is not None
        else 0,
        "plan_sha256": scope["acceptance"]["plan"]["plan_sha256"],
        "record": record,
        "completion": "completion/output.json"
        if (attempt_view(attempt_descriptor) / "completion" / "output.json").exists()
        else None,
        "error_category": category,
        "error": message,
        "artifacts": {
            "request": "request.json",
            "stdout": "stdout.log.json",
            "stderr": "stderr.log.json",
        },
    }
    seal_json_at(attempt_descriptor, "output.json", output)


def overlaps(left, right):
    return left == right or left in right.parents or right in left.parents
