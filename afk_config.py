"""Shared validation for optional AFK configuration sections."""

from pathlib import Path

INFERENCE_ROLE_DEFAULTS = {
    "acceptance_planner": {"model": "gpt-5.6-luna", "thinking": "low"},
    "review": {"model": "gpt-5.6-sol", "thinking": "medium"},
    "finding_assessment": {"model": "gpt-5.6-sol", "thinking": "medium"},
    "feedback_response": {"model": "gpt-5.6-sol", "thinking": "medium"},
}
SUPPORTED_THINKING = {"off", "minimal", "low", "medium", "high", "xhigh"}


def effective_inference_roles(value=None):
    """Validate configured role overrides and return a complete frozen mapping."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or not set(value) <= set(INFERENCE_ROLE_DEFAULTS):
        raise ValueError("configuration inference_roles contains an invalid role")
    effective = {
        name: dict(settings) for name, settings in INFERENCE_ROLE_DEFAULTS.items()
    }
    for role, configured in value.items():
        if (
            not isinstance(configured, dict)
            or not configured
            or not set(configured) <= {"model", "thinking"}
        ):
            raise ValueError(f"configuration inference role {role} is malformed")
        if "model" in configured and (
            not isinstance(configured["model"], str) or not configured["model"].strip()
        ):
            raise ValueError(
                f"configuration inference role {role} model must be nonempty"
            )
        if (
            "thinking" in configured
            and configured["thinking"] not in SUPPORTED_THINKING
        ):
            raise ValueError(
                f"configuration inference role {role} thinking is unsupported"
            )
        effective[role].update(configured)
    return effective


def validate_inference_setting(value):
    """Validate one complete setting retained in durable Run evidence."""
    if not isinstance(value, dict) or set(value) != {"model", "thinking"}:
        raise ValueError("inference setting is malformed")
    model = value["model"]
    if not isinstance(model, str) or not model.strip():
        raise ValueError("inference model must be nonempty")
    if value["thinking"] not in SUPPORTED_THINKING:
        raise ValueError("inference thinking is unsupported")
    return value


def attestation_result_root(value):
    """Return the configured existing attestation root or raise ValueError."""
    if not isinstance(value, dict) or set(value) != {"result_root"}:
        raise ValueError("configuration attestation is malformed")
    configured = value["result_root"]
    if not isinstance(configured, str) or not Path(configured).is_absolute():
        raise ValueError("attestation result_root must be an absolute path")
    root = Path(configured).resolve()
    if not root.is_dir():
        raise ValueError("configured attestation result_root must already exist")
    return root
