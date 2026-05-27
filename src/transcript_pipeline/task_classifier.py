"""Classify extracted tasks for placement in the Team Task Tracker.

Loads a prompt template from Notion, fills ``{{HIERARCHY}}`` and
``{{TEAM_MEMBERS}}`` with token-compressed renderings of live context,
sends a dedicated LLM call (OpenAI Structured Outputs with the
``ClassificationOutput`` Pydantic schema), and translates the int-token
response back into Notion UUIDs + derives ``category`` from each task's
chosen Tier-0 ancestor.

The output schema is intentionally tiny — ``{"i", "p", "a"}`` — so the
classifier emits ~30-45 output tokens per task instead of the previous
~150-180. Heavy UUID fields are never sent over the wire. Strict
Structured Outputs requires every field present, so ``p`` is emitted as
``null`` (no parent fit) and ``a`` as ``[]`` (no internal assignee) —
both equivalent to "drop this field" downstream.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import OpenAI

from src.transcript_pipeline.schemas import ClassificationOutput
from src.utils.llm_dump import dump_call
from src.utils.llm_logging import log_usage

logger = logging.getLogger(__name__)

# Reasoning models (gpt-5*, o-series) accept ``reasoning_effort``. Plain
# chat models (gpt-4o-mini, the test stub) reject the kwarg, so we only
# send it when the model supports it.
_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _supports_reasoning_effort(model: str) -> bool:
    m = model.lower()
    return any(m.startswith(p) for p in _REASONING_MODEL_PREFIXES)


def _substitute_placeholders(template: str, **kwargs: str) -> str:
    """Replace ``{{KEY}}`` markers in a template string with values."""
    for key, value in kwargs.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template


def _build_hierarchy_token_map(
    hierarchy: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, str], dict[int, str]]:
    """DFS-walk the hierarchy, assigning a small integer token per node.

    Returns three things:
      - ``compact_tree``: the same tree shape with keys renamed for
        token economy — ``{n, t, notes?, c?}`` instead of
        ``{id, title, notes?, children}``. ``notes`` is omitted when
        empty; ``c`` is omitted when there are no children.
      - ``token_to_uuid``: ``n`` → tracker page UUID (used by the
        unpacker to translate the model's emitted ``p`` back to a real
        ``parent_task_id``).
      - ``token_to_category``: ``n`` → the category string of the
        node's Tier-0 ancestor. Used to derive each task's
        ``category`` (a tracker select option) without the model
        having to emit it.
    """
    token_to_uuid: dict[int, str] = {}
    token_to_category: dict[int, str] = {}
    counter = [0]

    def _walk(
        nodes: list[dict[str, Any]], root_category: str,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for node in nodes:
            counter[0] += 1
            n = counter[0]
            token_to_uuid[n] = node["id"]
            # Tier-0 roots carry their own category in the source data.
            # For deeper nodes inherit the root's category.
            category = (node.get("category") or "").strip() or root_category
            token_to_category[n] = category
            entry: dict[str, Any] = {"n": n, "t": node["title"]}
            notes = (node.get("notes") or "").strip()
            if notes:
                entry["notes"] = notes
            children = node.get("children") or []
            if children:
                entry["c"] = _walk(children, category)
            out.append(entry)
        return out

    compact = _walk(hierarchy, "")
    return compact, token_to_uuid, token_to_category


def _format_team_members(
    users: list[dict[str, str]],
) -> tuple[str, dict[int, str]]:
    """Render team members with integer tokens, returning the lookup map.

    Output lines look like ``[3] Santiago Cuadra (santiago)`` — name plus
    the email prefix as a single first-name disambiguator. No verbose
    ``(aliases: ...)`` block: the prompt already says first-name matches
    must be unambiguous, so showing one short alias is enough and keeps
    the {{TEAM_MEMBERS}} placeholder lean.
    """
    if not users:
        return "No team members available", {}
    lines: list[str] = []
    token_to_uuid: dict[int, str] = {}
    for i, m in enumerate(users, start=1):
        token_to_uuid[i] = m["id"]
        name = m.get("name", "")
        email = m.get("email") or ""
        alias = email.split("@")[0] if email else ""
        if alias and alias.lower() != name.lower():
            lines.append(f"[{i}] {name} ({alias})")
        else:
            lines.append(f"[{i}] {name}")
    return "\n".join(lines), token_to_uuid


def _format_meeting_taxonomy(taxonomy: dict[str, Any]) -> str:
    """Render the operator-set tags as the ``MEETING TAXONOMY`` block.

    Returns the empty string when nothing is set so the caller can skip
    emitting the block entirely (no zero-signal noise).
    """
    lines: list[str] = []
    mwb = (taxonomy.get("macro_work_block") or "").strip()
    if mwb:
        lines.append(f"Macro Work Block: {mwb}")
    detail = taxonomy.get("detail") or []
    if isinstance(detail, list) and detail:
        lines.append(f"Detail: {', '.join(detail)}")
    elif isinstance(detail, str) and detail.strip():
        lines.append(f"Detail: {detail.strip()}")
    ext_org = (taxonomy.get("external_org") or "").strip()
    if ext_org:
        lines.append(f"External Org: {ext_org}")
    return "\n".join(lines)


class TaskClassifier:
    """Classifies extracted tasks against the tracker's structure via LLM."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str | None = None,
    ) -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def classify(
        self,
        tasks: list[dict[str, Any]],
        prompt_template: str,
        hierarchy: list[dict[str, Any]],
        team_members: list[dict[str, str]],
        meeting_title: str = "",
        meeting_date: str = "",
        enriched_attendees: str = "",
        notes_text: str = "",
        meeting_taxonomy: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Classify tasks and merge category/parent/assignee into each dict.

        The model is asked for the minimal shape ``{"i", "p"?, "a"?}``
        per task with integer tokens (see ``_build_hierarchy_token_map``
        and ``_format_team_members``). This call translates those tokens
        back to UUIDs and derives ``category`` by walking each chosen
        node up to its Tier-0 ancestor.

        Returns the original task list with these fields merged in:
        ``category``, ``parent_task_id``, ``assignee_id``,
        ``deal_page_id`` (always None — the deal-context branch was
        retired). ``external_assignees`` is preserved untouched from
        the extractor.
        """
        if not tasks:
            return tasks

        compact_tree, hierarchy_token_to_uuid, hierarchy_token_to_category = (
            _build_hierarchy_token_map(hierarchy)
        )
        members_text, member_token_to_uuid = _format_team_members(team_members)
        hierarchy_text = json.dumps(compact_tree, ensure_ascii=False)

        system_prompt = _substitute_placeholders(
            prompt_template,
            HIERARCHY=hierarchy_text,
            TEAM_MEMBERS=members_text,
        )

        task_input = [
            {
                "index": i,
                "title": t.get("title", ""),
                "internal_assignees": t.get("internal_assignees", []),
                "external_assignees": t.get("external_assignees", []),
                "priority": t.get("priority", "Medium"),
                "due_date": t.get("due_date"),
            }
            for i, t in enumerate(tasks)
        ]

        user_sections: list[str] = []
        if meeting_title or meeting_date:
            meeting_block = "=== MEETING CONTEXT ==="
            if meeting_title:
                meeting_block += f"\nTitle: {meeting_title}"
            if meeting_date:
                meeting_block += f"\nDate: {meeting_date}"
            user_sections.append(meeting_block)
        if meeting_taxonomy:
            tax_text = _format_meeting_taxonomy(meeting_taxonomy)
            if tax_text:
                user_sections.append(
                    "=== MEETING TAXONOMY (operator-set tags on the meeting page) ===\n"
                    + tax_text
                )
        if enriched_attendees:
            user_sections.append(
                "=== ATTENDEES (with org-chart role annotations where available) ===\n"
                + enriched_attendees
            )
        if notes_text:
            user_sections.append(
                "=== HUMAN NOTES (note-taker's ground truth — highest priority) ===\n"
                + notes_text
            )
        user_sections.append(
            "=== TASKS TO CLASSIFY ===\n"
            + json.dumps(task_input, indent=2, ensure_ascii=False)
        )
        user_prompt = "\n\n".join(user_sections)

        logger.debug(
            "Classifying %d tasks with %s (hierarchy=%d nodes, members=%d)",
            len(tasks), self._model,
            len(hierarchy_token_to_uuid), len(member_token_to_uuid),
        )

        # Token→UUID mapping needs almost no deliberation, so default to
        # ``minimal`` reasoning — without this, gpt-5-mini spends ~1,500-2,000
        # hidden reasoning tokens per call (billed as output), dwarfing the
        # tiny visible JSON. Override per-run with NZYME_CLASSIFY_REASONING_EFFORT
        # (minimal|low|medium|high, or "default" to let the API decide) when
        # comparing classification quality.
        extra: dict[str, Any] = {}
        effort = os.environ.get("NZYME_CLASSIFY_REASONING_EFFORT", "minimal").strip().lower()
        if _supports_reasoning_effort(self._model) and effort != "default":
            extra["reasoning_effort"] = effort

        response = self._client.beta.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format=ClassificationOutput,
            **extra,
        )

        usage_info = log_usage(response, self._model, stage="Classification", logger=logger)

        message = response.choices[0].message
        dump_call(
            stage="Classification",
            model=self._model,
            system=system_prompt,
            user=user_prompt,
            raw_response=getattr(message, "content", None) or "",
            usage=usage_info,
        )
        refusal = getattr(message, "refusal", None)
        if refusal:
            logger.warning("Classifier refused to comply: %s", refusal)
            classified: list[Any] = []
        else:
            parsed: ClassificationOutput | None = getattr(message, "parsed", None)
            classified = list(parsed.tasks) if parsed is not None else []

        for entry in classified:
            idx = entry.i
            if not isinstance(idx, int) or idx < 0 or idx >= len(tasks):
                logger.warning("Invalid task index %s in classifier response", idx)
                continue

            task = tasks[idx]

            # Parent token → UUID, with category derived from Tier-0 ancestor.
            p = entry.p
            if isinstance(p, int) and p in hierarchy_token_to_uuid:
                task["parent_task_id"] = hierarchy_token_to_uuid[p]
                task["category"] = hierarchy_token_to_category.get(p) or "Other"
            else:
                if p is not None:
                    logger.warning(
                        "Invalid parent token %s for task '%s', dropping",
                        p, task.get("title", "?")[:60],
                    )
                task["parent_task_id"] = None
                task["category"] = "Other"

            # Assignee tokens → UUIDs.
            assignee_ids: list[str] = []
            for token in entry.a or []:
                if isinstance(token, int) and token in member_token_to_uuid:
                    assignee_ids.append(member_token_to_uuid[token])
                elif token:
                    logger.warning(
                        "Invalid assignee token %r for task '%s', dropping",
                        token, task.get("title", "?")[:60],
                    )

            # External-only safety net: if the extractor flagged external
            # assignees and there are no internal ones, drop any IDs the
            # classifier might have guessed via first-name aliasing.
            ext = task.get("external_assignees") or []
            internal = task.get("internal_assignees") or []
            if ext and not internal and assignee_ids:
                logger.info(
                    "Dropping %d assignee_id(s) for task '%s' — only external "
                    "assignees (%s) were named; creator fallback will apply",
                    len(assignee_ids), task.get("title", "?")[:60],
                    ", ".join(ext),
                )
                assignee_ids = []

            task["assignee_id"] = assignee_ids
            # Deal context branch is retired; the field stays in the dict
            # so the writer's `task.get("deal_page_id")` check remains a
            # no-op without raising KeyError on downstream consumers.
            task["deal_page_id"] = None

        # Backstop: any task the model didn't classify still needs defaults.
        for task in tasks:
            if "category" not in task:
                logger.warning(
                    "Task '%s' was not classified — defaulting to 'Other'",
                    task.get("title", "?")[:60],
                )
                task["category"] = "Other"
                task.setdefault("parent_task_id", None)
                task.setdefault("assignee_id", [])
                task.setdefault("deal_page_id", None)

        logger.debug("Classified %d tasks", len(tasks))
        return tasks
