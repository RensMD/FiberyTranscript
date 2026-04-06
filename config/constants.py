"""Application constants and prompt templates."""

APP_NAME = "Fibery Transcript"
APP_VERSION = "1.4.5"
APP_WINDOW_TITLE = "FiberyTranscript"
APP_AUTOSTART_REG_VALUE = "FiberyTranscript"
APP_LEGACY_AUTOSTART_REG_VALUES = ("Fibery Transcript",)
APP_SINGLE_INSTANCE_MUTEX_NAME = r"Local\FiberyTranscript.SingleInstance"

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

# Core prompt (always included, cannot be changed by user)
CORE_PROMPT = """Your task is to analyze the provided user notes and auto-generated transcript. \
The speakers in the meeting are non English natives (mostly Dutch). The transcript likely contains small talk, \
misheard words, grammar errors, and mistakes with names; use the meeting context provided to resolve naming ambiguities. \
The provided notes are written on the fly by meeting participants and may be incomplete, unstructured, or contain errors."""


# Default meeting prompt (overwritten when user provides a custom prompt)
DEFAULT_MEETING_PROMPT = """You are a professional meeting summarizer.\
    Keep the summary clear, professional, and on topic."""

# Interview prompt (switched to when interview mode is selected)
DEFAULT_INTERVIEW_PROMPT = """You are a professional interview summarizer. Your task is to analyze the \
provided user notes and generated transcript to create a summary. \
Keep the summary clear, professional, and focused on the following topics: \
JOB TO BE DONE, ACTIVITIES, PROBLEMS, SEARCH FOR ALTERNATIVES, CONSIDERATIONS, ALTERNATIVES, COMPLAINTS. \
At the end of your summary, please create a list of problem definition suggestions \
(depending on the interview create between zero and six definitions. Please create a separate \
definition per problem, no mixing of problems.) Follow this template exactly:

**We believe that:** [Market Segment]
**Struggle with:** [Problem]
**When they:** [Activity]
**In order to:** [Job to be done]
**Based on:** [Considerations]
**They solve this now by:** [Alternatives]
**The downside is:** [Complaints]
**They are searching for alternatives by:** [Search approach]"""

# Shareable summary prompt (professional external-facing summary)
DEFAULT_SHAREABLE_PROMPT = """You are a professional meeting summarizer creating a summary to share with \
external meeting participants. Focus on key outcomes, decisions, action items with owners, and next steps. \
Keep it concise and professional. Avoid internal jargon, confidential strategic context, and anything not \
appropriate for external sharing. Start with a brief meeting overview, then list decisions and action items."""

# Maps prompt type keys to their role prompt constants
PROMPT_TYPE_PROMPTS = {
    "summarize": DEFAULT_MEETING_PROMPT,
    "interview": DEFAULT_INTERVIEW_PROMPT,
    "shareable": DEFAULT_SHAREABLE_PROMPT,
}

# Maps prompt type keys to display labels for section headers
PROMPT_TYPE_LABELS = {
    "summarize": "Summary",
    "interview": "Problem Interview",
    "shareable": "Shareable Summary",
    "custom": "Custom",
}

# Transcript cleanup prompt (used by Gemini to clean up raw AssemblyAI output)
TRANSCRIPT_CLEANUP_PROMPT = """You are a transcript checker. Your task is to clean up \
an auto-generated meeting transcript without turning it into a summary. Use the meeting context to resolve name \
ambiguities and fix obvious transcription errors. Instructions:
- The detected transcript language is {language}. Keep the entire output in {language}. Never translate, localize, \
or rewrite it into another language.
- This is transcript cleanup, not summarization. Do not summarize, condense, paraphrase, reorder, or rewrite the \
meeting into a shorter narrative.
- Preserve full content coverage. Keep every substantive statement, question, answer, decision, example, and action \
item from the source transcript.
- Keep the original turn order. Unless you are removing a clear duplicate echo, every input speaker turn should \
still be represented in the output.
- Only replace a generic speaker label with a real name when the identity is directly supported by \
meeting-specific evidence such as the confirmed participant list, meeting notes, the transcript itself, \
or the attached audio.
- Never assign a speaker name based only on general company context, a glossary, or a list of possible names. \
General company context is only for spelling correction and term disambiguation, not proof that someone attended.
- If there is any doubt, keep the label generic and consistent rather than guessing.
- When two nearby speaker turns contain identical or almost identical text and one is clearly a duplicate echo, \
keep the source version and remove the echoed duplicate.
- If only part of a nearby speaker turn is duplicated (for example the first one or two sentences are repeated \
and the rest is new), remove the duplicated portion and keep the unique remainder.
- If the duplicate appears on two channels and the later/source copy is on Channel 1 while the earlier duplicate \
is on Channel 0, prefer keeping Channel 1 and removing the duplicate Channel 0 text.
- Format speaker label as **Name:** on its own line, followed by the text.
- Fix obvious transcription errors: broken sentences, misheard words, grammar issues, punctuation, and capitalization.
- Remove only standalone filler words or short verbal tics such as Yeah, Uh, Um, Like, You know, or a lonely \
"and" when they add no meaning. Do not remove meaningful phrases, partial sentences, or hedging that carries content.
- If you are unsure whether text is filler or meaningful, keep it.
- Preserve the original meaning and level of detail; do not add, remove, or change what was said beyond the cleanup \
rules above.
- Do not add section summaries. Output a full cleaned transcript only.
- No em-dashes."""

