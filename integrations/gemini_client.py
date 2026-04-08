"""Google Gemini summarization client."""

import logging
import re
import threading
import time

from config.constants import (
    CORE_PROMPT,
    DEFAULT_COMPANY_CONTEXT,
    DEFAULT_INTERVIEW_PROMPT,
    DEFAULT_MEETING_PROMPT,
    PROBLEM_EXTRACTION_PROMPT,
    PROBLEM_EXTRACTION_SCHEMA,
    PROBLEM_EXTRACTION_USER_TEMPLATE,
    PROMPT_TYPE_LABELS,
    PROMPT_TYPE_PROMPTS,
    TRANSCRIPT_CLEANUP_AUDIO_ADDENDUM,
    TRANSCRIPT_CLEANUP_PROMPT,
)

logger = logging.getLogger(__name__)

_SUMMARY_REQUEST_TIMEOUT_MS = 300_000
_CLEANUP_REQUEST_TIMEOUT_MS = 300_000
_FILE_DELETE_TIMEOUT_MS = 10_000
_CLEANUP_LENGTH_GUARD_MIN_WORDS = 600
_CLEANUP_LENGTH_GUARD_MIN_WORD_RATIO = 0.4
_CLEANUP_LENGTH_GUARD_MIN_CHAR_RATIO = 0.45
_CLEANUP_MAX_CHARS_PER_REQUEST = 18_000
_CLEANUP_MAX_BLOCKS_PER_REQUEST = 80

SUMMARY_STYLE_INSTRUCTIONS = {
    "normal": " ",
    "short": (
        "Use a genuinely short summary. Keep it under about 900 characters unless a critical detail would otherwise be lost. Omit routine background, filler, repetition, and long examples."
    ),
    "minimal": "Use minimal output: 3-5 short sentences/bullets with only critical info.",
}


def _normalize_summary_style(summary_style: str) -> str:
    """Return a supported summary style, defaulting to normal."""
    style = (summary_style or "").strip().lower()
    return style if style in SUMMARY_STYLE_INSTRUCTIONS else "normal"


def _is_retryable_gemini_error(exc: Exception) -> bool:
    """Return True when Gemini failed in a way that should trigger fallback/retry."""
    try:
        from google.api_core.exceptions import (
            DeadlineExceeded,
            GatewayTimeout,
            NotFound,
            ResourceExhausted,
            ServiceUnavailable,
            TooManyRequests,
        )

        if isinstance(
            exc,
            (
                DeadlineExceeded,
                GatewayTimeout,
                NotFound,
                ResourceExhausted,
                ServiceUnavailable,
                TooManyRequests,
            ),
        ):
            return True
    except ImportError:
        pass

    error_msg = f"{type(exc).__name__}: {exc}".lower()
    if any(code in error_msg for code in ("429", "500", "502", "503", "504")):
        return True

    return any(
        token in error_msg
        for token in (
            "deadline exceeded",
            "deadline_exceeded",
            "gateway timeout",
            "resource exhausted",
            "service unavailable",
            "temporarily unavailable",
            "timed out",
            "timeout",
            "too many requests",
            "not found",
        )
    )


def _build_system_prompt(
    role_prompt: str,
    prompt_type: str,
    summary_style: str,
    summary_language: str,
    company_context: str,
    meeting_context: str,
) -> str:
    """Compose a full Gemini system prompt from role prompt and shared context parameters."""
    context = company_context.strip() if company_context.strip() else DEFAULT_COMPANY_CONTEXT
    system_prompt = f"{role_prompt}\n\n{CORE_PROMPT}\n\n{context}"

    if meeting_context.strip():
        system_prompt += f"\n\nMeeting participants and context:\n{meeting_context}"
        system_prompt += "\nUse the participant names above to identify speakers where possible."

    normalized_style = _normalize_summary_style(summary_style)
    system_prompt += (
        f"\n\nSummary style setting: {normalized_style}\n"
        f"{SUMMARY_STYLE_INSTRUCTIONS[normalized_style]}"
    )

    summary_language_name = _LANGUAGE_NAMES.get(summary_language, "English")
    system_prompt += (
        f"\n\nOutput language: {summary_language_name}. "
        f"Write the entire summary in {summary_language_name}. "
        "Do not translate the transcript itself; only summarize it in the requested language."
    )

    if prompt_type == "interview" and normalized_style == "short":
        system_prompt += (
            "\nBecause the summary style is short, include at most 2 problem definition suggestions "
            "and omit that section entirely if the evidence is weak, repetitive, or low-value."
        )
    elif prompt_type == "interview" and normalized_style == "minimal":
        system_prompt += (
            "\nBecause the summary style is minimal, include at most 1 problem definition suggestion "
            "and omit it entirely unless it is especially strong and clearly supported."
        )

    return system_prompt


