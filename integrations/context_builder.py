"""Build transcription and summarization context from Fibery entity data."""

from integrations.fibery_client import EntityContext


def build_word_boost(context: EntityContext) -> list[str]:
    """Build AssemblyAI word_boost list from entity context.

    Collects participant names, organization names, and operator names.
    For multi-word names, includes both the full name and first name.
    Returns a deduplicated list (max 1000 entries, each max 6 words).
    """
    if not context:
        return []

    raw = []

    for name in context.assignee_names:
        raw.append(name)
        _add_first_name(raw, name)

    for name in context.people_names:
        raw.append(name)
        _add_first_name(raw, name)

    for name in context.organization_names:
        raw.append(name)

    for name in context.operator_names:
        raw.append(name)

    # Extract keywords from meeting title (skip very short/common words)
    if context.entity_name:
        for word in context.entity_name.split():
            if len(word) > 3:
                raw.append(word)

    # Deduplicate (case-insensitive) and filter
    seen = set()
    result = []
    for term in raw:
        term = term.strip()
        if not term:
            continue
        key = term.lower()
        if key in seen:
            continue
        if len(term.split()) > 6:
            continue
        seen.add(key)
        result.append(term)

    return result[:1000]


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
        lines.append(f"Our team members in this meeting: {names}")

    if context.people_with_orgs:
        parts = []
        for p in context.people_with_orgs:
            if p.get("org"):
                parts.append(f"{p['name']} ({p['org']})")
            else:
                parts.append(p["name"])
        lines.append(f"External participants: {', '.join(parts)}")
    elif context.people_names:
        lines.append(f"External participants: {', '.join(context.people_names)}")

    if context.organization_names:
        lines.append(f"Organizations: {', '.join(context.organization_names)}")

    if context.operator_names:
        lines.append(f"Operators: {', '.join(context.operator_names)}")

    return "\n".join(lines)


def _add_first_name(names: list[str], full_name: str) -> None:
    """Add the first name as a separate entry if the name has multiple parts."""
    parts = full_name.strip().split()
    if len(parts) > 1:
        names.append(parts[0])
