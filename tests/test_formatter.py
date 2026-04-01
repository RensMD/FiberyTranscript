from transcription.formatter import format_diarized_transcript, format_plain_transcript


def test_format_diarized_transcript_includes_channel_when_present():
    utterances = [
        {"speaker": "A", "channel": 0, "text": "Local voice"},
        {"speaker": "A", "channel": 1, "text": "Remote voice"},
    ]

    formatted = format_diarized_transcript(utterances)

    assert "**Speaker A (Channel 0)**" in formatted
    assert "**Speaker A (Channel 1)**" in formatted
    assert "Local voice" in formatted
    assert "Remote voice" in formatted


def test_format_plain_transcript_keeps_channel_specific_labels():
    utterances = [
        {"speaker": 1, "channel": 0, "text": "First"},
        {"speaker": 1, "channel": 1, "text": "Second"},
    ]

    formatted = format_plain_transcript(utterances)

    assert "Speaker 1 (Channel 0): First" in formatted
    assert "Speaker 1 (Channel 1): Second" in formatted
