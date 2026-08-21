"""Public structured contracts for acceptance-evidence preflight."""

import hashlib
import json
import re

CATALOG_CATEGORIES = {
    "repository_validation",
    "pipeline_evidence",
    "operator_external",
}
RESULT_CATEGORIES = CATALOG_CATEGORIES | {"unsupported", "ambiguous"}
AUTOMATIC_CATEGORIES = {"repository_validation", "pipeline_evidence"}
POLICY_FIELDS = {
    "input_contract",
    "classification_contract",
    "provider",
    "model",
    "thinking",
    "system_prompt_sha256",
    "adapter_command_sha256",
}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_CLASSIFICATION_REQUESTS = 256


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def classification_key(
    preflight_input: dict[str, object], policy: dict[str, object]
) -> str:
    """Identify one validated input and classifier policy pair."""
    return digest({"input": preflight_input, "policy": policy})


def validate_input(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "source",
        "title",
        "acceptance_criteria",
        "evidence_catalog",
        "timeout_seconds",
    }:
        raise ValueError("preflight input must use the exact schema_version 1 shape")
    if value["schema_version"] != 1:
        raise ValueError("preflight input must use schema_version 1")
    source = value["source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"kind", "id"}
        or source.get("kind") != "bead"
        or not bounded(source.get("id"), 256)
    ):
        raise ValueError("preflight source must identify one Bead")
    if not bounded(value["title"], 1000):
        raise ValueError("preflight title must be bounded nonempty text")
    if not bounded(value["acceptance_criteria"], 32 * 1024):
        raise ValueError("preflight acceptance_criteria must be bounded nonempty text")
    timeout = value["timeout_seconds"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("preflight timeout_seconds must be a positive integer")
    catalog = value["evidence_catalog"]
    if not isinstance(catalog, list) or not catalog:
        raise ValueError("preflight evidence_catalog must be a nonempty array")
    categories = set()
    for item in catalog:
        if not isinstance(item, dict) or set(item) != {
            "category",
            "route",
            "can_prove",
        }:
            raise ValueError("preflight evidence_catalog item has an invalid shape")
        category = item["category"]
        if category not in CATALOG_CATEGORIES or category in categories:
            raise ValueError("preflight evidence_catalog categories must be unique")
        categories.add(category)
        if not bounded(item["route"], 1000) or not bounded(item["can_prove"], 4000):
            raise ValueError("preflight evidence_catalog text must be bounded")
    return value


def validate_classification(
    preflight_input: dict[str, object], value: object
) -> list[dict[str, object]]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "requests"}:
        raise ValueError("classification must contain schema_version and requests")
    if value["schema_version"] != 1:
        raise ValueError("classification must use schema_version 1")
    requests = value["requests"]
    if not isinstance(requests, list) or not requests:
        raise ValueError("classification requests must be a nonempty array")
    if len(requests) > MAX_CLASSIFICATION_REQUESTS:
        raise ValueError(
            f"classification requests must contain at most "
            f"{MAX_CLASSIFICATION_REQUESTS} items"
        )
    routes = {
        item["category"]: item["route"] for item in preflight_input["evidence_catalog"]
    }
    for expected_index, request in enumerate(requests, 1):
        if not isinstance(request, dict) or set(request) != {
            "index",
            "request",
            "category",
            "route",
            "rationale",
        }:
            raise ValueError(f"classification request {expected_index} is malformed")
        if (
            not isinstance(request["index"], int)
            or isinstance(request["index"], bool)
            or request["index"] != expected_index
        ):
            raise ValueError("classification request indices must be contiguous")
        if request["category"] not in RESULT_CATEGORIES:
            raise ValueError("classification request category is invalid")
        for name in ("request", "route", "rationale"):
            if not bounded(request[name], 1000):
                raise ValueError(f"classification request {name} must be bounded text")
        expected_route = routes.get(request["category"], "human clarification")
        if request["route"] != expected_route:
            raise ValueError(
                "classification request route is not in the evidence catalog"
            )
    return requests


def decision(requests: list[dict[str, object]]) -> str:
    return (
        "proceed"
        if all(request["category"] in AUTOMATIC_CATEGORIES for request in requests)
        else "pause"
    )


