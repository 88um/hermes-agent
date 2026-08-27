"""Profile-owned review-helper protocol used by candidate deliveries.

The gateway deliberately knows only a short candidate identifier.  The helper
registered the complete candidate before it was sent and remains the authority
for resolving it, recording delivery, and appending feedback events.  In
particular, no path from model output is ever interpolated into a command.

Configuration lives in the active profile's platform ``extra`` mapping::

    platforms:
      telegram:
        extra:
          review_helper:
            enabled: true
            executable: /absolute/path/to/review-helper
            args: [--profile, humorbank]

``executable`` and ``args`` are read once from profile configuration.  Calls
use an argv list (never a shell) and send a compact JSON request on stdin.  A
helper returns one JSON object on stdout.  The operation is appended after the
configured arguments, so static helper options remain profile-owned::

    review-helper [configured args...] resolve < request.json

The protocol intentionally has a small vocabulary.  ``resolve`` is used before
attaching buttons, ``event`` is used for delivery and feedback (including
corrections), and ``note`` records a bounded force-reply note.  Helpers may
reject stale, unauthorized, unbound, or duplicate events; the gateway fails
closed when they do.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)


# Keep these values independent from the Postgen protocol.  Postgen's marker
# and ``pg:*`` callback lane are compatibility APIs and must remain byte-for-
# byte stable.
REVIEW_CANDIDATE_MARKER_NAME = "review_candidate_id"
REVIEW_CANDIDATE_MARKER = f"[[{REVIEW_CANDIDATE_MARKER_NAME}:"
REVIEW_CANDIDATE_ID_MAX_BYTES = 48
REVIEW_CANDIDATE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,48}")
REVIEW_CALLBACK_MAX_BYTES = 64
REVIEW_CALLBACK_PREFIX = "rh"

# Candidate-level actions.  Short callback codes leave room for the maximum
# marker id and keep every callback below Telegram's 64-byte limit.
REVIEW_ACTION_CODES: Mapping[str, str] = {
    "f": "funny",
    "w": "weak",
    "b": "bad",
    "r": "repost",
}
REVIEW_ACTION_TO_CODE: Mapping[str, str] = {
    action: code for code, action in REVIEW_ACTION_CODES.items()
}

# Stable reason keys are part of the feedback/eval contract.  The short wire
# codes are only for Telegram callback data; labels are operator-facing.
REVIEW_REASON_LABELS: Mapping[str, str] = {
    "bad_news_peg": "Bad news peg",
    "wrong_card_retrieval": "Wrong card/retrieval",
    "wrong_subject_archetype_context": "Wrong subject, archetype, or context",
    "mechanism_lost": "Mechanism lost",
    "unsupported_slot_fact": "Unsupported slot/fact",
    "awkward_deterministic_render": "Awkward deterministic render",
    "source_joke_weak": "Source joke itself is weak",
    "other_add_note": "Other / add note",
}
REVIEW_REASON_CODES: Mapping[str, str] = {
    "np": "bad_news_peg",
    "wr": "wrong_card_retrieval",
    "wc": "wrong_subject_archetype_context",
    "ml": "mechanism_lost",
    "uf": "unsupported_slot_fact",
    "ar": "awkward_deterministic_render",
    "sw": "source_joke_weak",
    "ot": "other_add_note",
}
REVIEW_REASON_TO_CODE: Mapping[str, str] = {
    reason: code for code, reason in REVIEW_REASON_CODES.items()
}
REVIEW_NOTE_REASON = "other_add_note"

# The note is intentionally bounded before it reaches a helper or the ledger.
# The value is in UTF-8 bytes because that is the transport/storage boundary.
DEFAULT_REVIEW_NOTE_MAX_BYTES = 4096
MAX_REVIEW_NOTE_BYTES = 16 * 1024
DEFAULT_REVIEW_HELPER_TIMEOUT_SECONDS = 15.0
DEFAULT_REVIEW_HELPER_MAX_OUTPUT_BYTES = 1 * 1024 * 1024

_ALLOWED_OPERATIONS = frozenset({"resolve", "event", "note"})


def valid_candidate_id(value: Any) -> bool:
    """Return whether *value* is a safe short candidate id."""

    if not isinstance(value, str):
        return False
    if not value or len(value.encode("utf-8")) > REVIEW_CANDIDATE_ID_MAX_BYTES:
        return False
    return bool(REVIEW_CANDIDATE_ID_RE.fullmatch(value))


def extract_review_candidate_metadata(content: str) -> tuple[dict[str, str] | None, str]:
    """Extract and strip a generic short review-candidate marker.

    The accepted canonical marker is ``[[review_candidate_id:<id>]]``.  The
    shorter ``[[review_candidate:<id>]]`` spelling is accepted for helpers
    written against the initial draft of this protocol.  Both forms carry
    only an id; malformed or unterminated directives are stripped and never
    become metadata.  This mirrors the Postgen fail-closed treatment without
    changing Postgen's parser or its warning text.
    """

    if "[[review_candidate" not in content:
        return None, content

    pattern = re.compile(r"\[\[review_candidate(?:_id)?:([^\]]*)\]\]")
    candidate: dict[str, str] | None = None

    def _remove(match: re.Match[str]) -> str:
        nonlocal candidate
        token = match.group(1).strip()
        marker = match.group(0)
        if candidate is None and valid_candidate_id(token):
            candidate = {"id": token}
        else:
            logger.warning(
                "review_candidate_directive_invalid: stripped malformed or "
                "abbreviated candidate directive (len=%d); no buttons attached",
                len(token),
            )
        return ""

    cleaned = pattern.sub(_remove, content)
    # A malformed marker must never leak private protocol text into a chat.
    if "[[review_candidate" in cleaned:
        cleaned, malformed_count = re.subn(
            r"\[\[review_candidate[^\r\n]*",
            "",
            cleaned,
        )
        for _ in range(malformed_count):
            logger.warning(
                "review_candidate_directive_invalid: stripped unterminated "
                "candidate directive; no buttons attached"
            )
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return candidate, cleaned


def _as_nonnegative_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result <= 0 or result != result or result in {float("inf"), float("-inf")}:
        return default
    return result


def _as_positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _mapping_for_profile(config: Any) -> Mapping[str, Any]:
    """Return the active profile's review-helper config mapping.

    Accept both a ``PlatformConfig``-like object and a raw mapping to keep the
    resolver easy to test.  No process environment fallback is intentional:
    helper identity is behavioral profile configuration, not a secret.
    """

    if isinstance(config, Mapping):
        if isinstance(config.get("extra"), Mapping):
            extra = config["extra"]
            if isinstance(extra.get("review_helper"), Mapping):
                return extra["review_helper"]
            if isinstance(extra.get("review_helper_config"), Mapping):
                return extra["review_helper_config"]
            if isinstance(extra.get("review_helper"), str):
                return {"executable": extra["review_helper"]}
        for key in ("review_helper", "review_helper_config"):
            if isinstance(config.get(key), Mapping):
                return config[key]
            if isinstance(config.get(key), str):
                return {"executable": config[key]}
        # A direct helper mapping is useful for callers that already selected
        # the profile's nested setting.
        if "executable" in config or "path" in config:
            return config
        return {}

    extra = getattr(config, "extra", None)
    if isinstance(extra, Mapping):
        for key in ("review_helper", "review_helper_config"):
            if isinstance(extra.get(key), Mapping):
                return extra[key]
        # String shorthand is accepted only from config, never from content.
        for key in ("review_helper", "review_helper_config"):
            if isinstance(extra.get(key), str):
                return {"executable": extra[key]}
    return {}


@dataclass(frozen=True)
class ReviewHelperConfig:
    """Validated, immutable helper identity for one profile."""

    executable: str
    args: tuple[str, ...] = ()
    enabled: bool = True
    timeout_seconds: float = DEFAULT_REVIEW_HELPER_TIMEOUT_SECONDS
    note_max_bytes: int = DEFAULT_REVIEW_NOTE_MAX_BYTES
    working_directory: str | None = None

    @classmethod
    def from_config(cls, config: Any) -> "ReviewHelperConfig | None":
        raw = _mapping_for_profile(config)
        if not raw:
            return None
        if raw.get("enabled") is False or str(raw.get("enabled", "")).strip().lower() in {
            "false", "0", "no", "off"
        }:
            return None

        executable_value = raw.get("executable", raw.get("path"))
        if not isinstance(executable_value, str) or not executable_value.strip():
            logger.warning("review_helper_config_invalid: executable is required")
            return None
        # Expand a user-home prefix for operator convenience, but require an
        # absolute path after expansion. A PATH lookup would let process state
        # or a model-controlled environment swap the helper identity.
        executable = os.path.expanduser(executable_value.strip())
        executable_path = Path(executable)
        if not executable_path.is_absolute():
            logger.warning(
                "review_helper_config_invalid: executable must be an absolute path"
            )
            return None
        executable = str(executable_path)

        raw_args = raw.get("args", raw.get("arguments", ()))
        if raw_args is None:
            raw_args = ()
        if not isinstance(raw_args, (list, tuple)) or any(
            not isinstance(arg, str) for arg in raw_args
        ):
            logger.warning(
                "review_helper_config_invalid: args must be a list of strings"
            )
            return None

        working_directory = raw.get(
            "working_directory", raw.get("working_dir", raw.get("cwd"))
        )
        if working_directory is not None:
            if not isinstance(working_directory, str) or not working_directory.strip():
                logger.warning(
                    "review_helper_config_invalid: working_directory must be a path"
                )
                return None
            working_directory = os.path.expanduser(working_directory.strip())
            if not Path(working_directory).is_absolute():
                logger.warning(
                    "review_helper_config_invalid: working_directory must be absolute"
                )
                return None

        return cls(
            executable=executable,
            args=tuple(raw_args),
            enabled=True,
            timeout_seconds=_as_nonnegative_float(
                raw.get("timeout_seconds", raw.get("timeout")),
                DEFAULT_REVIEW_HELPER_TIMEOUT_SECONDS,
            ),
            note_max_bytes=min(
                _as_positive_int(
                    raw.get("note_max_bytes", raw.get("max_note_bytes")),
                    DEFAULT_REVIEW_NOTE_MAX_BYTES,
                ),
                MAX_REVIEW_NOTE_BYTES,
            ),
            working_directory=working_directory,
        )

    def command(self, operation: str) -> list[str] | None:
        """Return an argv-only command for a protocol operation."""

        if operation not in _ALLOWED_OPERATIONS:
            return None
        # ``args`` is copied from the frozen tuple.  Dynamic data is supplied
        # through stdin, never interpolated into executable/args or a shell.
        return [self.executable, *self.args, operation]


def sanitize_candidate_payload(value: Any) -> dict[str, str]:
    """Keep only the id field when constructing helper resolve requests."""

    if not isinstance(value, Mapping):
        return {}
    candidate_id = value.get("id", value.get("candidate_id"))
    if not valid_candidate_id(candidate_id):
        return {}
    return {"id": candidate_id}


class ReviewHelperClient:
    """Synchronous, bounded client for a profile-owned review helper."""

    def __init__(self, config: ReviewHelperConfig):
        self.config = config

    def invoke(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Invoke one helper operation and return a JSON object.

        Missing, non-executable, malformed, timed-out, or oversized helper
        responses return ``{"ok": False, "reason": ...}``; callers use that
        result to fail closed.  We intentionally do not expose stderr or
        command paths in returned messages.
        """

        command = self.config.command(operation)
        if command is None:
            return {"ok": False, "reason": "unknown_operation"}
        executable = Path(self.config.executable)
        try:
            if not executable.is_file():
                return {"ok": False, "reason": "helper_missing"}
            if os.name != "nt" and not os.access(executable, os.X_OK):
                return {"ok": False, "reason": "helper_not_executable"}
        except (OSError, ValueError):
            return {"ok": False, "reason": "helper_unavailable"}

        try:
            request = json.dumps(
                dict(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
            completed = subprocess.run(
                command,
                input=request,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                cwd=self.config.working_directory,
                check=False,
            )
        except (subprocess.TimeoutExpired, TimeoutError):
            return {"ok": False, "reason": "helper_timeout"}
        except (OSError, TypeError, ValueError, OverflowError):
            return {"ok": False, "reason": "helper_unavailable"}

        if getattr(completed, "returncode", None) != 0:
            return {"ok": False, "reason": "helper_failed"}
        stdout = completed.stdout or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8", "replace")
        if not isinstance(stdout, bytes):
            return {"ok": False, "reason": "helper_invalid_response"}
        if len(stdout) > DEFAULT_REVIEW_HELPER_MAX_OUTPUT_BYTES:
            return {"ok": False, "reason": "helper_output_too_large"}
        try:
            result = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"ok": False, "reason": "helper_invalid_response"}
        if not isinstance(result, dict):
            return {"ok": False, "reason": "helper_invalid_response"}
        return result


def callback_data(code: str, candidate_id: str, *extra: str) -> str | None:
    """Build and byte-bound a generic Telegram callback payload."""

    if not valid_candidate_id(candidate_id):
        return None
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", code):
        return None
    if any(not isinstance(value, str) or ":" in value for value in extra):
        return None
    data = ":".join((REVIEW_CALLBACK_PREFIX, code, candidate_id, *extra))
    if len(data.encode("utf-8")) > REVIEW_CALLBACK_MAX_BYTES:
        return None
    return data


def reason_from_code(code: str) -> str | None:
    """Resolve a short or stable reason key from callback data."""

    return REVIEW_REASON_CODES.get(code, code if code in REVIEW_REASON_LABELS else None)


__all__ = [
    "DEFAULT_REVIEW_NOTE_MAX_BYTES",
    "DEFAULT_REVIEW_HELPER_MAX_OUTPUT_BYTES",
    "DEFAULT_REVIEW_HELPER_TIMEOUT_SECONDS",
    "MAX_REVIEW_NOTE_BYTES",
    "REVIEW_ACTION_CODES",
    "REVIEW_ACTION_TO_CODE",
    "REVIEW_CALLBACK_MAX_BYTES",
    "REVIEW_CALLBACK_PREFIX",
    "REVIEW_CANDIDATE_ID_MAX_BYTES",
    "REVIEW_CANDIDATE_MARKER_NAME",
    "REVIEW_CANDIDATE_MARKER",
    "REVIEW_REASON_CODES",
    "REVIEW_REASON_LABELS",
    "REVIEW_REASON_TO_CODE",
    "ReviewHelperClient",
    "ReviewHelperConfig",
    "callback_data",
    "extract_review_candidate_metadata",
    "reason_from_code",
    "sanitize_candidate_payload",
    "valid_candidate_id",
]
