"""Read central Beads with credentials confined to the tracker subprocess."""

import os

from afk_pr.config import location


def configured_environment(bead_id, config):
    from afk_run import SAFE_ID

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
    from afk_run import read_bead

    workspace, environment = configured_environment(bead_id, config)
    return read_bead(bead_id, workspace, env=environment)


def close_configured_bead(bead_id, config, reason, log):
    import subprocess

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