def _call_gemini_with_retry(client, system_prompt: str, user_message: str, models_to_try: list, types) -> str:
    """Run a single Gemini summarization call with model fallback and retry."""
    max_retries = 2

    for attempt in range(max_retries):
        for current_model in models_to_try:
            try:
                logger.info(
                    "Summarizing with Gemini model=%s (attempt %d)",
                    current_model,
                    attempt + 1,
                )
                response = client.models.generate_content(
                    model=current_model,
                    contents=user_message,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.3,
                    ),
                )
                summary = response.text
                logger.info("Gemini summarization complete (%d chars)", len(summary))
                return summary

            except Exception as exc:
                if _is_retryable_gemini_error(exc):
                    logger.warning("Falling back from %s: %s", current_model, exc)
                    continue
                logger.error("Unexpected error with %s: %s", current_model, exc)
                raise

        if attempt < max_retries - 1:
            sleep_time = 5 * (attempt + 1)
            logger.warning("Both models unavailable. Retrying in %d seconds...", sleep_time)
            time.sleep(sleep_time)

    raise RuntimeError(f"Summarization failed: Models {models_to_try} are experiencing high demand.")


def summarize_transcript(
    api_key: str,
    transcript: str,
    notes: str,
    model: str,
    model_fallback: str,
    prompt_types: "list[str] | None" = None,
    custom_prompt: str = "",
    summary_style: str = "normal",
    summary_language: str = "en",
    company_context: str = "",
    meeting_context: str = "",
    # Deprecated — kept for backward compatibility with existing callers/tests
    is_interview: bool = False,
) -> str:
    """Summarize a meeting transcript using Google Gemini.

    Args:
        api_key: Google Gemini API key.
        transcript: The full meeting transcript text.
        notes: User notes from Fibery (may be empty).
        prompt_types: List of prompt type keys ('summarize', 'interview', 'shareable', 'custom').
            Multiple types trigger separate Gemini calls combined with section headers.
        custom_prompt: Role prompt text used when 'custom' is in prompt_types.
        summary_style: Output length style: normal, short, or minimal.
        summary_language: Output language code for the generated summary.
        company_context: Company-specific context (uses default if empty).
        meeting_context: Dynamic per-meeting context (participants, orgs).
        is_interview: Deprecated. Used as fallback when prompt_types is not provided.

    Returns:
        The AI-generated summary text. Multiple prompt types produce labeled sections
        separated by horizontal rules.
    """
    from google import genai
    from google.genai import types

    # Backward compat: derive prompt_types from is_interview when not explicitly set
    if not prompt_types:
        prompt_types = ["interview"] if is_interview else ["summarize"]

    client = genai.Client(
        api_key=api_key,
        http_options={"timeout": _SUMMARY_REQUEST_TIMEOUT_MS},
    )

    user_message = f"User notes: {notes}\n\nTranscript:\n{transcript}"
    models_to_try = [model, model_fallback]
    is_multi = len(prompt_types) > 1
    sections = []

    for ptype in prompt_types:
        if ptype == "custom":
            role_prompt = custom_prompt.strip() if custom_prompt.strip() else DEFAULT_MEETING_PROMPT
        else:
            role_prompt = PROMPT_TYPE_PROMPTS.get(ptype, DEFAULT_MEETING_PROMPT)

        system_prompt = _build_system_prompt(
            role_prompt=role_prompt,
            prompt_type=ptype,
            summary_style=summary_style,
            summary_language=summary_language,
            company_context=company_context,
            meeting_context=meeting_context,
        )

        section_text = _call_gemini_with_retry(client, system_prompt, user_message, models_to_try, types)

        if is_multi:
            label = PROMPT_TYPE_LABELS.get(ptype, ptype.title())
            sections.append(f"## {label}\n\n{section_text}")
        else:
            sections.append(section_text)

    return "\n\n---\n\n".join(sections)


