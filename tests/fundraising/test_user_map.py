"""Tests for the Kibo user map."""
from __future__ import annotations

import json
from pathlib import Path

from src.fundraising.user_map import KiboUserMap


def _write_map(tmp_path: Path, users: list[dict]) -> Path:
    p = tmp_path / "map.json"
    p.write_text(json.dumps({"users": users}), encoding="utf-8")
    return p


def test_load_empty_when_missing(tmp_path):
    m = KiboUserMap.load(tmp_path / "nope.json")
    assert len(m) == 0
    assert m.affinity_id_for_notion_user("anything") is None


def test_lookup_by_notion_id_and_email(tmp_path):
    p = _write_map(tmp_path, [
        {
            "notion_user_id": "uid-santiago",
            "email": "santiago@kiboventures.com",
            "affinity_user_id": 41826372,
            "display_name": "Santiago",
        },
        {
            "notion_user_id": "uid-other",
            "email": "other@kiboventures.com",
            # missing affinity_user_id — should not break lookups
            "display_name": "Other",
        },
    ])
    m = KiboUserMap.load(p)
    assert m.affinity_id_for_notion_user("uid-santiago") == 41826372
    assert m.affinity_id_for_email("SANTIAGO@kiboventures.com") == 41826372
    assert m.affinity_id_for_notion_user("uid-other") is None
    assert m.affinity_id_for_notion_user("uid-missing") is None
