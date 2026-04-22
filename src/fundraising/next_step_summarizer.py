"""LLM call that condenses classified tasks into the four Affinity Next Step
fields on the matched LP row.

Output shape::

    {
      "drop_down_option_id": int | None,    # maps to Nzyme next step [DROP-DOWN]
      "follow_up_option_id": int | None,    # maps to Nzyme next step: Follow Up Date
      "details_text": str,                  # plain text summary for DETAILS (append)
      "owner_notion_user_id": str | None,   # best-effort internal owner
    }

Enum options are supplied live (fetched from Affinity at call time) so the
prompt is never out of sync with the list's configured dropdowns.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

logger = logging.getLogger(__name__)

FIELD_ID_NEXT_STEP_DROPDOWN = "field-5175722"
FIELD_ID_FOLLOW_UP_DATE = "field-5171600"
FIELD_ID_OWNER = "field-5432855"
FIELD_ID_DETAILS = "field-5437596"


SYSTEM_PROMPT = """You summarise the outcome of a fundraising meeting with a prospective
limited partner (LP) into four Affinity CRM fields. Output is JSON that strictly
matches the provided schema.

Rules:
- Pick the single most representative "next step" option from the list. If no
  option clearly fits, return null — do NOT invent one.
- Pick the single follow-up quarter that best matches the earliest relevant
  due date across the tasks. If no tasks have dates, return null.
- Keep the details summary to 1–3 short sentences in the meeting language
  (English unless the meeting was clearly Spanish). Focus on what the Nzyme
  team committed to do next for this LP, not what the LP said.
- Owner: pick the Notion user id of the Kibo team member who will own the
  follow-up. Prefer the assignee_id list on the tasks. If no internal owner is
  clear, return null — the caller will fall back to the meeting creator."""


def _format_option_list(options: list[dict[str, Any]]) -> str:
    lines = []
    for opt in options:
        text = opt.get("text") or opt.get("name") or ""
        opt_id = opt.get("id") or opt.get("dropdownOptionId")
        if text and opt_id is not None:
            lines.append(f"- {text!r} (id={opt_id})")
    return "\n".join(lines) if lines else "(no options available)"


def _extract_dropdown_options(
    fields: list[dict[str, Any]], field_id: str,
) -> list[dict[str, Any]]:
    """Pull ``dropdown_options`` for a given field from V1 fields response.

    V1 returns each field with a ``dropdown_options`` array when it's a
    dropdown or ranked-dropdown.
    """
    for field in fields:
        # V1 field id is a bare int; V2 prefixes with "field-". Compare both.
        raw_id = field.get("id")
        fid_str = f"field-{raw_id}" if isinstance(raw_id, int) else str(raw_id)
        if fid_str == field_id or str(raw_id) == field_id:
            return field.get("dropdown_options") or []
    logger.warning("No dropdown_options found for %s in Affinity fields", field_id)
    return []


def summarize_next_step(
    *,
    openai_client: OpenAI,
    model: str,
    classified_tasks: list[dict[str, Any]],
    affinity_fields: list[dict[str, Any]],
    meeting_title: str,
    meeting_date: str,
    creator_name: str,
) -> dict[str, Any]:
    """Run the LLM summarizer and return the four-field payload."""
    dropdown_opts = _extract_dropdown_options(
        affinity_fields, FIELD_ID_NEXT_STEP_DROPDOWN,
    )
    follow_up_opts = _extract_dropdown_options(
        affinity_fields, FIELD_ID_FOLLOW_UP_DATE,
    )

    user_prompt = (
        f"=== MEETING ===\n"
        f"Title: {meeting_title}\n"
        f"Date: {meeting_date}\n"
        f"Creator: {creator_name}\n\n"
        f"=== NEXT STEP OPTIONS (pick one, by id) ===\n"
        f"{_format_option_list(dropdown_opts)}\n\n"
        f"=== FOLLOW-UP QUARTER OPTIONS (pick one, by id) ===\n"
        f"{_format_option_list(follow_up_opts)}\n\n"
        f"=== TASKS EXTRACTED FROM THE MEETING ===\n"
        f"{json.dumps(classified_tasks, indent=2, default=str)}"
    )

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "drop_down_option_id",
            "follow_up_option_id",
            "details_text",
            "owner_notion_user_id",
        ],
        "properties": {
            "drop_down_option_id": {"type": ["integer", "null"]},
            "follow_up_option_id": {"type": ["integer", "null"]},
            "details_text": {"type": "string"},
            "owner_notion_user_id": {"type": ["string", "null"]},
        },
    }

    resp = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "nzyme_next_step",
                "strict": True,
                "schema": schema,
            },
        },
    )
    content = resp.choices[0].message.content or "{}"
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        logger.exception("Summarizer returned non-JSON: %s", content[:500])
        raise

    # Validate enum ids against the fetched options.
    valid_dropdown = {
        opt.get("id") or opt.get("dropdownOptionId") for opt in dropdown_opts
    }
    valid_follow_up = {
        opt.get("id") or opt.get("dropdownOptionId") for opt in follow_up_opts
    }
    if result.get("drop_down_option_id") not in valid_dropdown | {None}:
        logger.warning(
            "Summarizer picked invalid drop_down_option_id=%s — nulling",
            result.get("drop_down_option_id"),
        )
        result["drop_down_option_id"] = None
    if result.get("follow_up_option_id") not in valid_follow_up | {None}:
        logger.warning(
            "Summarizer picked invalid follow_up_option_id=%s — nulling",
            result.get("follow_up_option_id"),
        )
        result["follow_up_option_id"] = None

    return result


__all__ = [
    "summarize_next_step",
    "FIELD_ID_NEXT_STEP_DROPDOWN",
    "FIELD_ID_FOLLOW_UP_DATE",
    "FIELD_ID_OWNER",
    "FIELD_ID_DETAILS",
]