def extract_problems(
    api_key: str,
    transcript: str,
    notes: str,
    model: str,
    model_fallback: str,
    interview_name: str = "",
    segment_hints: str = "",
    company_context: str = "",
    meeting_context: str = "",
) -> list[dict]:
    """Extract structured problems from a market interview using Google Gemini.

    Args:
        api_key: Google Gemini API key.
        transcript: The full interview transcript text.
        notes: User notes from Fibery (may be empty).
        interview_name: Display name of the interview entity.
        segment_hints: Comma-separated market segments linked to the interview.
        model: Primary Gemini model.
        model_fallback: Fallback Gemini model.
        company_context: Company-specific context.
        meeting_context: Dynamic per-meeting context (participants, orgs).

    Returns:
        List of problem dicts with keys matching PROBLEM_EXTRACTION_SCHEMA item properties.
    """
    import json
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options={"timeout": _SUMMARY_REQUEST_TIMEOUT_MS},
    )

    context = company_context.strip() if company_context.strip() else DEFAULT_COMPANY_CONTEXT
    system_prompt = PROBLEM_EXTRACTION_PROMPT
    if context:
        system_prompt += f"\n\nCompany context:\n{context}"
    if meeting_context.strip():
        system_prompt += f"\n\nMeeting participants and context:\n{meeting_context}"

    segment_line = f"Market segments: {segment_hints}" if segment_hints else ""
    user_message = PROBLEM_EXTRACTION_USER_TEMPLATE.format(
        interview_name=interview_name or "Unknown",
        segment_hints=segment_line,
        notes_text=notes or "(none)",
        transcript_text=transcript or "(none)",
    )

    models_to_try = [m for m in [model, model_fallback] if m]
    seen_keys: set = set()
    max_retries = 2

    for attempt in range(max_retries):
        for current_model in models_to_try:
            try:
                logger.info(
                    "Extracting problems with Gemini model=%s (attempt %d)",
                    current_model,
                    attempt + 1,
                )
                response = client.models.generate_content(
                    model=current_model,
                    contents=user_message,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.2,
                        response_mime_type="application/json",
                        response_schema=PROBLEM_EXTRACTION_SCHEMA,
                    ),
                )
                data = json.loads(response.text)
                problems = data.get("problems", [])

                # Dedup within run by exact (struggle_with, when_they, in_order_to_achieve, based_on) tuple
                unique = []
                for p in problems:
                    key = (
                        p.get("struggle_with", ""),
                        p.get("when_they", ""),
                        p.get("in_order_to_achieve", ""),
                        p.get("based_on", ""),
                    )
                    if key in seen_keys:
                        logger.info("Skipping duplicate problem: %.60s", key[0])
                        continue
                    seen_keys.add(key)
                    unique.append(p)

                logger.info("Extracted %d problems (%d unique)", len(problems), len(unique))
                return unique

            except Exception as exc:
                if _is_retryable_gemini_error(exc):
                    logger.warning("Falling back from %s for problem extraction: %s", current_model, exc)
                    continue
                logger.error("Unexpected error with %s during problem extraction: %s", current_model, exc)
                raise

        if attempt < max_retries - 1:
            sleep_time = 5 * (attempt + 1)
            logger.warning("Both models unavailable for problem extraction. Retrying in %d seconds...", sleep_time)
            time.sleep(sleep_time)

    raise RuntimeError(f"Problem extraction failed: Models {models_to_try} are experiencing high demand.")


_LANGUAGE_NAMES = {
    "en": "English",
    "nl": "Dutch",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "da": "Danish",
    "sv": "Swedish",
    "no": "Norwegian",
    "fi": "Finnish",
    "pl": "Polish",
}


_AUDIO_MIME_TYPES = {
    ".ogg": "audio/ogg",
    ".mp3": "audio/mp3",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".webm": "audio/webm",
}


def _upload_audio_for_cleanup(client, audio_path: str):
    """Upload an audio file to Gemini's File API for multimodal cleanup."""
    from pathlib import Path

    path = Path(audio_path)
    suffix = path.suffix.lower()
    mime = _AUDIO_MIME_TYPES.get(suffix)
    if not mime:
        logger.warning("Unsupported audio format for cleanup: %s", suffix)
        return None
    if not path.exists():
        logger.warning("Audio file not found for cleanup: %s", audio_path)
        return None

    try:
        uploaded = client.files.upload(file=str(path), config={"mime_type": mime})
        logger.info("Uploaded %s to Gemini File API (%s)", path.name, mime)
        return uploaded
    except Exception as exc:
        logger.warning("Audio upload to Gemini failed, continuing text-only: %s", exc)
        return None


