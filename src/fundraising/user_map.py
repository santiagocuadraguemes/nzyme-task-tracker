"""Static Kibo team member map used by the fundraising → Affinity branch.

Resolves Notion user ids / emails to Affinity user ids for the
``Nzyme Next Step OWNER`` field. Populate
``src/fundraising/data/kibo_user_map.json`` with one entry per team member:

    {
      "users": [
        {
          "notion_user_id": "aaaa-bbbb-cccc",
          "email": "santiago@kiboventures.com",
          "affinity_user_id": 41826372,
          "display_name": "Santiago Cuadra"
        },
        ...
      ]
    }

Entries without ``affinity_user_id`` are ignored for owner resolution.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAP_PATH = (
    Path(__file__).resolve().parent / "data" / "kibo_user_map.json"
)


class KiboUserMap:
    """In-memory lookup table keyed by notion_user_id and email."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._by_notion: dict[str, dict[str, Any]] = {}
        self._by_email: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if nid := entry.get("notion_user_id"):
                self._by_notion[nid] = entry
            if email := entry.get("email"):
                self._by_email[email.lower()] = entry

    @classmethod
    def load(cls, path: str | Path | None = None) -> KiboUserMap:
        target = Path(path) if path else DEFAULT_MAP_PATH
        try:
            with target.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except FileNotFoundError:
            logger.warning(
                "Kibo user map not found at %s — owner resolution will fail",
                target,
            )
            return cls([])
        users = payload.get("users") or []
        if not isinstance(users, list):
            raise ValueError(
                f"Kibo user map at {target} must contain a top-level 'users' list"
            )
        return cls(users)

    def affinity_id_for_notion_user(self, notion_user_id: str) -> int | None:
        entry = self._by_notion.get(notion_user_id)
        if not entry:
            return None
        return entry.get("affinity_user_id")

    def affinity_id_for_email(self, email: str) -> int | None:
        if not email:
            return None
        entry = self._by_email.get(email.lower())
        if not entry:
            return None
        return entry.get("affinity_user_id")

    def __len__(self) -> int:
        return max(len(self._by_notion), len(self._by_email))


__all__ = ["KiboUserMap", "DEFAULT_MAP_PATH"]
