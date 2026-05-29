"""Tests for contributor-note labeling + Internal attendees population in the
Meeting Mirrors writer."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.topic_mirror.outcome import MirrorAction
from src.topic_mirror.route_registry import ACTION_MIRROR_TO_DB, Route
from src.topic_mirror.writer import (
    _build_contributor_heading,
    _internal_attendee_ids,
    clone_or_merge,
    find_existing_mirror,
)

_ISO_TITLE = "Ext. Poseidon | Deep dive session - modelo 2026-05-29T14:00:00.000+02:00"
_CLEAN_TITLE = "Ext. Poseidon | Deep dive session - modelo"


def _route() -> Route:
    return Route(
        match_property="External Org",
        match_value="Poseidon",
        target_db_id="target-db",
        label="poseidon",
        action=ACTION_MIRROR_TO_DB,
    )


def _meeting_notes_block(attendees: list[str], notes_block_id: str = "notes-1") -> dict:
    return {
        "type": "meeting_notes",
        "meeting_notes": {
            "children": {"notes_block_id": notes_block_id},
            "calendar_event": {"attendees": attendees},
        },
    }


def _notes_heading(block_id: str = "notes-heading-1") -> dict:
    return {
        "id": block_id,
        "type": "heading_3",
        "heading_3": {"rich_text": [{"plain_text": "Notes"}]},
    }


class TestContributorHeading:
    def test_blue_background_possessive_lowercase(self):
        block = _build_contributor_heading("Guillermo Puebla")
        assert block["type"] == "heading_3"
        assert block["heading_3"]["color"] == "blue_background"
        assert (
            block["heading_3"]["rich_text"][0]["text"]["content"]
            == "Guillermo Puebla's notes"
        )


class TestInternalAttendeeIds:
    def test_keeps_only_member_attendees_order_preserved(self):
        client = MagicMock()
        client.get_block_children.return_value = [
            _meeting_notes_block(["u-guille", "u-ext", "u-sakhee", "u-guille"]),
        ]
        # u-ext is not a workspace member (e.g. a guest / stray id).
        client.list_users.return_value = [
            {"id": "u-guille", "type": "person"},
            {"id": "u-sakhee", "type": "person"},
            {"id": "u-bot", "type": "bot"},
        ]

        ids = _internal_attendee_ids(client, "source-page")

        assert ids == ["u-guille", "u-sakhee"]

    def test_no_meeting_notes_block_returns_empty(self):
        client = MagicMock()
        client.get_block_children.return_value = [{"type": "paragraph"}]

        assert _internal_attendee_ids(client, "source-page") == []
        client.list_users.assert_not_called()

    def test_no_attendees_skips_user_lookup(self):
        client = MagicMock()
        client.get_block_children.return_value = [_meeting_notes_block([])]

        assert _internal_attendee_ids(client, "source-page") == []
        client.list_users.assert_not_called()


class TestCloneWritesInternalAttendees:
    def test_internal_attendees_set_on_clone(self):
        client = MagicMock()
        client.query_database.return_value = {"results": []}  # no existing mirror
        client.retrieve_data_source.return_value = {
            "properties": {
                "Meeting": {"type": "title"},
                "Owner": {"type": "people"},
                "Internal attendees": {"type": "people"},
            },
        }
        client._client.pages.create.return_value = {"id": "mirror-1"}
        client._call_with_retry.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
        client.list_users.return_value = [
            {"id": "u-guille", "type": "person"},
            {"id": "u-sakhee", "type": "person"},
        ]
        # Source blocks (attendees) + mirror notes container with a Notes heading.
        client.get_block_children.return_value = [
            _meeting_notes_block(["u-guille", "u-sakhee"]),
            _notes_heading(),
        ]

        source_page = {"id": "source-1", "url": "https://n", "properties": {}}

        action = clone_or_merge(
            client=client,
            route=_route(),
            source_page=source_page,
            source_title="Ext. Poseidon",
            source_date="2026-05-29",
            owner_user_id="u-guille",
            owner_name="Guillermo",
        )

        assert action == MirrorAction.CLONED
        props_sent = client._client.pages.create.call_args.kwargs["properties"]
        assert props_sent["Internal attendees"] == {
            "people": [{"id": "u-guille"}, {"id": "u-sakhee"}],
        }


class TestTitleDatetimeStripped:
    def test_clone_meeting_title_strips_iso_datetime(self):
        client = MagicMock()
        client.query_database.return_value = {"results": []}
        client.retrieve_data_source.return_value = {
            "properties": {"Meeting": {"type": "title"}},
        }
        client._client.pages.create.return_value = {"id": "mirror-1"}
        client._call_with_retry.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
        client.list_users.return_value = []
        client.get_block_children.return_value = [
            _meeting_notes_block([], "notes-1"),
            _notes_heading(),
        ]

        clone_or_merge(
            client=client,
            route=_route(),
            source_page={"id": "source-1", "url": "https://n", "properties": {}},
            source_title=_ISO_TITLE,
            source_date="2026-05-29",
            owner_user_id="",
            owner_name="Sakhee",
        )

        props = client._client.pages.create.call_args.kwargs["properties"]
        assert props["Meeting"]["title"][0]["text"]["content"] == _CLEAN_TITLE

    def test_find_existing_mirror_matches_across_datetime_suffix(self):
        # Mirror stored WITH the ISO suffix (pre-cleanup), source already clean.
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                {"properties": {"Meeting": {
                    "type": "title", "title": [{"plain_text": _ISO_TITLE}],
                }}},
            ],
        }
        match = find_existing_mirror(client, "db", _CLEAN_TITLE, "2026-05-29")
        assert match is not None


class TestMergeAppendsLabelAndUnionsAttendees:
    def _client_with_existing_mirror(self) -> MagicMock:
        client = MagicMock()
        client._call_with_retry.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
        # Existing mirror: Guillermo owns it; Internal attendees already has him.
        existing = {
            "id": "mirror-1",
            "properties": {
                "Meeting": {"type": "title", "title": [{"plain_text": "Ext. Poseidon"}]},
                "Owner": {"type": "people", "people": [{"id": "u-guille"}]},
                "Internal attendees": {
                    "type": "people", "people": [{"id": "u-guille"}],
                },
            },
        }
        client.query_database.return_value = {"results": [existing]}
        return client

    def test_second_contributor_notes_labeled_and_attendees_unioned(self):
        client = self._client_with_existing_mirror()
        client.list_users.return_value = [
            {"id": "u-guille", "type": "person"},
            {"id": "u-sakhee", "type": "person"},
        ]

        # get_block_children is hit for: (1) source attendees, (2) source notes
        # extraction, (3) mirror notes_block_id lookup, (4) mirror notes
        # children. Route by block id.
        def block_children(block_id, *a, **kw):
            if block_id == "source-2":
                return [_meeting_notes_block(["u-guille", "u-sakhee"], "src-notes-2")]
            if block_id == "src-notes-2":
                # Full notes container: Action Items heading + the Notes
                # heading + a paragraph. The whole thing must be copied.
                return [
                    {
                        "type": "heading_2",
                        "heading_2": {
                            "rich_text": [{"type": "text", "plain_text": "Action Items", "text": {"content": "Action Items"}}],
                            "color": "gray_background",
                        },
                    },
                    _notes_heading("src-notes-heading"),
                    {
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "plain_text": "YYY", "text": {"content": "YYY"}}]},
                    },
                ]
            if block_id == "mirror-1":
                return [_meeting_notes_block([], "mirror-notes-1")]
            if block_id == "mirror-notes-1":
                return [_notes_heading("mirror-notes-heading")]
            return []

        client.get_block_children.side_effect = block_children

        action = clone_or_merge(
            client=client,
            route=_route(),
            source_page={"id": "source-2", "properties": {}},
            source_title="Ext. Poseidon",
            source_date="2026-05-29",
            owner_user_id="u-sakhee",
            owner_name="Sakhee Sukhwani-Joisher",
        )

        assert action == MirrorAction.MERGED

        # Internal attendees unioned: Guillermo (existing) + Sakhee (new).
        attendee_update = next(
            c for c in client.update_page.call_args_list
            if "Internal attendees" in c.kwargs.get("properties", {})
        )
        people = attendee_update.kwargs["properties"]["Internal attendees"]["people"]
        assert [p["id"] for p in people] == ["u-guille", "u-sakhee"]

        # Sakhee's notes appended with a blue "<Name>'s notes" H3 at the end
        # (no `after` → default end position) of the mirror's notes container.
        notes_append = next(
            c for c in client.append_block_children.call_args_list
            if c.kwargs.get("block_id") == "mirror-notes-1"
            and c.kwargs.get("after") is None
        )
        children = notes_append.kwargs["children"]
        heading = children[0]
        assert heading["heading_3"]["color"] == "blue_background"
        assert (
            heading["heading_3"]["rich_text"][0]["text"]["content"]
            == "Sakhee Sukhwani-Joisher's notes"
        )
        # Full literal copy: the source's own Action Items + Notes headings and
        # the paragraph are all reproduced below the label (nothing sliced).
        copied_types = [c.get("type") for c in children[1:]]
        assert copied_types == ["heading_2", "heading_3", "paragraph"]
        # Block-level color preserved on the copied Action Items heading.
        assert children[1]["heading_2"]["color"] == "gray_background"

        # Owner unioned.
        owner_update = next(
            c for c in client.update_page.call_args_list
            if "Owner" in c.kwargs.get("properties", {})
        )
        owner_ids = [p["id"] for p in owner_update.kwargs["properties"]["Owner"]["people"]]
        assert owner_ids == ["u-guille", "u-sakhee"]

    def test_owner_already_present_still_unions_internal_attendees(self):
        client = self._client_with_existing_mirror()
        client.list_users.return_value = [
            {"id": "u-guille", "type": "person"},
            {"id": "u-sakhee", "type": "person"},
        ]

        def block_children(block_id, *a, **kw):
            if block_id == "source-1":
                # Guillermo re-processed; his recording now also shows Sakhee.
                return [_meeting_notes_block(["u-guille", "u-sakhee"], "src-notes-1")]
            return []

        client.get_block_children.side_effect = block_children

        action = clone_or_merge(
            client=client,
            route=_route(),
            source_page={"id": "source-1", "properties": {}},
            source_title="Ext. Poseidon",
            source_date="2026-05-29",
            owner_user_id="u-guille",  # already in Owner
            owner_name="Guillermo",
        )

        assert action == MirrorAction.NOOP
        # Even though the merge no-ops, the new internal attendee is unioned.
        attendee_update = next(
            c for c in client.update_page.call_args_list
            if "Internal attendees" in c.kwargs.get("properties", {})
        )
        people = attendee_update.kwargs["properties"]["Internal attendees"]["people"]
        assert [p["id"] for p in people] == ["u-guille", "u-sakhee"]

    def test_missing_internal_attendees_field_is_graceful(self):
        client = MagicMock()
        client._call_with_retry.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
        client.list_users.return_value = [{"id": "u-sakhee", "type": "person"}]
        # Existing mirror WITHOUT an Internal attendees property (older DB).
        existing = {
            "id": "mirror-1",
            "properties": {
                "Meeting": {"type": "title", "title": [{"plain_text": "Ext. Poseidon"}]},
                "Owner": {"type": "people", "people": [{"id": "u-guille"}]},
            },
        }
        client.query_database.return_value = {"results": [existing]}

        def block_children(block_id, *a, **kw):
            if block_id == "source-2":
                return [_meeting_notes_block(["u-sakhee"], "src-notes-2")]
            if block_id == "src-notes-2":
                return []  # no notes content
            return []

        client.get_block_children.side_effect = block_children

        action = clone_or_merge(
            client=client,
            route=_route(),
            source_page={"id": "source-2", "properties": {}},
            source_title="Ext. Poseidon",
            source_date="2026-05-29",
            owner_user_id="u-sakhee",
            owner_name="Sakhee",
        )

        # No notes → NOOP, and no Internal attendees PATCH (field absent).
        assert action == MirrorAction.NOOP
        internal_updates = [
            c for c in client.update_page.call_args_list
            if "Internal attendees" in c.kwargs.get("properties", {})
        ]
        assert internal_updates == []