def _delete_gemini_file(api_key: str, file_name: str) -> None:
    """Best-effort delete of a Gemini File API upload."""
    from google import genai

    try:
        client = genai.Client(
            api_key=api_key,
            http_options={"timeout": _FILE_DELETE_TIMEOUT_MS},
        )
        client.files.delete(name=file_name)
        logger.debug("Deleted Gemini file %s", file_name)
    except Exception:
        logger.debug("Failed to delete Gemini file", exc_info=True)


def _schedule_gemini_file_delete(api_key: str, file_ref) -> None:
    """Delete a Gemini upload in the background so cleanup never blocks the UI."""
    file_name = getattr(file_ref, "name", "")
    if not file_name:
        return

    threading.Thread(
        target=_delete_gemini_file,
        args=(api_key, file_name),
        name="gemini-file-delete",
        daemon=True,
    ).start()


def _cleanup_output_is_suspiciously_short(source: str, cleaned: str) -> bool:
    """Return True when cleanup output looks more like a summary than a transcript."""
    source_words = re.findall(r"\b[\w']+\b", source or "")
    if len(source_words) < _CLEANUP_LENGTH_GUARD_MIN_WORDS:
        return False

    cleaned_words = re.findall(r"\b[\w']+\b", cleaned or "")
    if not cleaned_words:
        return True

    source_chars = len(re.sub(r"\s+", "", source or ""))
    if source_chars == 0:
        return False

    cleaned_chars = len(re.sub(r"\s+", "", cleaned or ""))
    word_ratio = len(cleaned_words) / len(source_words)
    char_ratio = cleaned_chars / source_chars
    return (
        word_ratio < _CLEANUP_LENGTH_GUARD_MIN_WORD_RATIO
        and char_ratio < _CLEANUP_LENGTH_GUARD_MIN_CHAR_RATIO
    )


def _is_transcript_speaker_header(line: str) -> bool:
    """Return True when the line looks like a standalone markdown speaker label."""
    stripped = (line or "").strip()
    return stripped.startswith("**") and stripped.endswith("**") and len(stripped) > 4


def _split_transcript_for_cleanup(
    transcript: str,
    max_chars: int | None = None,
    max_blocks: int | None = None,
) -> list[str]:
    """Split long transcripts into speaker-block chunks for more reliable cleanup."""
    max_chars = max_chars or _CLEANUP_MAX_CHARS_PER_REQUEST
    max_blocks = max_blocks or _CLEANUP_MAX_BLOCKS_PER_REQUEST
    text = (transcript or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    blocks = []
    current_block = []
    for line in text.splitlines():
        if _is_transcript_speaker_header(line) and current_block:
            blocks.append("\n".join(current_block).strip())
            current_block = [line.strip()]
            continue

        if not current_block and not line.strip():
            continue
        current_block.append(line)

    if current_block:
        blocks.append("\n".join(current_block).strip())

    if len(blocks) <= 1:
        return [text]

    chunks = []
    current_chunk = []
    current_len = 0

    for block in blocks:
        block_len = len(block) + (2 if current_chunk else 0)
        should_flush = (
            current_chunk
            and (
                current_len + block_len > max_chars
                or len(current_chunk) >= max_blocks
            )
        )
        if should_flush:
            chunks.append("\n\n".join(current_chunk).strip())
            current_chunk = [block]
            current_len = len(block)
        else:
            current_chunk.append(block)
            current_len += block_len

    if current_chunk:
        chunks.append("\n\n".join(current_chunk).strip())

    return chunks or [text]


def _build_cleanup_user_prompt(
    transcript: str,
    notes: str = "",
    chunk_index: int = 1,
    chunk_count: int = 1,
) -> str:
    """Build the user prompt for transcript cleanup."""
    prefix = ""
    if chunk_count > 1:
        prefix = (
            f"Transcript chunk {chunk_index} of {chunk_count}. Clean only this chunk and output only the "
            "cleaned transcript for this chunk. Do not omit content because it might appear elsewhere.\n\n"
        )

    if notes.strip():
        return f"{prefix}Meeting notes:\n{notes.strip()}\n\nTranscript:\n{transcript}"
    if prefix:
        return f"{prefix}Transcript:\n{transcript}"
    return transcript


def _cleanup_transcript_chunk(
    client,
    *,
    transcript: str,
    system_prompt: str,
    models_to_try: list[str],
    user_prompt: str,
    audio_ref=None,
) -> str:
    """Run Gemini cleanup for a single transcript chunk."""
    from google.genai import types

    contents = [audio_ref, user_prompt] if audio_ref else user_prompt
    saw_overcompressed_output = False

    for current_model in models_to_try:
        try:
            mode = "audio-assisted" if audio_ref else "text-only"
            logger.info("Cleaning transcript with Gemini model=%s (%s)", current_model, mode)
            response = client.models.generate_content(
                model=current_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.2,
                ),
            )
            cleaned = (response.text or "").strip()
            if _cleanup_output_is_suspiciously_short(transcript, cleaned):
                saw_overcompressed_output = True
                logger.warning(
                    "Discarding suspiciously short cleanup output from %s (%d -> %d chars)",
                    current_model,
                    len(transcript),
                    len(cleaned),
                )
                continue
            logger.info("Transcript cleanup complete (%d -> %d chars)", len(transcript), len(cleaned))
            return cleaned

        except Exception as exc:
            if _is_retryable_gemini_error(exc):
                logger.warning("Falling back from %s for cleanup: %s", current_model, exc)
                continue

            logger.error("Transcript cleanup failed with %s: %s", current_model, exc)
            raise

    if saw_overcompressed_output:
        logger.warning("Cleanup output looked summarized; using raw transcript instead")
        return transcript

    raise RuntimeError(f"Transcript cleanup failed: Models {models_to_try} all unavailable.")


