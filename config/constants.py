"""Application constants and prompt templates."""

APP_NAME = "Fibery Transcript"
APP_VERSION = "1.3.5"

_DEFAULT_FIBERY_INSTANCE_URL = "https://your-workspace.fibery.io"
_DEFAULT_COMPANY_CONTEXT = """Add your internal company context here to improve name disambiguation.
Example details:
- common organization names and product names
- common participant names and preferred spellings

Keep this file safe for public repos. Place sensitive context in config/private_context.py.
"""


def _load_private_context() -> tuple[str, str]:
    """Load local private overrides if available."""
    try:
        from config.private_context import DEFAULT_COMPANY_CONTEXT as private_company_context
        from config.private_context import FIBERY_INSTANCE_URL as private_instance_url
    except Exception:
        return _DEFAULT_FIBERY_INSTANCE_URL, _DEFAULT_COMPANY_CONTEXT

    resolved_instance_url = (
        private_instance_url.strip() if isinstance(private_instance_url, str) else ""
    )
    resolved_company_context = (
        private_company_context.strip() if isinstance(private_company_context, str) else ""
    )
    return (
        resolved_instance_url or _DEFAULT_FIBERY_INSTANCE_URL,
        resolved_company_context or _DEFAULT_COMPANY_CONTEXT,
    )


FIBERY_INSTANCE_URL, DEFAULT_COMPANY_CONTEXT = _load_private_context()

# Audio defaults
SAMPLE_RATE = 16000  # AssemblyAI expects 16kHz
CHANNELS = 1  # Mono
CHUNK_DURATION_MS = 100  # Audio chunk size in milliseconds
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_DURATION_MS // 1000  # 1600 samples per chunk
LEVEL_UPDATE_FPS = 30  # Audio level visualization update rate

# AssemblyAI
ASSEMBLYAI_REALTIME_SAMPLE_RATE = 16000

# Fibery
FIBERY_API_PATH = "/api/commands"

# --- Summarization prompts ---

# Default meeting prompt (overwritten when user provides a custom prompt)
DEFAULT_MEETING_PROMPT = """You are a professional meeting summarizer.\
    Keep the summary clear, professional, and focused on the topic at hand."""

# Interview prompt (switched to when interview mode is selected)
DEFAULT_INTERVIEW_PROMPT = """You are a professional interview summarizer. Your task is to analyze the \
provided user notes and generated transcript to create a summary. \
Keep the summary clear, professional, and focused on the following topics: \
JOB TO BE DONE, ACTIVITIES, PROBLEMS, SEARCH FOR ALTERNATIVES, CONSIDERATIONS, ALTERNATIVES, COMPLAINTS. \
At the end of your summary, please create a list of problem definition suggestions \
(depending on the interview create between zero and six definitions. Please create a separate \
definition per problem, no mixing of problems.) Follow this template:

**We believe that:** [Market Segment]
**Struggle with:** [Problem]
**When they:** [Activity]
**In order to:** [Job to be done]
**Based on:** [Considerations]
**They solve this now by:** [Alternatives]
**The downside is:** [Complaints]
**They are searching for alternatives by:** [Search approach]"""

# Core prompt (always included, cannot be changed by user)
CORE_PROMPT = """Your task is to analyze the provided user notes and auto generated transcript. \
The speakers in the meeting are non English natives (mostly Dutch). The transcript likely contains small talk, \
misheard words, grammar errors, and mistakes with names; use the meeting context provided to resolve name ambiguities. \
The provided notes are written on the fly by a meeting participant, and may be incomplete, unstructured, or contain errors."""

# Transcript cleanup prompt (used by Gemini to clean up raw AssemblyAI output)
TRANSCRIPT_CLEANUP_PROMPT = """You are a transcript checker. Your task is to clean up \
an auto-generated meeting transcript. Remove filler words. Use the meeting context to resolve name ambiguities and fix obvious transcription errors. \

Instructions:
- Identify speakers by name using the meeting context below. Replace generic \
"Speaker A/B/C" labels with real names where you can confidently identify them. \
If uncertain about a speaker's identity, use a descriptive label like \
"[Internal participant]" or "[External participant]".
- Fix obvious transcription errors: broken sentences, misheard words, grammar issues.
- Keep the transcript in its original language. Do NOT translate.
- Preserve the original meaning do not add, remove, or change what was said.
- Split the transcript into a few broad thematic sections with short bold headers only.
- Keep the speaker label format as **Name:** on its own line, followed by their text.
- No em-dashes.
- No Yeah, Uh, Um, Like, You know, or lonely "and" or similar filler words. Remove them entirely."""

# Audio-assisted variant: appended when the recording audio is also provided
TRANSCRIPT_CLEANUP_AUDIO_ADDENDUM = """
The original meeting audio is attached. Do NOT re-transcribe from scratch; improve the provided transcript. Use it to:
- Verify and correct words that the automatic transcription may have misheard, \
especially names, technical terms, and non-English words.
- Resolve speaker identification where the text alone is ambiguous — listen for \
voice differences to confirm who is speaking.
"""
