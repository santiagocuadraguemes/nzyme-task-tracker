"""Confidentiality gate for the Meeting Mirrors branch.

A meeting can be kept out of the shared topic DBs even when it matches a
mirror rule. Two inputs decide it:

  - the meeting's ``Confidential`` select (per-member Meeting Notes DB) —
    ``Confidential`` (force-private) / ``Shareable`` (force-share) / blank;
  - the owner's ``Default Mirror Visibility`` (Org Chart row) —
    ``Private`` / ``Shared``, used only when the meeting value is blank.

An explicit meeting value always wins. Blank falls back to the owner
default, which itself defaults to ``Shared`` (mirror as before) so the
feature is back-compat: a member DB without the ``Confidential`` column or
an Org Chart row without ``Default Mirror Visibility`` behaves exactly as
today. The resolver is pure so the truth table is unit-testable.
"""
from __future__ import annotations

from typing import Any

# Meeting-level select option names (on each per-member Meeting Notes DB).
CONFIDENTIAL = "Confidential"
SHAREABLE = "Shareable"
CONFIDENTIAL_PROPERTY = "Confidential"

# Owner-level default (Org Chart `Default Mirror Visibility` select).
VISIBILITY_PRIVATE = "Private"
VISIBILITY_SHARED = "Shared"
DEFAULT_VISIBILITY = VISIBILITY_SHARED  # back-compat: mirror as today


def read_confidential(page_properties: dict[str, Any]) -> str:
    """Return the meeting's ``Confidential`` select name ('' if absent/blank)."""
    prop = page_properties.get(CONFIDENTIAL_PROPERTY, {})
    if isinstance(prop, dict) and prop.get("type") == "select":
        return ((prop.get("select") or {}).get("name") or "").strip()
    return ""


def mirror_allowed(confidential: str, owner_default: str) -> bool:
    """Decide whether a matched meeting may be mirrored.

    ``Confidential`` → never; ``Shareable`` → always; blank → mirror unless
    the owner's default is ``Private``. Comparisons are case-insensitive.
    """
    c = (confidential or "").strip().lower()
    if c == CONFIDENTIAL.lower():
        return False
    if c == SHAREABLE.lower():
        return True
    return (owner_default or "").strip().lower() != VISIBILITY_PRIVATE.lower()


__all__ = [
    "CONFIDENTIAL",
    "CONFIDENTIAL_PROPERTY",
    "DEFAULT_VISIBILITY",
    "SHAREABLE",
    "VISIBILITY_PRIVATE",
    "VISIBILITY_SHARED",
    "mirror_allowed",
    "read_confidential",
]