def cleanup_transcript(
    api_key: str,
    transcript: str,
    model: str,
    model_fallback: str,
    notes: str = "",
    language: str = "en",
    meeting_context: str = "",
    company_context: str = "",
    audio_path: str = "",
) -> str:
    """Clean up a raw transcript using Gemini: fix names, sentences, and formatting.

    When *audio_path* points to a valid audio file the recording is uploaded
    to Gemini's File API and sent alongside the text so the model can
    cross-reference the audio to correct misheard words and verify speakers.

    Args:
        api_key: Google Gemini API key.
        transcript: Raw formatted transcript from AssemblyAI.
        notes: Fibery meeting notes or other trusted meeting context.
        language: Detected language code (e.g. "en", "nl").
        meeting_context: Participant names and organizations from Fibery.
        company_context: Company-specific context (uses default if empty).
        model: Gemini model to use (flash for speed/cost).
        audio_path: Optional path to the compressed recording (OGG/FLAC/etc).

    Returns:
        Cleaned transcript as markdown string.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options={"timeout": _CLEANUP_REQUEST_TIMEOUT_MS},
    )

    lang_name = _LANGUAGE_NAMES.get(language, language)
    context = company_context.strip() if company_context.strip() else DEFAULT_COMPANY_CONTEXT

    system_prompt = TRANSCRIPT_CLEANUP_PROMPT.format(language=lang_name)

    audio_ref = None
    if audio_path:
        audio_ref = _upload_audio_for_cleanup(client, audio_path)
    if audio_ref:
        system_prompt += TRANSCRIPT_CLEANUP_AUDIO_ADDENDUM

    if meeting_context.strip():
        system_prompt += f"\n\nConfirmed meeting-specific context:\n{meeting_context}"
    system_prompt += (
        "\n\nGeneral company context (glossary only; not evidence that a person attended this meeting):\n"
        f"{context}"
    )
    models_to_try = []
    for candidate in (model, model_fallback):
        if candidate and candidate not in models_to_try:
            models_to_try.append(candidate)

    try:
        chunks = _split_transcript_for_cleanup(transcript)
        if len(chunks) > 1:
            logger.info(
                "Cleaning long transcript in %d chunks (max %d chars each)",
                len(chunks),
                _CLEANUP_MAX_CHARS_PER_REQUEST,
            )

        cleaned_chunks = []
        for index, chunk in enumerate(chunks, start=1):
            if len(chunks) > 1:
                logger.info("Cleaning transcript chunk %d/%d (%d chars)", index, len(chunks), len(chunk))

            user_prompt = _build_cleanup_user_prompt(
                chunk,
                notes=notes,
                chunk_index=index,
                chunk_count=len(chunks),
            )
            cleaned_chunks.append(
                _cleanup_transcript_chunk(
                    client,
                    transcript=chunk,
                    system_prompt=system_prompt,
                    models_to_try=models_to_try,
                    user_prompt=user_prompt,
                    audio_ref=audio_ref,
                )
            )

        return "\n\n".join(part for part in cleaned_chunks if part).strip()
    finally:
        if audio_ref:
            _schedule_gemini_file_delete(api_key, audio_ref)
