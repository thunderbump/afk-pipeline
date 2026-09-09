"""Project sealed runtime results into component-owned process and log views."""

import shutil
from pathlib import Path

from afk_runtime import process_result


def publish_runtime_logs(result: Path, receipt: object) -> None:
    attempts = receipt["attempts"]
    for artifact, filename in (("events", "events.jsonl"), ("stderr", "stderr.log")):
        source = attempts[-1]["artifacts"].get(artifact) if attempts else None
        if source:
            shutil.copyfile(result / "inference" / source, result / filename)
        else:
            (result / filename).touch()


def runtime_process(receipt: object) -> dict[str, object]:
    attempts = receipt["attempts"]
    process = attempts[-1].get("process", {}) if attempts else {}
    return process_result(process.get("exit_code"), process.get("error"))
