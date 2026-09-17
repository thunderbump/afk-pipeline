"""Read central Beads with credentials confined to the tracker subprocess."""

import os

from afk_pr.config import location


def read_configured_bead(bead_id, config):
    from afk_run import SAFE_ID, read_bead

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
    return read_bead(bead_id, workspace, env=environment)
