"""Prompt-injection / jailbreak scanner (G30).

A deterministic heuristic ruleset over the user-supplied prompt. Each rule is a
``(id, category, severity, pattern)`` tuple; :meth:`InjectionScanner.scan` returns
the highest-severity match (or a clean verdict). The ruleset is **precision-biased**:
patterns require an attack *verb* near an attack *object* (e.g. ``ignore`` within a
few words of ``instructions``) so ordinary text that merely contains "ignore" or
"act as" does not trip. Recall on novel attacks is meant to be raised by the
commercial managed ruleset feed (``guardrails/ruleset_feed.py``) or an optional
classifier backend — not by loosening these into false-positive territory.

The scanner never sees or emits raw prompt text in its verdict beyond a short,
bounded ``evidence`` snippet used only for operator debugging; the middleware logs
categories + rule ids only, so no user content is persisted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

# Categories (stable strings — used as metric/audit labels).
INSTRUCTION_OVERRIDE = "instruction_override"
SYSTEM_PROMPT_EXFIL = "system_prompt_exfil"
ROLE_PLAY_JAILBREAK = "role_play_jailbreak"
RESTRICTION_BYPASS = "restriction_bypass"

# Each entry: (rule_id, category, severity 0-1, regex source). Compiled case-insensitively.
# `[^.\n]{0,N}` bounds the verb→object distance so the pattern stays local (an attack
# phrase), not "these two words appear somewhere in a long benign document".
DEFAULT_INJECTION_RULES: Tuple[Tuple[str, str, float, str], ...] = (
    # ── Override / disregard the standing instructions ────────────────────────
    ("override.ignore_instructions", INSTRUCTION_OVERRIDE, 0.9,
     r"\b(?:ignore|disregard|forget|override|discard|bypass)\b[^.\n]{0,40}?"
     r"\b(?:previous|prior|earlier|above|preceding|all|any|the|your|these)?\b[^.\n]{0,20}?"
     r"\b(?:instructions?|prompts?|rules?|directions?|guidelines?|commands?|system\s+prompt)\b"),
    ("override.new_instructions_supersede", INSTRUCTION_OVERRIDE, 0.8,
     r"\b(?:from now on|new instructions?:|your real instructions?)\b[^.\n]{0,40}"
     r"\b(?:ignore|disregard|instead|override)\b"),
    # ── Exfiltrate the system prompt / hidden instructions ────────────────────
    ("exfil.reveal_system_prompt", SYSTEM_PROMPT_EXFIL, 0.9,
     r"\b(?:reveal|show|print|repeat|output|display|reveal to me|tell me|give me|what\s+(?:is|are))\b"
     r"[^.\n]{0,30}?\b(?:your\s+)?(?:system\s+prompt|initial\s+instructions?|"
     r"(?:the\s+)?(?:hidden|secret|original)\s+(?:instructions?|prompt))\b"),
    ("exfil.repeat_text_above", SYSTEM_PROMPT_EXFIL, 0.8,
     r"\brepeat\b[^.\n]{0,20}\b(?:the\s+)?(?:words?|text|everything|prompt)\b[^.\n]{0,10}\babove\b"),
    # ── Role-play / persona jailbreaks ────────────────────────────────────────
    ("jailbreak.dan_dev_mode", ROLE_PLAY_JAILBREAK, 0.95,
     r"\b(?:DAN\b|do anything now|developer mode|dev mode enabled|STAN\b|AIM\b)"),
    ("jailbreak.you_are_now_unrestricted", ROLE_PLAY_JAILBREAK, 0.9,
     r"\byou are (?:now )?(?:DAN\b|an?\s+(?:unfiltered|unrestricted|uncensored|amoral|jailbroken)\b)"),
    ("jailbreak.pretend_no_restrictions", ROLE_PLAY_JAILBREAK, 0.85,
     r"\b(?:pretend|imagine|roleplay|role-play|act)\b[^.\n]{0,40}?"
     r"\b(?:no\s+(?:restrictions?|rules?|filters?|limits?|guidelines?|morals?)|"
     r"unfiltered|unrestricted|uncensored|jailbroken)\b"),
    # ── Disable the safety layer ──────────────────────────────────────────────
    # `remove` is intentionally NOT a verb here — "remove the safety cover" is benign.
    # `safety` must be qualified by an AI-safety noun so a physical "safety cover"
    # never trips; bare guardrails/moderation/content-policy/restrictions still do.
    ("bypass.disable_safety", RESTRICTION_BYPASS, 0.9,
     r"\b(?:bypass|disable|turn off|switch off|circumvent|evade|deactivate|override)\b[^.\n]{0,30}?"
     r"\b(?:safety\s+(?:filters?|guidelines?|guardrails?|settings?|measures?|protocols?|controls?|checks?)|"
     r"guardrails?|content\s+(?:filters?|policy|moderation)|moderation|"
     r"restrictions?|safeguards?|content\s+filters?)\b"),
    ("bypass.without_restrictions", RESTRICTION_BYPASS, 0.8,
     r"\bwithout\s+(?:any\s+)?(?:restrictions?|filters?|censorship|limits?|guardrails?|"
     r"safety|moral\s+(?:constraints?|guidelines?))\b"),
    ("bypass.jailbreak_literal", RESTRICTION_BYPASS, 0.85, r"\bjailbreak\b"),
)


@dataclass
class InjectionVerdict:
    """Result of scanning a prompt. ``matched`` False → clean."""

    matched: bool
    category: Optional[str] = None
    rule_id: Optional[str] = None
    score: float = 0.0
    categories: List[str] = field(default_factory=list)  # all categories that fired (PII-free)
    evidence: str = ""                                    # bounded snippet, operator-debug only


class InjectionScanner:
    """Compile + run the injection ruleset.

    Parameters
    ----------
    rules:
        Override the default ruleset. ``None`` → :data:`DEFAULT_INJECTION_RULES`.
    extra_rules:
        Appended to the active ruleset (used by the commercial managed feed to add
        fresh signatures without a redeploy).
    threshold:
        Minimum severity for :attr:`InjectionVerdict.matched`. Defaults to 0.5, so
        every default rule (severity ≥ 0.8) trips; raise it to require higher
        confidence.
    """

    def __init__(
        self,
        rules: Optional[Sequence[Tuple[str, str, float, str]]] = None,
        *,
        extra_rules: Optional[Sequence[Tuple[str, str, float, str]]] = None,
        threshold: float = 0.5,
    ) -> None:
        active = list(rules if rules is not None else DEFAULT_INJECTION_RULES)
        if extra_rules:
            active += list(extra_rules)
        self.threshold = threshold
        self._compiled: List[Tuple[str, str, float, re.Pattern]] = []
        for rid, cat, sev, src in active:
            try:
                self._compiled.append((rid, cat, sev, re.compile(src, re.IGNORECASE)))
            except re.error:
                continue  # a malformed managed-feed rule must not break the scanner

    def scan(self, text: str) -> InjectionVerdict:
        """Scan one string, returning the highest-severity match."""
        if not text or not isinstance(text, str):
            return InjectionVerdict(matched=False)
        best: Optional[Tuple[str, str, float, re.Match]] = None
        categories: List[str] = []
        for rid, cat, sev, pat in self._compiled:
            m = pat.search(text)
            if not m:
                continue
            if cat not in categories:
                categories.append(cat)
            if best is None or sev > best[2]:
                best = (rid, cat, sev, m)
        if best is None or best[2] < self.threshold:
            return InjectionVerdict(matched=False, categories=categories)
        rid, cat, sev, m = best
        snippet = m.group(0)
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        return InjectionVerdict(
            matched=True, category=cat, rule_id=rid, score=sev,
            categories=categories, evidence=snippet,
        )
