"""LLM-based transcript correction using terminology dictionary and org chart."""

from __future__ import annotations

import logging

from openai import OpenAI

from src.utils.llm_logging import log_usage

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a transcript correction assistant for Kibo Ventures, a PE/VC fund.

You will receive (in the system message) a TERMINOLOGY DICTIONARY of \
domain-specific terms with their common mistranscriptions.

You will then receive (in the user message):
1. A list of MEETING ATTENDEES with their roles, seniority, and typical topics
2. HUMAN NOTES taken by the note-taker during the meeting (high-priority ground truth)
3. A RAW TRANSCRIPT from Notion's automatic voice transcription

Your job is to correct transcription errors in the raw transcript. Rules:

CORRECTIONS:
- Fix domain-specific terms using the terminology dictionary (e.g., "civic lend" → "Civislend")
- Fix people's names using the attendee list (e.g., "ed vinas" → "Edvinas")
- Correct obvious grammar/transcription artifacts while preserving meaning

SPEAKER IDENTIFICATION (critical for downstream task extraction):
- HUMAN NOTES are the highest-priority signal. If notes attribute an action or topic \
to a specific person, use that to identify the speaker in the corresponding transcript segment.
- Match each segment's TOPIC to attendees' departments and typical_topics
- When a speaker says "I'll do X" / "yo me encargo", identify them by the TOPIC of X \
(e.g., if X is a tech/Notion task → the attendee with Technology department)
- Consider SENIORITY: senior members (Partner, Director) typically lead discussions, \
set the agenda, delegate tasks, and ask for status updates. Junior members more often \
receive assignments, report on execution details, and answer questions. Use this as a \
soft signal — not an absolute rule — when other cues are ambiguous.
- If you cannot confidently identify a speaker, use "[Unknown]:" rather than guessing wrong
- NEVER assign all unlabeled segments to the same person
- Use conversational cues: questions vs answers, "tú" / "you should" vs "I will"

CONSTRAINTS:
- Do NOT change the meaning or remove/add content
- Do NOT translate — keep the original language (Spanish, English, or mixed)
- Do NOT summarize or restructure — preserve the conversational flow
- If a correction is uncertain, mark it with [term?] (e.g., "[Civislend?]")
- Preserve timestamps and speaker transitions if present in the original

OUTPUT FORMAT:
- Return ONLY the corrected transcript text
- Use "Speaker Name:" labels when you can identify who is speaking
- Separate speaker turns with blank lines
"""


class TranscriptCorrector:
    """Corrects meeting transcripts using LLM + terminology/org chart context."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str | None = None,
    ) -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def correct(
        self,
        transcript: str,
        terminology_context: str,
        attendees: list[dict[str, str]],
        enriched_attendee_str: str = "",
        notes_text: str = "",
    ) -> str:
        """Send transcript + context to LLM and return corrected text.

        Args:
            transcript: Raw transcript text from Notion.
            terminology_context: Formatted terminology dictionary string.
            attendees: List of {"id": ..., "name": ...} dicts from the meeting.
            enriched_attendee_str: Pre-formatted attendee string with inline roles.
            notes_text: Human-written notes from the meeting (high-priority context).

        Returns:
            Corrected transcript text.
        """
        if not enriched_attendee_str:
            attendee_names = [a["name"] for a in attendees]
            enriched_attendee_str = ", ".join(attendee_names) if attendee_names else "(unknown)"

        # Stable prefix — system message holds the instructions + the
        # terminology dictionary, both reused across every meeting in a
        # sync tick. Putting them here maximises the OpenAI auto-cache
        # prefix (≥1024 tokens cached for ~5 min). Gemini's OpenAI-compat
        # endpoint ignores this today but the layout is harmless.
        system_message = (
            f"{SYSTEM_PROMPT}\n\n"
            f"=== TERMINOLOGY DICTIONARY ===\n"
            f"{terminology_context if terminology_context else '(none provided)'}"
        )

        # Variable per meeting — attendees, notes, transcript.
        user_prompt = f"""\
=== MEETING ATTENDEES ===
{enriched_attendee_str}

=== HUMAN NOTES (high-priority context from the note-taker) ===
{notes_text if notes_text else "(none provided)"}

=== RAW TRANSCRIPT ===
{transcript}
"""

        logger.debug(
            "Sending transcript to %s for correction (%d chars, %d attendees)",
            self._model,
            len(transcript),
            len(attendees),
        )

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_prompt},
            ],
        )

        corrected = response.choices[0].message.content or ""

        log_usage(response, self._model, stage="Correction", logger=logger)

        return corrected
