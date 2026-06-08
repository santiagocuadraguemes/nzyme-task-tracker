"""Tests for the Org Chart + Meeting Rules → Supabase canonical mirrors.

All Supabase I/O is patched at ``src.config_mirror_sync._http``; the Notion
client is a MagicMock. No network.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.config_mirror_sync import sync_meeting_rules, sync_org_chart

ROW_HEX_1 = "1" * 32
ROW_HEX_2 = "2" * 32
ROW_UUID_1 = "11111111-1111-1111-1111-111111111111"
ROW_UUID_2 = "22222222-2222-2222-2222-222222222222"
TARGET_HEX = "f" * 32
TARGET_UUID = "ffffffff-ffff-ffff-ffff-ffffffffffff"


def _client(results: list[dict]) -> MagicMock:
    client = MagicMock()
    client.query_database.return_value = {"results": results}
    return client


def _org_row(
    page_id: str = ROW_HEX_1,
    name: str = "Santiago Cuadra",
    *,
    url: str | None = "https://www.notion.so/kibo/Santiago-Meeting-Notes-" + "b" * 32,
    active: bool = True,
    auto_extract: bool = False,
    visibility: str | None = None,
    seniority: str | None = None,
    email: str | None = "Santiago@Kibo.vc",
) -> dict:
    props: dict = {
        "Name": {"type": "title", "title": [{"plain_text": name}]},
        "Active": {"type": "checkbox", "checkbox": active},
        "Auto-extract Tasks": {"type": "checkbox", "checkbox": auto_extract},
    }
    if url is not None:
        props["Meeting Notes DB"] = {"type": "url", "url": url}
    if email is not None:
        props["Email"] = {"type": "email", "email": email}
    if visibility is not None:
        props["Default Mirror Visibility"] = {
            "type": "select", "select": {"name": visibility},
        }
    if seniority is not None:
        props["Seniority"] = {"type": "select", "select": {"name": seniority}}
    return {"id": page_id, "properties": props}


def _rule_row(
    page_id: str = ROW_HEX_1,
    *,
    match_property: str = "Detail",
    match_value: str = "AI & Tech",
    action: str | None = "Mirror to DB",
    target_url: str | None = "https://www.notion.so/kibo/Mirror-" + TARGET_HEX,
    active: bool = True,
    title: str = "",
) -> dict:
    props: dict = {
        "Route": {"type": "title", "title": [{"plain_text": title}]},
        "Match Property": {
            "type": "select", "select": {"name": match_property},
        },
        "Match Value": {
            "type": "rich_text", "rich_text": [{"plain_text": match_value}],
        },
        "Active": {"type": "checkbox", "checkbox": active},
    }
    if action is not None:
        props["Action"] = {"type": "select", "select": {"name": action}}
    if target_url is not None:
        props["Target DB"] = {"type": "url", "url": target_url}
    return {"id": page_id, "properties": props}


# ---------------------------------------------------------------------------
# sync_org_chart
# ---------------------------------------------------------------------------


@patch("src.config_mirror_sync._http")
def test_org_chart_row_mapping(mock_http):
    mock_http.return_value = []  # tombstone GET finds nothing live
    n = sync_org_chart(_client([_org_row(
        visibility="Private", seniority="Partner", auto_extract=True,
    )]), "o" * 32)
    assert n == 1

    # First _http call is the upsert POST.
    method, path = mock_http.call_args_list[0].args[:2]
    assert method == "POST"
    assert "org_chart_rows" in path and "on_conflict=notion_page_id" in path
    (row,) = mock_http.call_args_list[0].kwargs["body"]
    assert row["notion_page_id"] == ROW_UUID_1
    assert row["name"] == "Santiago Cuadra"
    assert row["email"] == "santiago@kibo.vc"          # lowercased
    assert row["meeting_notes_db_id"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert row["active"] is True
    assert row["auto_extract_tasks"] is True
    assert row["default_mirror_visibility"] == "Private"
    assert row["seniority"] == "Partner"
    assert row["deleted_at"] is None                   # revive on reappearance


@patch("src.config_mirror_sync._http")
def test_org_chart_keeps_rows_without_db_url(mock_http):
    """Unlike discover_meeting_dbs, members without a Meeting Notes DB are
    mirrored too (meeting_notes_db_id stays NULL)."""
    mock_http.return_value = []
    n = sync_org_chart(_client([_org_row(url=None, email=None)]), "o" * 32)
    assert n == 1
    (row,) = mock_http.call_args_list[0].kwargs["body"]
    assert row["meeting_notes_db_id"] is None
    assert row["email"] is None


@patch("src.config_mirror_sync._http")
def test_org_chart_visibility_unset_stays_null(mock_http):
    # Raw mirror: NULL when unset — consumers apply the "Shared" default.
    mock_http.return_value = []
    sync_org_chart(_client([_org_row()]), "o" * 32)
    (row,) = mock_http.call_args_list[0].kwargs["body"]
    assert row["default_mirror_visibility"] is None


@patch("src.config_mirror_sync._http")
def test_org_chart_tombstones_vanished_rows(mock_http):
    def http_side_effect(method, path, body=None, prefer="return=minimal"):
        if method == "GET":
            return [
                {"notion_page_id": ROW_UUID_1},   # still in Notion
                {"notion_page_id": ROW_UUID_2},   # vanished
            ]
        return None

    mock_http.side_effect = http_side_effect
    sync_org_chart(_client([_org_row(ROW_HEX_1)]), "o" * 32)

    patches = [c for c in mock_http.call_args_list if c.args[0] == "PATCH"]
    assert len(patches) == 1
    path = patches[0].args[1]
    assert f"notion_page_id=in.({ROW_UUID_2})" in path
    assert "deleted_at=is.null" in path
    assert patches[0].kwargs["body"]["deleted_at"] is not None


@patch("src.config_mirror_sync._http")
def test_org_chart_unset_db_id_skips(mock_http):
    assert sync_org_chart(MagicMock(), None) == 0
    mock_http.assert_not_called()


# ---------------------------------------------------------------------------
# sync_meeting_rules
# ---------------------------------------------------------------------------


@patch("src.config_mirror_sync._http")
def test_rules_row_mapping(mock_http):
    mock_http.return_value = []
    n = sync_meeting_rules(_client([_rule_row(title="AI mirror")]), "r" * 32)
    assert n == 1
    (row,) = mock_http.call_args_list[0].kwargs["body"]
    assert row["notion_page_id"] == ROW_UUID_1
    assert row["label"] == "AI mirror"
    assert row["match_property"] == "Detail"
    assert row["match_value"] == "AI & Tech"
    assert row["action"] == "Mirror to DB"
    assert row["target_db_id"] == TARGET_UUID
    assert row["active"] is True


@patch("src.config_mirror_sync._http")
def test_rules_inactive_rows_mirrored_with_flag(mock_http):
    """load_routes filters Active; the mirror keeps inactive rules so
    consumers can tell "off" from "deleted"."""
    mock_http.return_value = []
    n = sync_meeting_rules(_client([_rule_row(active=False)]), "r" * 32)
    assert n == 1
    (row,) = mock_http.call_args_list[0].kwargs["body"]
    assert row["active"] is False


@patch("src.config_mirror_sync._http")
def test_rules_legacy_affinity_action_normalized(mock_http):
    mock_http.return_value = []
    sync_meeting_rules(_client([_rule_row(
        action="Fire Affinity LP Funnel", target_url=None,
        match_property="Macro Work Block",
        match_value="Investor Relations & Fundraising",
    )]), "r" * 32)
    (row,) = mock_http.call_args_list[0].kwargs["body"]
    assert row["action"] == "Fire Affinity LP Funnel (no transcript)"
    assert row["target_db_id"] is None


@patch("src.config_mirror_sync._http")
def test_rules_synthesized_label_when_title_empty(mock_http):
    mock_http.return_value = []
    sync_meeting_rules(_client([_rule_row(title="")]), "r" * 32)
    (row,) = mock_http.call_args_list[0].kwargs["body"]
    assert row["label"] == "Detail:AI & Tech"


@patch("src.config_mirror_sync._http")
def test_rules_invalid_rows_skipped(mock_http):
    mock_http.return_value = []
    n = sync_meeting_rules(_client([
        _rule_row(ROW_HEX_1, match_property="Nonsense"),       # bad property
        _rule_row(ROW_HEX_2, match_value=""),                  # empty value
        _rule_row("3" * 32, action="Mirror to DB", target_url=None),  # no target
        _rule_row("4" * 32),                                   # valid
    ]), "r" * 32)
    assert n == 1
    (row,) = mock_http.call_args_list[0].kwargs["body"]
    assert row["notion_page_id"] == "44444444-4444-4444-4444-444444444444"


@patch("src.config_mirror_sync._http")
def test_rules_unset_db_id_skips(mock_http):
    assert sync_meeting_rules(MagicMock(), None) == 0
    mock_http.assert_not_called()


# ---------------------------------------------------------------------------
# Focused extras (spec Task 4): tombstone-then-revive, legacy-action
# normalization with the raw legacy tag, unparseable Target DB skip.
# ---------------------------------------------------------------------------


@patch("src.config_mirror_sync._http")
def test_org_chart_tombstone_then_revive(mock_http):
    """A row that disappears from Notion is tombstoned; when it reappears on
    a later tick the upsert carries ``deleted_at: None``, reviving it."""
    # Tick 1: ROW_1 present, ROW_2 vanished → ROW_2 tombstoned.
    def http_tick1(method, path, body=None, prefer="return=minimal"):
        if method == "GET":
            return [
                {"notion_page_id": ROW_UUID_1},
                {"notion_page_id": ROW_UUID_2},
            ]
        return None

    mock_http.side_effect = http_tick1
    sync_org_chart(_client([_org_row(ROW_HEX_1)]), "o" * 32)
    patches = [c for c in mock_http.call_args_list if c.args[0] == "PATCH"]
    assert len(patches) == 1
    assert f"notion_page_id=in.({ROW_UUID_2})" in patches[0].args[1]
    assert patches[0].kwargs["body"]["deleted_at"] is not None

    # Tick 2: ROW_2 reappears in Notion. The upsert body must carry
    # deleted_at=None (the revive) and there must be NO new tombstone PATCH.
    mock_http.reset_mock()
    mock_http.side_effect = None
    mock_http.return_value = [
        {"notion_page_id": ROW_UUID_1},
        {"notion_page_id": ROW_UUID_2},  # row is live again post-revive-upsert
    ]
    sync_org_chart(_client([_org_row(ROW_HEX_1), _org_row(ROW_HEX_2)]), "o" * 32)

    post = mock_http.call_args_list[0]
    assert post.args[0] == "POST"
    revived = {r["notion_page_id"]: r for r in post.kwargs["body"]}
    assert set(revived) == {ROW_UUID_1, ROW_UUID_2}
    assert revived[ROW_UUID_2]["deleted_at"] is None  # revive on reappearance
    # Both Notion rows are present, so nothing is tombstoned this tick.
    assert [c for c in mock_http.call_args_list if c.args[0] == "PATCH"] == []


@patch("src.config_mirror_sync._http")
def test_rules_raw_legacy_affinity_tag_normalized(mock_http):
    """The bare pre-split tag ``Fire Affinity LP Funnel`` (no variant suffix)
    is normalized to the no-transcript variant before validation, so the row
    survives instead of being dropped as an unknown action."""
    mock_http.return_value = []
    n = sync_meeting_rules(_client([_rule_row(
        action="Fire Affinity LP Funnel",  # exact legacy tag
        target_url=None,
        match_property="External Org",
        match_value="Acme Capital",
    )]), "r" * 32)
    assert n == 1
    (row,) = mock_http.call_args_list[0].kwargs["body"]
    assert row["action"] == "Fire Affinity LP Funnel (no transcript)"
    assert row["target_db_id"] is None


@patch("src.config_mirror_sync._http")
def test_rules_mirror_to_db_unparseable_target_skipped(mock_http):
    """A Mirror-to-DB rule whose Target DB URL has no 32-hex id is skipped
    (distinct from the missing-URL case: the URL is present but unparseable)."""
    mock_http.return_value = []
    n = sync_meeting_rules(_client([
        _rule_row(ROW_HEX_1, target_url="https://www.notion.so/no-id-here"),
        _rule_row(ROW_HEX_2),  # valid control, proves the sync still ran
    ]), "r" * 32)
    assert n == 1
    (row,) = mock_http.call_args_list[0].kwargs["body"]
    assert row["notion_page_id"] == ROW_UUID_2