def validate_output(
    value: object, preflight_input: dict[str, object]
) -> dict[str, object]:
    required = {
        "schema_version",
        "outcome",
        "source",
        "decision",
        "started_at",
        "finished_at",
        "duration_seconds",
        "process",
        "agent",
        "classifier",
        "requests",
        "artifacts",
    }
    if (
        not isinstance(value, dict)
        or frozenset(value)
        not in {frozenset(required), frozenset(required | {"classification_error"})}
        or value.get("schema_version") != 1
    ):
        raise ValueError("preflight output must use schema_version 1")
    if value.get("source") != preflight_input["source"]:
        raise ValueError("preflight output source does not match its input")
    outcome = value.get("outcome")
    preflight_decision = value.get("decision")
    classifier = value.get("classifier")
    requests = value.get("requests")
    if outcome not in {"completed", "failed", "timed_out", "interrupted"}:
        raise ValueError("preflight output outcome is invalid")
    if preflight_decision not in {"proceed", "pause"}:
        raise ValueError("preflight output decision is invalid")
    legacy_classifier = {"kind", "provider", "model", "status"}
    stored_classifier = legacy_classifier | {
        "source",
        "key",
        "record",
        "policy",
    }
    if (
        not isinstance(classifier, dict)
        or frozenset(classifier)
        not in {frozenset(legacy_classifier), frozenset(stored_classifier)}
        or classifier.get("kind") != "inference"
        or classifier.get("provider") != "openai-codex"
        or classifier.get("model") != "gpt-5.6-luna"
        or classifier.get("status")
        not in {"completed", "failed", "timed_out", "interrupted"}
    ):
        raise ValueError("preflight output classifier is invalid")
    classification_source = None
    if set(classifier) == stored_classifier:
        classification_source = classifier["source"]
        policy = classifier["policy"]
        if (
            classification_source not in {"inferred", "reused", "unavailable"}
            or not isinstance(classifier["key"], str)
            or not SHA256.fullmatch(classifier["key"])
            or classifier["key"] != classification_key(preflight_input, policy)
            or (
                classifier["record"] is not None
                and classifier["record"] != f"{classifier['key']}.json"
            )
            or not isinstance(policy, dict)
            or set(policy) != POLICY_FIELDS
            or policy.get("provider") != classifier["provider"]
            or policy.get("model") != classifier["model"]
            or policy.get("thinking") != "low"
            or policy.get("input_contract") != "afk-preflight-input-v1"
            or policy.get("classification_contract")
            != "afk-preflight-classification-v1"
            or not all(
                isinstance(policy.get(field), str) and policy[field].strip()
                for field in POLICY_FIELDS
            )
            or not SHA256.fullmatch(policy["system_prompt_sha256"])
            or not SHA256.fullmatch(policy["adapter_command_sha256"])
        ):
            raise ValueError("preflight output classifier storage is invalid")
        if (
            classification_source == "unavailable" and classifier["record"] is not None
        ) or (classification_source == "reused" and classifier["record"] is None):
            raise ValueError("preflight output classifier record is invalid")
    if not isinstance(requests, list):
        raise TypeError("preflight output requests must be an array")
    process = value.get("process")
    if not isinstance(process, dict):
        raise TypeError("preflight output process is invalid")
    if not all(
        isinstance(value.get(field), str) for field in ("started_at", "finished_at")
    ):
        raise ValueError("preflight output timestamps are invalid")
    duration = value.get("duration_seconds")
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or duration < 0
    ):
        raise ValueError("preflight output duration is invalid")
    if value.get("artifacts") != {
        "events": "events.jsonl",
        "stderr": "stderr.log",
    }:
        raise ValueError("preflight output artifacts are invalid")
    if outcome == "completed":
        if classification_source == "unavailable":
            raise ValueError("completed preflight must have a classification")
        validate_classification(
            preflight_input, {"schema_version": 1, "requests": requests}
        )
        if classifier.get("status") != "completed":
            raise ValueError("completed preflight classifier must be completed")
        if classifier.get("record") is None and classification_source is not None:
            raise ValueError("completed preflight must identify its stored record")
        if classification_source == "reused":
            if process.get("exit_code") is not None or value.get("agent") is not None:
                raise ValueError("reused preflight must not claim an agent process")
        else:
            if process.get("exit_code") != 0:
                raise ValueError("completed preflight process must exit zero")
            if value.get("agent") != {"status": "completed"}:
                raise ValueError("completed preflight agent must be completed")
        if preflight_decision != decision(requests):
            raise ValueError("preflight output decision does not match its requests")
    elif (
        classifier.get("status") != outcome or preflight_decision != "pause" or requests
    ):
        raise ValueError("noncompleted preflight must pause without a request ledger")
    return value


def bounded(value: object, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit
