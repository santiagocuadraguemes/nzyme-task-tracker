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

    Lean shape: ``a`` (derivable from ia+ea), ``c`` (derivable from ct),
    and ``sr`` (diagnostic-only) are intentionally absent — see
    output-token-reduction-plan.md (A1/A2/A3). They are reconstructed in
    ``TaskExtractor._unpack_merged_response`` so downstream code sees the
    same dict shape it always has.
    """

    t: str = Field(description="Title: clear, actionable description (one sentence)")
    ia: list[str] = Field(
        default_factory=list,
        description="Internal assignees: team members responsible for the task (Org Chart names)",
    )
    ea: list[str] = Field(
        default_factory=list,
        description="External assignees: external people responsible (portfolio staff, advisers, etc.)",
    )
    ct: str = Field(
        description="Commitment type. One of: hard | conditional | soft | group",
    )
    p: str = Field(description="Priority. One of: High | Medium | Low")
    dd: Optional[str] = Field(
        default=None,
        description="Due date: ISO date (YYYY-MM-DD). OMIT this key entirely if no deadline.",
    )


class MergedExtractionOutput(BaseModel):
    """Top-level JSON the merged correction+extraction call returns.

    Scratch fields (``domain_corrections``, ``speaker_resolutions``) were
    dropped as part of plan A4 — domain correction and speaker resolution
    still happen mentally, just no longer reported.
    """

    tasks: list[ExtractedTask] = Field(
        default_factory=list, description="Extracted action items",
    )


# ---------------------------------------------------------------------------
# Measurement-only candidate variants.
#
# These are used by scripts/compare_candidate.py to test output-token
# reductions under a real Gemini call. Production code paths always use
# ``MergedExtractionOutput`` above — the variants below are wired in only
# when ``set_response_schema_override`` is called explicitly from a script.
# ---------------------------------------------------------------------------


class ExtractedTaskNoSR(BaseModel):
    """Same as ExtractedTask but without the diagnostic ``sr`` field."""

    t: str = Field(description="Title: clear, actionable description (one sentence)")
    a: str = Field(description="Assignee display string, e.g. 'Santiago, Jacob'")
    ia: list[str] = Field(default_factory=list,
        description="Internal assignees (Org Chart names)")
    ea: list[str] = Field(default_factory=list,
        description="External assignees (non-Kibo)")
    ct: str = Field(description="Commitment type. One of: hard | conditional | soft | group")
    p: str = Field(description="Priority. One of: High | Medium | Low")
    dd: Optional[str] = Field(default=None,
        description="Due date: ISO date (YYYY-MM-DD) or null")
    c: str = Field(description="Confidence. One of: high | medium | low")


class ExtractedTaskNoSRNoA(BaseModel):
    """Drops both ``sr`` (diagnostic) and ``a`` (derivable from ia+ea)."""

    t: str = Field(description="Title: clear, actionable description (one sentence)")
    ia: list[str] = Field(default_factory=list,
        description="Internal assignees (Org Chart names)")
    ea: list[str] = Field(default_factory=list,
        description="External assignees (non-Kibo)")
    ct: str = Field(description="Commitment type. One of: hard | conditional | soft | group")
    p: str = Field(description="Priority. One of: High | Medium | Low")
    dd: Optional[str] = Field(default=None,
        description="Due date: ISO date (YYYY-MM-DD) or null")
    c: str = Field(description="Confidence. One of: high | medium | low")


class MergedExtractionOutputNoSR(BaseModel):
    """Variant: drop the per-task ``sr`` diagnostic field. Scratch fields kept."""

    domain_corrections: list[str] = Field(default_factory=list)
    speaker_resolutions: list[SpeakerResolution] = Field(default_factory=list)
    tasks: list[ExtractedTaskNoSR] = Field(default_factory=list)


class MergedExtractionOutputNoScratch(BaseModel):
    """Variant: drop the per-call scratch fields. Tasks kept identical."""

    tasks: list[ExtractedTask] = Field(default_factory=list)


class MergedExtractionOutputCombined(BaseModel):
    """Variant: drop ``sr`` + ``a`` + scratch fields. The lean prod candidate."""

    tasks: list[ExtractedTaskNoSRNoA] = Field(default_factory=list)


# Lookup used by scripts/compare_candidate.py to pick a variant by flag.
CANDIDATE_SCHEMAS = {
    "baseline":   MergedExtractionOutput,
    "no-sr":      MergedExtractionOutputNoSR,
    "no-scratch": MergedExtractionOutputNoScratch,
    "combined":   MergedExtractionOutputCombined,
}
