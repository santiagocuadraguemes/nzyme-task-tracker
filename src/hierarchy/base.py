"""Uniform shape for Hierarchy DB → downstream-state sub-syncs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from src.config import SyncConfig
    from src.notion_client_wrapper import NotionClientWrapper


@dataclass
class SyncReport:
    """Aggregate per-sub-sync result for the orchestrator to log.

    Sub-syncs use whatever subset of counters fit their model; unused
    counters stay 0. ``edited`` / ``deleted`` / ``reactivated`` exist for
    the canonical mirror's diff semantics; ``renamed`` / ``archived`` exist
    for the Notion-side propagation semantics.
    """

    name: str
    created: int = 0
    renamed: int = 0
    archived: int = 0
    edited: int = 0
    deleted: int = 0
    reactivated: int = 0
    parent_fixed: int = 0
    errors: int = 0
    details: list[str] = field(default_factory=list)


SubSync = Callable[["NotionClientWrapper", "SyncConfig"], SyncReport]


__all__ = ["SubSync", "SyncReport"]
