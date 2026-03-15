"""Google Gemini summarization client."""

import logging
import time

from config.constants import (
    DEFAULT_INTERVIEW_PROMPT, DEFAULT_MEETING_PROMPT, CORE_PROMPT,
    DEFAULT_COMPANY_CONTEXT, TRANSCRIPT_CLEANUP_PROMPT,
    TRANSCRIPT_CLEANUP_AUDIO_ADDENDUM,
)

logger = logging.getLogger(__name__)

SUMMARY_STYLE_INSTRUCTIONS = {
    "normal": " ",
    "short": "The user wants an extra concise result, so limit your output (keep only key information.",
    "minimal": "Use minimal output: 3-5 short bullets with only critical outcomes.",
}


def _normalize_summary_style(summary_style: str) -> str:
    """Return a supported summary style, defaulting to normal."""
    style = (summary_style or "").strip().lower()
    return style if style in SUMMARY_STYLE_INSTRUCTIONS else "normal"


def summarize_transcript(
    api_key: str,
    transcript: str,
    notes: str,
    is_interview: bool,
    custom_prompt: str = "",
    summary_style: str = "normal",
    model: str = "gemini-3.1-pro-preview",
    model_fallback: str = "gemini-3-flash-preview",
    company_context: str = "",
    meeting_context: str = "",
) -> str:
    """Summarize a meeting transcript using Google Gemini.

    Args:
        api_key: Google Gemini API key.
        transcript: The full meeting transcript text.
        notes: User notes from Fibery (may be empty).
        is_interview: True to use interview prompt, False for meeting prompt.
        custom_prompt: Additional user instructions that replace the default role prompt.
        summary_style: Output length style: normal, short, or minimal.
        company_context: Company-specific context (uses default if empty).
        meeting_context: Dynamic per-meeting context (participants, orgs).

    Returns:
        The AI-generated summary text.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options={"timeout": 120_000},  # 120s for long transcripts
    )

    primary_model = model
    fallback_model = model_fallback

    # Build role prompt: custom_prompt replaces the default when provided
    if custom_prompt.strip():
        role_prompt = custom_prompt.strip()
    else:
        role_prompt = DEFAULT_INTERVIEW_PROMPT if is_interview else DEFAULT_MEETING_PROMPT

    # Compose full system prompt: role + core + company context + meeting context + style
    context = company_context.strip() if company_context.strip() else DEFAULT_COMPANY_CONTEXT
    system_prompt = f"""{role_prompt}

{CORE_PROMPT}

