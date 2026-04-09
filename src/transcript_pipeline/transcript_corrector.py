"""LLM-based transcript correction using terminology dictionary and org chart."""

from __future__ import annotations

import logging

from openai import OpenAI

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a transcript correction assistant for Kibo Ventures, a PE/VC fund.

You will receive:
1. A TERMINOLOGY DICTIONARY of domain-specific terms with their common mistranscriptions
2. An ORG CHART of team members with their roles and typical topics
3. A list of MEETING ATTENDEES (people who were in this specific meeting)
4. A RAW TRANSCRIPT from Notion's automatic voice transcription

Your job is to correct transcription errors in the raw transcript. Rules:

CORRECTIONS:
- Fix domain-specific terms using the terminology dictionary (e.g., "civic lend" → "Civislend")
- Fix people's names using the org chart (e.g., "ed vinas" → "Edvinas")
- Infer speaker labels where possible using the attendee list and org chart context
- Correct obvious grammar/transcription artifacts while preserving meaning

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
        org_chart_context: str,
        attendees: list[dict[str, str]],
    ) -> str:
        """Send transcript + context to LLM and return corrected text.

        Args:
            transcript: Raw transcript text from Notion.
            terminology_context: Formatted terminology dictionary string.
            org_chart_context: Formatted org chart string.
            attendees: List of {"id": ..., "name": ...} dicts from the meeting.

        Returns:
            Corrected transcript text.
        """
        attendee_names = [a["name"] for a in attendees]
        attendee_str = ", ".join(attendee_names) if attendee_names else "(unknown)"

        user_prompt = f"""\
=== TERMINOLOGY DICTIONARY ===
{terminology_context if terminology_context else "(none provided)"}

=== ORG CHART ===
{org_chart_context if org_chart_context else "(none provided)"}

=== MEETING ATTENDEES ===
{attendee_str}

=== RAW TRANSCRIPT ===
{transcript}
"""

        logger.info(
            "Sending transcript to %s for correction (%d chars, %d attendees)",
            self._model,
            len(transcript),
            len(attendees),
        )

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )

        corrected = response.choices[0].message.content or ""

        logger.info(
            "Correction complete: %d input chars → %d output chars",
            len(transcript),
            len(corrected),
        )

        return corrected