# Audio-assisted variant: appended when the recording audio is also provided
TRANSCRIPT_CLEANUP_AUDIO_ADDENDUM = """
The original meeting audio is attached. Do NOT re-transcribe from scratch; improve the provided transcript only. Use it to:
- Verify and correct words that the automatic transcription may have misheard, \
especially names, technical terms, and non-English words.
- Resolve speaker identification where the text alone is ambiguous. Listen for \
voice differences to confirm who is speaking. 
- Keep full transcript coverage. Use the audio to confirm and correct the existing transcript, not to shorten it.
- Keep the transcript in its detected source language. Never translate.
"""

# --- Problem extraction prompts ---

PROBLEM_EXTRACTION_PROMPT = """You are a market research analyst specializing in problem discovery \
and the Jobs-to-be-Done (JTBD) framework. Your task is to extract distinct customer problems \
from a market interview.

For each problem identified, fill in all fields of the structured template using only evidence \
directly supported by the interview. If no clear evidence exists for a particular field, leave it blank. Do not speculate or infer beyond the provided content.

Field intent:
- struggle_with: The specific pain point or challenge the customer faces (concise problem statement)
- when_they: The activity or situation during which the struggle occurs
- in_order_to_achieve: The underlying goal or job-to-be-done the customer is trying to accomplish
- based_on: The considerations, context, or constraints that make this a real problem for them
- they_solve_this_now_by: Current workarounds, tools, or alternatives they use today
- the_downside_is: Complaints, limitations, or frustrations with their current solution
- they_are_searching_by: How they look for better alternatives (search terms, channels, methods)
- evidence: Direct quotes or paraphrased statements from the interview that support this problem
- confidence: Integer 0-100 indicating how strongly the evidence supports this problem
- urgency: How urgent this problem is for the customer; must be exactly one of: \
Hair on fire, High, Medium, Low
- frequency: How often the customer encounters this problem; must be exactly one of: \
Daily, Weekly, Monthly, Quarterly, Yearly, Once

Splitting policy: Create a separate entry per distinct problem. Do not merge unrelated problems. \
Only include problems clearly supported by the interview content. \
Omit speculative problems not grounded in the interview. \
Provide concise input to fields, avoid unnecessary/invented elaboration.
Aim  for at least 3 problems per interview if evidence is available."""

PROBLEM_EXTRACTION_USER_TEMPLATE = """Interview: {interview_name}
{segment_hints}
Notes:
{notes_text}

Transcript:
{transcript_text}"""

PROBLEM_EXTRACTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "problems": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "struggle_with": {"type": "STRING"},
                    "when_they": {"type": "STRING"},
                    "in_order_to_achieve": {"type": "STRING"},
                    "based_on": {"type": "STRING"},
                    "they_solve_this_now_by": {"type": "STRING"},
                    "the_downside_is": {"type": "STRING"},
                    "they_are_searching_by": {"type": "STRING"},
                    "evidence": {"type": "STRING"},
                    "confidence": {"type": "INTEGER"},
                    "urgency": {"type": "STRING"},
                    "frequency": {"type": "STRING"},
                },
            },
        }
    },
}
