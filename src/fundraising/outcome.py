"""Structured outcome of the fundraising → Affinity branch.

Returned by ``write_to_affinity`` so the pipeline can emit a single
grep-friendly ``fundraising outcome: status=...`` log line per run. There is
no Notion property for status — CloudWatch is the alerting channel.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FundraisingStatus(str, Enum):
    """Outcomes of a single fundraising → Affinity attempt."""

    POSTED = "Posted"
    SKIPPED_NO_EXTERNAL_ATTENDEES = "Skipped: no external attendees"
    SKIPPED_NO_LP_MATCH = "Skipped: no LP match"
    FAILED_API_ERROR = "Failed: API error"


@dataclass(frozen=True)
class FundraisingOutcome:
    status: FundraisingStatus
    detail: str = ""
    summary: str | None = None
    # Affinity opportunity ids the note was posted to (POSTED only) —
    # recorded into the Supabase claim row for audit/debug.
    opportunity_ids: list[int] | None = None
