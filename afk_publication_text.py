"""Public text redaction shared by current PR publication and retained exports."""

import re

PRIVATE_KEY_TEXT = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
REDACTABLE_CREDENTIAL_TEXT = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(?:password|token|secret|api[_-]?key)\s*[:=]\s*\S+"),
    re.compile(
        r"(?i)(?:AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|AZURE_CLIENT_SECRET|"
        r"GOOGLE_APPLICATION_CREDENTIALS)\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)(?:authorization\s*:\s*)?(?:basic|bearer)\s+\S{12,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@"),
)
# A component may contain horizontal whitespace, but not at either edge.  The
# edge rule is important: without it, ``/tmp/a and compare /tmp/b`` is parsed
# as one path whose second component is "a and compare ".
POSIX_PATH_COMPONENT = r"[^\s/'\"`](?:[^\r\n/'\"`]*?[^\s/'\"`])?"
WINDOWS_PATH_COMPONENT = r"[^\s:\\/'\"`](?:[^\r\n:\\/'\"`]*?[^\s:\\/'\"`])?"
HOST_PATH = re.compile(
    r"(?:"
    # A spaced final filename is unambiguous when its last word has a file
    # extension.  Stop at that extension rather than consuming later prose.
    r"(?<![A-Za-z0-9./])/(?!/)(?:" + POSIX_PATH_COMPONENT + r"/)*"
    r"[^\s/'\"`]+(?:[ \t]+[^\s/'\"`]+)+?\.[A-Za-z0-9]{1,16}"
    r"(?![A-Za-z0-9._-])"
    r"|(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/](?:"
    + WINDOWS_PATH_COMPONENT
    + r"[\\/])*[^\s\\/'\"`]+(?:[ \t]+[^\s\\/'\"`]+)+?"
    r"\.[A-Za-z0-9]{1,16}(?![A-Za-z0-9._-]))"
    # If a string consists solely of a path, its final component can safely
    # contain spaces even without an extension.
    r"|\A/(?!/)(?:" + POSIX_PATH_COMPONENT + r"/)*" + POSIX_PATH_COMPONENT + r"\Z"
    r"|\A[A-Za-z]:[\\/](?:"
    + WINDOWS_PATH_COMPONENT
    + r"[\\/])*"
    + WINDOWS_PATH_COMPONENT
    + r"\Z"
    # General paths retain prose by allowing spaces only in completed,
    # separator-terminated components and using a whitespace-free final one.
    r"|(?<![A-Za-z0-9./])/(?!/)(?:" + POSIX_PATH_COMPONENT + r"/)*[^\s/'\"`]+"
    r"|(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/](?:"
    + WINDOWS_PATH_COMPONENT
    + r"[\\/])*[^\s\\/'\"`]+)"
    r"|(?<![\\])\\\\" + WINDOWS_PATH_COMPONENT + r"[\\/]"
    r"(?:" + WINDOWS_PATH_COMPONENT + r"[\\/])*[^\s\\/'\"`]+"
    r")"
)
# Embedded paths whose final component may contain spaces but has no
# recognizable prose boundary cannot be safely separated from following text.
# Reject them rather than publishing a suffix after HOST_PATH redacts only the
# first word. Paths matched above *with* whitespace are unambiguous (an
# extension, an intermediate spaced component, or a whole-string path).
PATH_PROSE_BOUNDARY = (
    r"(?:and|or|but|before|after|then|while|when|where|which|that|to|for|from|"
    r"with|without|is|was|must|should|can)\b"
)
AMBIGUOUS_SPACED_FINAL_PATH = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9./])/(?!/)(?:" + POSIX_PATH_COMPONENT + r"/)*"
    r"[^\s/'\"`]+[ \t]+(?!" + PATH_PROSE_BOUNDARY + r")[^\r\n/'\"`]+"
    r"|(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/](?:"
    + WINDOWS_PATH_COMPONENT
    + r"[\\/])*[^\s\\/'\"`]+[ \t]+(?!"
    + PATH_PROSE_BOUNDARY
    + r")[^\r\n\\/'\"`]+)"
    r"|(?<![\\])\\\\" + WINDOWS_PATH_COMPONENT + r"[\\/]"
    r"(?:" + WINDOWS_PATH_COMPONENT + r"[\\/])*"
    r"[^\s\\/'\"`]+[ \t]+(?!" + PATH_PROSE_BOUNDARY + r")[^\r\n\\/'\"`]+"
    r")",
    re.IGNORECASE,
)
REDACTED_SECRET = "[redacted-secret]"


class ExportError(Exception):
    pass


def sanitize_public_artifact_text(text, redactions):
    """Derive public artifact text by redacting paths and replaceable credentials."""
    text = redact_public_paths(text, redactions)
    if PRIVATE_KEY_TEXT.search(text):
        raise ExportError("artifact contains unsafe private key material")
    for pattern in REDACTABLE_CREDENTIAL_TEXT:
        text = pattern.sub(REDACTED_SECRET, text)
    return text


def redact_public_paths(text, redactions):
    for prefix in sorted(redactions, key=len, reverse=True):
        text = text.replace(prefix, "[redacted-path]")

    # Ignore complete, safely bounded spaced paths while looking for the
    # ambiguous case. A whitespace-free HOST_PATH match is deliberately left
    # visible: it may be merely the leaked prefix of a spaced final component.
    ambiguity_input = list(text)
    for match in HOST_PATH.finditer(text):
        if re.search(r"[ \t]", match.group()):
            ambiguity_input[match.start() : match.end()] = " " * len(match.group())
    if AMBIGUOUS_SPACED_FINAL_PATH.search("".join(ambiguity_input)):
        raise ExportError("text contains an ambiguously bounded host path")

    text = HOST_PATH.sub("[redacted-path]", text)
    return text
