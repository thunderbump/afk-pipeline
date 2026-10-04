"""Read central Beads with credentials confined to the tracker subprocess."""

import json
import os
import re
import subprocess

from afk_pr.config import location

SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class PreparationError(Exception):
    pass


def read_bead(bead_id, workspace, *, env=None):
    try:
        completed = subprocess.run(
            ["bd", "show", bead_id, "--json"],
            cwd=workspace,
            env=env,
            timeout=120,
            text=True,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
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


def configured_environment(bead_id, config):
    if not SAFE_ID.fullmatch(bead_id):
        raise ValueError("invalid central Bead ID")
    workspace = location(config.get("beads_workspace"), "beads_workspace")
    if not workspace.is_dir():
        raise ValueError("Beads workspace is unavailable")
    environment = os.environ.copy()
    secret = location(
        config.get("beads", {}).get(
            "password_file", str(workspace / "secrets/dolt_beads_password.txt")
        ),
        "beads password_file",
    )
    if secret.exists():
        password = secret.read_text().splitlines()
        if not password or not password[0]:
            raise ValueError("Beads credential file is empty")
        environment["BEADS_DOLT_PASSWORD"] = password[0]
    return workspace, environment


def read_configured_bead(bead_id, config):
    workspace, environment = configured_environment(bead_id, config)
    return read_bead(bead_id, workspace, env=environment)


def close_configured_bead(bead_id, config, reason, log):
    workspace, environment = configured_environment(bead_id, config)
    with log.open("w") as diagnostics:
        result = subprocess.run(
            ["bd", "close", bead_id, "--reason", reason],
            cwd=workspace,
            env=environment,
            stdout=diagnostics,
            stderr=diagnostics,
            timeout=120,
            check=False,
        )
    if result.returncode:
        raise RuntimeError("Bead closure failed; inspect private diagnostics")
