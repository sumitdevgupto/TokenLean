"""Tool-call eligibility policy (G32).

A deterministic per-tenant allow/deny policy over the tool names a model *requests*.
Deliberately NOT a rules engine: the whole policy is three keys a human can read in a
config file —

    allow:   ["db_read_*", "search_docs"]     # glob patterns (fnmatch, case-SENSITIVE)
    deny:    ["*_delete", "shell_exec"]       # deny always wins over allow
    default: allow                            # what an unmatched tool gets

``default: deny`` turns the policy into an allowlist, which is the answer for a tenant
that wants a hard boundary rather than a blocklist of known-bad names.

Two design points that are load-bearing for security:

**Glob validation is not cosmetic.** ``fnmatch`` never raises on a malformed pattern —
``fnmatch.fnmatchcase("x", "[")`` silently returns False. For an ``allow`` entry that is
fail-safe (it matches nothing, so the tool falls through to ``default``); for a ``deny``
entry it is fail-*dangerous* — a typo'd deny pattern silently stops denying and nothing
anywhere reports it. So :func:`normalize_policy` validates every pattern up front and
raises :class:`ToolPolicyError`; callers surface that (the portal as a 422, the config
hot-reload as a WARNING that retains the last-good policy) rather than degrading quietly
to "allow everything".

**Case sensitivity.** Uses ``fnmatch.fnmatchcase``, never ``fnmatch.fnmatch`` — the
latter applies ``os.path.normcase``, which lowercases on Windows and would make the same
policy behave differently on a developer's laptop than in Cloud Run. Tool names are
case-sensitive identifiers.

The engine is pure stdlib, does no I/O, and never sees prompt content — only tool names,
which are function identifiers, so its inputs and outputs are PII-free by construction.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Verdict reasons (stable strings — used as audit detail / test assertions).
REASON_DENY_MATCH = "deny_match"
REASON_ALLOW_MATCH = "allow_match"
REASON_DEFAULT = "default"
REASON_INVALID_NAME = "invalid_name"

VALID_DEFAULTS = ("allow", "deny")


class ToolPolicyError(ValueError):
    """A policy that cannot be compiled — a malformed glob, or a bad ``default``.

    Raised by :func:`normalize_policy` only. Callers MUST NOT swallow it into an
    empty/permissive policy: the whole point of validating is that a broken deny rule
    is louder than a silently-inert one.
    """


@dataclass(frozen=True)
class ToolPolicy:
    """A compiled, validated policy. Immutable so it is safe to cache and share."""

    allow: Tuple[str, ...] = ()
    deny: Tuple[str, ...] = ()
    default: str = "allow"

    @property
    def is_noop(self) -> bool:
        """True when the policy can never deny anything — empty lists + ``default:
        allow``. The middleware uses this to skip work entirely, which is what keeps a
        default install byte-identical."""
        return not self.allow and not self.deny and self.default == "allow"


@dataclass
class ToolVerdict:
    """Outcome for one tool call. ``allowed`` False → the caller applies its mode."""

    allowed: bool
    tool_name: str = ""
    reason: str = REASON_DEFAULT
    matched_pattern: Optional[str] = None


def validate_pattern(pattern: Any) -> str:
    """Return ``pattern`` if it is a usable fnmatch glob, else raise ToolPolicyError.

    Checks three things ``fnmatch`` itself will not tell you about:
      1. it is a non-empty string;
      2. every ``[`` character class is closed (``fnmatch.translate`` silently escapes
         an unterminated ``[`` into a literal, so the pattern compiles but matches
         nothing);
      3. the translated regex actually compiles.
    """
    if not isinstance(pattern, str) or not pattern:
        raise ToolPolicyError(f"tool policy pattern must be a non-empty string, got {pattern!r}")
    _reject_unbalanced_class(pattern)
    try:
        re.compile(fnmatch.translate(pattern))
    except re.error as exc:
        raise ToolPolicyError(f"tool policy pattern {pattern!r} is not a valid glob: {exc}") from exc
    return pattern


def _reject_unbalanced_class(pattern: str) -> None:
    """Raise if a ``[`` character class is never closed.

    Mirrors ``fnmatch.translate``'s own scanning rules so we accept exactly what it
    accepts: a ``!`` may follow ``[`` for negation, and a ``]`` in the first position is
    a literal member rather than the terminator.
    """
    i, n = 0, len(pattern)
    while i < n:
        if pattern[i] != "[":
            i += 1
            continue
        j = i + 1
        if j < n and pattern[j] == "!":
            j += 1
        if j < n and pattern[j] == "]":
            j += 1
        while j < n and pattern[j] != "]":
            j += 1
        if j >= n:
            raise ToolPolicyError(
                f"tool policy pattern {pattern!r} has an unclosed '[' character class "
                f"(fnmatch would silently treat it as a literal and match nothing)"
            )
        i = j + 1


def _coerce_patterns(raw: Any, key: str) -> Tuple[str, ...]:
    """Validate one allow/deny list. A bare string is accepted as a single pattern —
    ``deny: "shell_exec"`` is an easy thing to write and unambiguous in meaning."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ToolPolicyError(f"tool policy '{key}' must be a list of glob patterns, got {type(raw).__name__}")
    out: List[str] = []
    for pattern in raw:
        validated = validate_pattern(pattern)
        if validated not in out:          # de-dup, preserve author order
            out.append(validated)
    return tuple(out)


def normalize_policy(cfg: Optional[Dict[str, Any]]) -> ToolPolicy:
    """Build a validated :class:`ToolPolicy` from a config block.

    Reads ``allow`` / ``deny`` / ``default`` and **ignores every other key**, so a
    future knob added to the same block (or a stale one left behind) never breaks an
    older proxy — forward compatibility without a version field.

    Raises :class:`ToolPolicyError` on a malformed pattern or an unrecognised
    ``default``. It deliberately does NOT fall back to a permissive policy: a config
    the operator cannot express correctly must be visible, not silently inert.
    """
    cfg = cfg or {}
    if not isinstance(cfg, dict):
        raise ToolPolicyError(f"tool policy must be a mapping, got {type(cfg).__name__}")

    default = str(cfg.get("default", "allow")).strip().lower()
    if default not in VALID_DEFAULTS:
        raise ToolPolicyError(
            f"tool policy 'default' must be one of {VALID_DEFAULTS}, got {cfg.get('default')!r}"
        )
    return ToolPolicy(
        allow=_coerce_patterns(cfg.get("allow"), "allow"),
        deny=_coerce_patterns(cfg.get("deny"), "deny"),
        default=default,
    )


def evaluate_tool(policy: ToolPolicy, tool_name: Any) -> ToolVerdict:
    """Decide whether one requested tool call is eligible.

    Order is **deny → allow → default**: an explicit deny always wins, so adding a tool
    to ``allow`` can never accidentally re-open something the operator denied.

    A missing or non-string tool name returns ``allowed=False`` regardless of
    ``default`` — it is malformed provider output that no dispatcher could route
    anyway, and fail-closed is the correct posture for a name we cannot evaluate.
    """
    if not isinstance(tool_name, str) or not tool_name:
        return ToolVerdict(allowed=False, tool_name="", reason=REASON_INVALID_NAME)

    for pattern in policy.deny:
        if fnmatch.fnmatchcase(tool_name, pattern):
            return ToolVerdict(False, tool_name, REASON_DENY_MATCH, pattern)
    for pattern in policy.allow:
        if fnmatch.fnmatchcase(tool_name, pattern):
            return ToolVerdict(True, tool_name, REASON_ALLOW_MATCH, pattern)
    return ToolVerdict(policy.default == "allow", tool_name, REASON_DEFAULT, None)


__all__ = [
    "ToolPolicy",
    "ToolPolicyError",
    "ToolVerdict",
    "normalize_policy",
    "evaluate_tool",
    "validate_pattern",
    "VALID_DEFAULTS",
    "REASON_DENY_MATCH",
    "REASON_ALLOW_MATCH",
    "REASON_DEFAULT",
    "REASON_INVALID_NAME",
]
