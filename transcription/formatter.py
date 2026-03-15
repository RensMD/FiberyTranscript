"""Format transcripts for display and export."""


def format_diarized_transcript(utterances: list[dict]) -> str:
    """Format diarized utterances into readable markdown text.

    Args:
        utterances: List of dicts with 'speaker' and 'text' keys.

    Returns:
        Markdown-formatted string with speaker labels.
    """
    if not utterances:
        return ""

    lines = []
    current_speaker = None

    for u in utterances:
        speaker = u.get("speaker", "?")
        text = u.get("text", "").strip()
        if not text:
            continue

        if speaker != current_speaker:
            current_speaker = speaker
            lines.append(f"\n**Speaker {speaker}**")

        lines.append(text)

    return "\n".join(lines).strip()


def format_plain_transcript(utterances: list[dict]) -> str:
    """Format utterances as plain text with speaker prefixes.

    Args:
        utterances: List of dicts with 'speaker' and 'text' keys.

    Returns:
        Plain text with "Speaker X: text" format.
    """
    if not utterances:
        return ""

    lines = []
    for u in utterances:
        speaker = u.get("speaker", "?")
        text = u.get("text", "").strip()
        if text:
            lines.append(f"Speaker {speaker}: {text}")

    return "\n".join(lines)
