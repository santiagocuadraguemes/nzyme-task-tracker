"""Shared output schemas for the transcript pipeline.

A single Pydantic model describes the merged extraction call's output so
the same shape feeds both the native Gemini SDK (`response_schema`) and
any future OpenAI structured-output path (`response_format`). Keeping
the schema portable here is what insulates the call site from a model
swap — the call wiring changes, the contract doesn't.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# NOTE: we deliberately do NOT set ``extra="forbid"`` here. Pydantic would
# emit ``additionalProperties: false`` in the generated JSON Schema, and
# Gemini's responseSchema parser (a strict OpenAPI 3 subset) rejects that
# field with 400 INVALID_ARGUMENT. Lacking that guard is harmless for our
# use — extra keys in the model's response would be silently dropped by
# downstream code anyway.


class SpeakerResolution(BaseModel):
    """One anonymous-label → name mapping reported by the merged call."""

    label: str = Field(description="The transcript's raw label, e.g. 'Speaker 3'")
    name: str = Field(description="The resolved person's name")
    evidence: str = Field(description="One short sentence justifying the resolution")


class ExtractedTask(BaseModel):
    """A single action item emitted by the merged extraction call.

    Field order mirrors what the prompt asks for so that LLMs that emit
    fields sequentially produce reasoning fields (e.g. speaker_reasoning)
    *after* the upstream decisions they justify.
    """

    title: str = Field(description="Clear, actionable description (one sentence)")
    assignee: str = Field(description="Display string, e.g. 'Santiago, Jacob'")
    internal_assignees: list[str] = Field(
        default_factory=list,
        description="Internal team members responsible for the task (Org Chart names)",
    )
    external_assignees: list[str] = Field(
        default_factory=list,
        description="External people responsible (portfolio staff, advisers, etc.)",
    )
    commitment_type: str = Field(
        description="One of: hard | conditional | soft | group",
    )
    priority: str = Field(description="One of: High | Medium | Low")
    due_date: Optional[str] = Field(
        default=None, description="ISO date (YYYY-MM-DD) if a deadline was mentioned, else null",
    )
    confidence: str = Field(description="One of: high | medium | low")
    speaker_reasoning: str = Field(
        description="One sentence explaining the assignment + any external classification"
    )


class MergedExtractionOutput(BaseModel):
    """Top-level JSON the merged correction+extraction call returns."""

    domain_corrections: list[str] = Field(
        default_factory=list,
        description="Compact 'from→to' strings; one per unique correction applied",
    )
    speaker_resolutions: list[SpeakerResolution] = Field(
        default_factory=list,
        description="Resolved anonymous speaker labels (empty if none)",
    )
    tasks: list[ExtractedTask] = Field(
        default_factory=list, description="Extracted action items",
    )
