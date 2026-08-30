"""Shared validation for optional AFK configuration sections."""

INFERENCE_ADAPTER_POLICY = {
    "adapter_family": "pi",
    "adapter_contract_version": 1,
}
INFERENCE_ROLE_DEFAULTS = {
    "acceptance_planner": {
        **INFERENCE_ADAPTER_POLICY,
        "model": "gpt-5.6-luna",
        "thinking": "low",
    },
    "review": {
        **INFERENCE_ADAPTER_POLICY,
        "model": "gpt-5.6-sol",
        "thinking": "medium",
    },
    "finding_assessment": {
        **INFERENCE_ADAPTER_POLICY,
        "model": "gpt-5.6-sol",
        "thinking": "medium",
    },
    "feedback_response": {
        **INFERENCE_ADAPTER_POLICY,
        "model": "gpt-5.6-sol",
        "thinking": "medium",
    },
}
SUPPORTED_THINKING = {"off", "minimal", "low", "medium", "high", "xhigh"}
_MISSING = object()


def _policy_value_matches(value, expected):
    """Match policy constants without Python's bool/int equality aliasing."""
    return type(value) is type(expected) and value == expected


def effective_inference_roles(value=_MISSING):
    """Validate configured role overrides and return a complete frozen mapping."""
    if value is _MISSING:
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
            or not set(configured)
            <= {
                "adapter_family",
                "adapter_contract_version",
                "model",
                "thinking",
            }
            or any(
                not _policy_value_matches(configured.get(name, expected), expected)
                for name, expected in INFERENCE_ADAPTER_POLICY.items()
            )
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
    """Validate one complete, immutable adapter and model policy."""
    fields = set(value) if isinstance(value, dict) else set()
    if fields not in (
        {"model", "thinking"},  # readable legacy v1 evidence
        {
            "adapter_family",
            "adapter_contract_version",
            "model",
            "thinking",
        },
    ):
        raise ValueError("inference setting is malformed")
    if "adapter_family" in value and any(
        not _policy_value_matches(value[name], expected)
        for name, expected in INFERENCE_ADAPTER_POLICY.items()
    ):
        raise ValueError("inference adapter policy is unsupported")
    model = value["model"]
    if not isinstance(model, str) or not model.strip():
        raise ValueError("inference model must be nonempty")
    if value["thinking"] not in SUPPORTED_THINKING:
        raise ValueError("inference thinking is unsupported")
    return value
