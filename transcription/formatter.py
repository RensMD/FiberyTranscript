"""Format transcripts for display and export."""


def _speaker_label(utterance: dict) -> str:
    """Build a stable display label for a diarized utterance."""
    speaker = utterance.get("speaker", "?")
    channel = utterance.get("channel")
    if channel is None:
        return f"Speaker {speaker}"
    return f"Speaker {speaker} (Channel {channel})"


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
    current_speaker_label = None

    for u in utterances:
        speaker_label = _speaker_label(u)
        text = u.get("text", "").strip()
        if not text:
            continue

        if speaker_label != current_speaker_label:
            current_speaker_label = speaker_label
            lines.append(f"\n**{speaker_label}**")

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
        speaker_label = _speaker_label(u)
        text = u.get("text", "").strip()
        if text:
            lines.append(f"{speaker_label}: {text}")

    return "\n".join(lines)
