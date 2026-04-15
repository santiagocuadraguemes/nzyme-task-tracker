"""Lightweight task extraction from corrected transcripts."""

from __future__ import annotations

import json
import logging

from openai import OpenAI

from src.transcript_pipeline.token_usage import log_token_usage

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a task extraction assistant for Kibo Ventures, a PE/VC fund (~10-20 people).

You will receive a corrected meeting transcript along with organizational context. \
Extract all clear action items from the conversation.

## Commitment classification

Classify each commitment type you find:
- **Hard commitment**: "I will do X by Friday" → extract as action item, confidence: high
- **Conditional commitment**: "If Y happens, I'll do X" → extract with condition noted, confidence: medium
- **Soft delegation**: "Maybe Sarah could look at this" → extract with named assignee, confidence: medium
- **Group commitment**: "We should do X" / "We need to do X" → extract with confidence: low. \
Try to identify the 2-3 most likely responsible people based on topic alignment \
and roles. Use comma-separated names (e.g., "Santiago, Jacob"). Only use "Team" \
as a last resort when no specific people can be inferred.
- **Vague / follow-up**: "Let's circle back on X" → do NOT extract as a task

## Speaker & Assignee Resolution (CRITICAL)

Before assigning any task, determine the assignee using these signals (priority order):
1. Explicit speaker label from the transcript ("Santiago:" prefix)
2. Topic alignment: match the task's subject to each attendee's role, department, and typical_topics
3. Conversational flow: who was addressed in the preceding sentences?

**CONSISTENCY**: If multiple tasks relate to the same domain or initiative (e.g., several \
Notion-related tasks, or several deal-related tasks), they should be assigned to the \
same person unless there is explicit evidence of different assignees. Group related \
tasks mentally before assigning.

For each task, include a "speaker_reasoning" field (1 sentence) explaining your assignment logic.
NEVER default all ambiguous tasks to the same person — use topic alignment to distribute.

## Human Notes (HIGH PRIORITY)

If human notes are provided, they represent the note-taker's ground truth understanding \
of what happened in the meeting. Use them to:
- Confirm or disambiguate task assignments
- Identify action items the note-taker explicitly captured
- Resolve speaker identity when transcript labels are ambiguous
Human notes take priority over inferences from the transcript when they conflict.

## Rules

- Only extract concrete, actionable items — not vague discussion points or information sharing
- Every task MUST have a "context" field: a short quote from the transcript that justifies it
- If you cannot find supporting evidence in the transcript, do NOT create the task
- The transcript may be in English, Spanish, or mixed — extract tasks regardless of language
- Write task titles in the same language they were discussed in
- If multiple people are responsible for the same task, list them comma-separated in the \
assignee field (e.g., "Santiago, Jacob") — do NOT create separate tasks
- If a speaker refers to themselves ("I'll do it", "yo me encargo"), use speaker attribution \
or attendee context to determine who they are
- Use the org chart and attendee roles to resolve role-based references \
("the tech team should...", "operations needs to...")

## Output

Return a JSON object: {{"tasks": [...]}}

Each task object:
- "title": clear, actionable description (one sentence)
- "assignee": person(s) responsible — comma-separated names from attendees/org chart. \
Only use "Team" if absolutely no specific person can be inferred.
- "priority": "High" | "Medium" | "Low" based on urgency signals
- "due_date": ISO date (YYYY-MM-DD) if a deadline is mentioned, otherwise null
- "confidence": "high" | "medium" | "low"
- "context": short transcript quote (1-2 sentences) that justifies this task
- "speaker_reasoning": 1 sentence explaining why this task is assigned to this person

If no tasks are found, return {{"tasks": []}}.
"""


class TaskExtractor:
    """Extracts action items from a corrected transcript via LLM."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str | None = None,
    ) -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def extract(
        self,
        transcript: str,
        attendees: list[dict[str, str]],
        org_chart: str = "",
        terminology: str = "",
        meeting_title: str = "",
        meeting_date: str = "",
        enriched_attendee_str: str = "",
        notes_text: str = "",
    ) -> list[dict]:
        """Extract tasks from a corrected transcript.

        Returns:
            List of dicts with keys: title, assignee, priority, due_date,
            confidence, context, speaker_reasoning.
        """
        if not enriched_attendee_str:
            attendee_names = [a["name"] for a in attendees]
            enriched_attendee_str = ", ".join(attendee_names) if attendee_names else "(unknown)"

        sections = []

        if meeting_title:
            sections.append(f"=== MEETING ===\nTitle: {meeting_title}")
            if meeting_date:
                sections[-1] += f"\nDate: {meeting_date}"
        elif meeting_date:
            sections.append(f"=== MEETING ===\nDate: {meeting_date}")

        if meeting_date:
            sections.append(
                f"Today's date is {meeting_date}. Resolve relative dates "
                f"('tomorrow', 'next week', 'el viernes') relative to this date."
            )

        sections.append(f"=== MEETING ATTENDEES ===\n{enriched_attendee_str}")

        if notes_text:
            sections.append(
                f"=== HUMAN NOTES (high-priority context from the note-taker) ===\n{notes_text}"
            )

        if org_chart:
            sections.append(f"=== ORG CHART (team roles & responsibilities) ===\n{org_chart}")

        if terminology:
            sections.append(f"=== TERMINOLOGY ===\n{terminology}")

        sections.append(f"=== CORRECTED TRANSCRIPT ===\n{transcript}")

        user_prompt = "\n\n".join(sections)

        logger.info(
            "Extracting tasks from transcript with %s (%d chars, %d attendees)",
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
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content or "{}"

        log_token_usage(self._model, response.usage)

        data = json.loads(raw)
        tasks = data.get("tasks", [])

        logger.info("Extracted %d tasks", len(tasks))
        return tasks
