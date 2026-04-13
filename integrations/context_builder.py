"""Build transcription and summarization context from Fibery entity data."""

import re
from collections import Counter
from dataclasses import dataclass, field

from integrations.fibery_client import EntityContext

_KEYTERMS_MAX_TOTAL_WORDS = 50
_KEYTERMS_MAX_WORDS_PER_PHRASE = 6
_KEYTERMS_MIN_CHARS = 5
_KEYTERMS_MAX_CHARS = 50
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class KeytermsPromptBuildResult:
    """Curated keyterms plus metadata for logging/debugging."""

    terms: list[str] = field(default_factory=list)
    total_words: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)

    def format_skipped_reasons(self) -> str:
        """Return a compact summary like ``duplicate=2, word_budget=1``."""
        if not self.skipped_reasons:
            return ""
        return ", ".join(f"{reason}={count}" for reason, count in self.skipped_reasons.items())


def build_speaker_names(context: EntityContext) -> list[str]:
    """Return deduplicated participant names for diarization hints."""
    if not context:
        return []

    names: list[str] = []
    seen: set[str] = set()

    for raw_name in [
        *context.assignee_names,
        *context.people_names,
        *context.operator_names,
    ]:
        name = (raw_name or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)

    return names


def build_speaker_hints(context: EntityContext) -> dict:
    """Build AssemblyAI speaker-count and identification hints."""
    names = build_speaker_names(context)
    count = len(names)
    if count < 2:
        return {}

    has_internal = bool(context.assignee_names)
    has_external = bool(context.people_names)
    has_operator = bool(context.operator_names)
    source_count = sum(1 for present in (has_internal, has_external, has_operator) if present)

    hints: dict = {}

    # Exact counts are only safe when multiple participant sources agree and the
    # result stays in a small human-sized range.
    if 2 <= count <= 6 and source_count >= 2:
        hints["speakers_expected"] = count
    else:
        hints["speaker_options"] = {
            "min_speakers_expected": max(2, count - 1),
            "max_speakers_expected": min(8, count + 1),
            "use_two_stage_clustering": True,
        }

    if count <= 6:
        hints["speaker_identification"] = names

    return hints


def build_keyterms_prompt(context: EntityContext) -> KeytermsPromptBuildResult:
    """Build a conservative AssemblyAI ``keyterms_prompt`` from entity context.

    The automatic prompt stays intentionally small and high-confidence:
    participants first, then operators, then organizations. Terms are
    whitespace-normalized, deduplicated case-insensitively, filtered to the
    shared Universal-3 Pro / Universal-2 envelope, and capped by total words.
    """
    if not context:
        return KeytermsPromptBuildResult()

    candidates = [
        *context.assignee_names,
        *context.people_names,
        *context.operator_names,
        *context.organization_names,
    ]
    seen: set[str] = set()
    terms: list[str] = []
    total_words = 0
    skipped = Counter()

    for raw_term in candidates:
        term = _normalize_keyterm(raw_term)
        if not term:
            skipped["empty"] += 1
            continue

        word_count = len(term.split())
        if word_count > _KEYTERMS_MAX_WORDS_PER_PHRASE:
            skipped["too_many_words"] += 1
            continue
        if len(term) < _KEYTERMS_MIN_CHARS or len(term) > _KEYTERMS_MAX_CHARS:
            skipped["unsupported_length"] += 1
            continue

        key = term.casefold()
        if key in seen:
            skipped["duplicate"] += 1
            continue
        if total_words + word_count > _KEYTERMS_MAX_TOTAL_WORDS:
            skipped["word_budget"] += 1
            continue

        seen.add(key)
        terms.append(term)
        total_words += word_count

    return KeytermsPromptBuildResult(
        terms=terms,
        total_words=total_words,
        skipped_reasons=dict(skipped),
    )


def build_summary_context(context: EntityContext) -> str:
    """Build a structured context string for Gemini summarization prompt.

    Returns a human-readable block of meeting context, or empty string
    if no meaningful context is available.
    """
    if not context:
        return ""

    lines = []

    if context.entity_name:
        lines.append(f"Meeting: {context.entity_name}")

    if context.assignee_names:
        names = ", ".join(context.assignee_names)
        lines.append(f"Confirmed internal participants in this meeting: {names}")

    if context.people_with_orgs:
        parts = []
        for p in context.people_with_orgs:
            if p.get("org"):
                parts.append(f"{p['name']} ({p['org']})")
            else:
                parts.append(p["name"])
        lines.append(f"Confirmed external participants in this meeting: {', '.join(parts)}")
    elif context.people_names:
        lines.append(f"Confirmed external participants in this meeting: {', '.join(context.people_names)}")

    if context.organization_names:
        lines.append(f"Organizations mentioned on this meeting record: {', '.join(context.organization_names)}")

    if context.operator_names:
        lines.append(f"Operators linked on this meeting record: {', '.join(context.operator_names)}")

    return "\n".join(lines)


def _normalize_keyterm(term: str) -> str:
    """Collapse whitespace so equivalent phrases dedupe reliably."""
    return _WHITESPACE_RE.sub(" ", (term or "").strip())