{context}"""

    if meeting_context.strip():
        system_prompt += f"\n\nMeeting participants and context:\n{meeting_context}"
        system_prompt += "\nUse the participant names above to identify speakers where possible."

    normalized_style = _normalize_summary_style(summary_style)
    system_prompt += (
        f"\n\nSummary style setting: {normalized_style}\n"
        f"{SUMMARY_STYLE_INSTRUCTIONS[normalized_style]}"
    )

    user_message = f"User notes: {notes}\n\nTranscript:\n{transcript}"

    models_to_try = [primary_model, fallback_model]
    max_retries = 2

    for attempt in range(max_retries):
        for current_model in models_to_try:
            try:
                logger.info(
                    "Summarizing with Gemini model=%s (attempt %d)", 
                    current_model, attempt + 1
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

            except Exception as e:
                # Check for retryable errors (capacity/rate limits/deprecated model)
                # by type first, then fall back to string matching for wrapped exceptions.
                retryable = False
                try:
                    from google.api_core.exceptions import ServiceUnavailable, ResourceExhausted, TooManyRequests, NotFound
                    retryable = isinstance(e, (ServiceUnavailable, ResourceExhausted, TooManyRequests, NotFound))
                except ImportError:
                    pass
                if not retryable:
                    error_msg = str(e)
                    retryable = "503" in error_msg or "429" in error_msg or "404" in error_msg

                if retryable:
                    logger.warning("Falling back from %s: %s", current_model, e)
                    continue  # Immediately loop to the fallback model
                else:
                    logger.error("Unexpected error with %s: %s", current_model, e)
                    raise  # Reraise if it is a 400 Bad Request (e.g., malformed prompt)

        # If both models fail in a single pass, wait a moment before trying the loop again
        if attempt < max_retries - 1:
            sleep_time = 5 * (attempt + 1)
            logger.warning("Both models unavailable. Retrying in %d seconds...", sleep_time)
            time.sleep(sleep_time)

    # If it fails all retries across all models
    raise RuntimeError(f"Summarization failed: Models {models_to_try} are experiencing high demand.")


# Language code to display name mapping for common AssemblyAI language codes
_LANGUAGE_NAMES = {
    "en": "English", "nl": "Dutch", "de": "German", "fr": "French",
    "es": "Spanish", "it": "Italian", "pt": "Portuguese", "da": "Danish",
    "sv": "Swedish", "no": "Norwegian", "fi": "Finnish", "pl": "Polish",
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
    """Upload an audio file to Gemini's File API for multimodal cleanup.

    Returns a File reference on success, or None on failure.
    """
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
    except Exception as e:
        logger.warning("Audio upload to Gemini failed, continuing text-only: %s", e)
        return None


def _delete_gemini_file(client, file_ref) -> None:
    """Best-effort delete of a Gemini File API upload."""
    try:
        client.files.delete(name=file_ref.name)
        logger.debug("Deleted Gemini file %s", file_ref.name)
    except Exception:
        logger.debug("Failed to delete Gemini file", exc_info=True)


def cleanup_transcript(
    api_key: str,
    transcript: str,
    language: str = "en",
    meeting_context: str = "",
    company_context: str = "",
    model: str = "gemini-2.5-flash-lite",
    audio_path: str = "",
) -> str:
    """Clean up a raw transcript using Gemini: fix names, sentences, add sections.

    When *audio_path* points to a valid audio file the recording is uploaded
    to Gemini's File API and sent alongside the text so the model can
    cross-reference the audio to correct misheard words and verify speakers.

    Args:
        api_key: Google Gemini API key.
        transcript: Raw formatted transcript from AssemblyAI.
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
        http_options={"timeout": 300_000},  # 5 min — audio uploads can be large
    )

    lang_name = _LANGUAGE_NAMES.get(language, language)
    context = company_context.strip() if company_context.strip() else DEFAULT_COMPANY_CONTEXT

    system_prompt = TRANSCRIPT_CLEANUP_PROMPT.format(language=lang_name)

    # Upload audio if a path was provided
    audio_ref = None
    if audio_path:
        audio_ref = _upload_audio_for_cleanup(client, audio_path)
    if audio_ref:
        system_prompt += TRANSCRIPT_CLEANUP_AUDIO_ADDENDUM

    if meeting_context.strip():
        system_prompt += f"\n{meeting_context}"
    system_prompt += f"\n\nGeneral context:\n{context}"

    # Build contents: text-only or [audio, text] multimodal
    if audio_ref:
        contents = [audio_ref, transcript]
    else:
        contents = transcript

    fallback_model = "gemini-3.1-flash-lite-preview"
    models_to_try = [model, fallback_model]

    try:
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
                cleaned = response.text
                logger.info("Transcript cleanup complete (%d → %d chars)", len(transcript), len(cleaned))
                return cleaned

            except Exception as e:
                retryable = False
                try:
                    from google.api_core.exceptions import ServiceUnavailable, ResourceExhausted, TooManyRequests, NotFound
                    retryable = isinstance(e, (ServiceUnavailable, ResourceExhausted, TooManyRequests, NotFound))
                except ImportError:
                    pass
                if not retryable:
                    error_msg = str(e)
                    retryable = "503" in error_msg or "429" in error_msg or "404" in error_msg

                if retryable:
                    logger.warning("Falling back from %s for cleanup: %s", current_model, e)
                    continue
                else:
                    logger.error("Transcript cleanup failed with %s: %s", current_model, e)
                    raise

        raise RuntimeError(f"Transcript cleanup failed: Models {models_to_try} all unavailable.")
    finally:
        # Clean up the uploaded file regardless of success/failure
        if audio_ref:
            _delete_gemini_file(client, audio_ref)
