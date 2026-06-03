"""Tests for the Meeting Mirrors confidentiality resolver."""
from __future__ import annotations

import pytest

from src.topic_mirror.confidentiality import mirror_allowed, read_confidential


class TestReadConfidential:
    def test_reads_select_name(self):
        props = {"Confidential": {"type": "select", "select": {"name": "Confidential"}}}
        assert read_confidential(props) == "Confidential"

    def test_blank_when_select_unset(self):
        props = {"Confidential": {"type": "select", "select": None}}
        assert read_confidential(props) == ""

    def test_blank_when_property_absent(self):
        # A member DB without the column → property simply missing.
        assert read_confidential({}) == ""

    def test_blank_when_wrong_type(self):
        props = {"Confidential": {"type": "checkbox", "checkbox": True}}
        assert read_confidential(props) == ""

    def test_strips_whitespace(self):
        props = {"Confidential": {"type": "select", "select": {"name": "  Shareable "}}}
        assert read_confidential(props) == "Shareable"


class TestMirrorAllowedTruthTable:
    # (confidential, owner_default) -> allowed
    @pytest.mark.parametrize(
        ("confidential", "owner_default", "allowed"),
        [
            # Shared (or unset) default
            ("", "Shared", True),
            ("Shareable", "Shared", True),
            ("Confidential", "Shared", False),
            ("", "", True),               # unset default behaves as Shared
            ("Confidential", "", False),
            # Private default
            ("", "Private", False),
            ("Shareable", "Private", True),   # explicit override wins
            ("Confidential", "Private", False),
        ],
    )
    def test_truth_table(self, confidential, owner_default, allowed):
        assert mirror_allowed(confidential, owner_default) is allowed

    def test_case_insensitive(self):
        assert mirror_allowed("confidential", "Shared") is False
        assert mirror_allowed("SHAREABLE", "private") is True
        assert mirror_allowed("", "PRIVATE") is False
