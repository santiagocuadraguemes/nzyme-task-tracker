"""Structured outcomes of a Meeting Mirrors run for a single page.

There is no Notion property tracking status — every run emits one
``topic mirror outcome:`` CloudWatch line, which is the only audit trail.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MirrorStatus(str, Enum):
    """Outcome of running the mirror branch for a single page.

    POSTED — at least one route ran successfully and no route failed.
    NO_MATCH — the page's tags matched zero active routes.
    DISABLED — feature flag off or routes DB unset.
    SKIPPED_CONFIDENTIAL — the page matched ≥1 route but the confidentiality
        gate held it back (meeting marked Confidential, or blank with the
        owner's default = Private).
    PARTIAL_FAILURE — some routes succeeded, others raised.
    FAILED — every matched route raised (or registry load failed).
    """

    POSTED = "Posted"
    NO_MATCH = "Skipped: no matching route"
    DISABLED = "Skipped: feature disabled"
    SKIPPED_CONFIDENTIAL = "Skipped: confidential"
    PARTIAL_FAILURE = "Partial: some routes failed"
    FAILED = "Failed: all routes failed"


class MirrorAction(str, Enum):
    """What a single route did for the current page."""

    CLONED = "cloned"          # first contributor — pages.create with template_id
    MERGED = "merged"          # subsequent contributor — appended notes to existing mirror
    NOOP = "noop"              # contributor already in Contributors → no append


@dataclass(frozen=True)
class MirrorOutcome:
    status: MirrorStatus
    detail: str = ""
