"""Tests for schema-aware filtering + missing-option ensurance in the
Meeting Mirrors writer."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.topic_mirror.route_registry import ACTION_MIRROR_TO_DB, Route
from src.topic_mirror.writer import (
    _ensure_select_options_on_target,
    _filter_to_target_schema,
    clone_or_merge,
)


def _select_schema(name: str, options: list[dict]) -> dict:
    return {name: {"type": "select", "name": name, "select": {"options": options}}}


def _multi_select_schema(name: str, options: list[dict]) -> dict:
    return {name: {"type": "multi_select", "name": name, "multi_select": {"options": options}}}


class TestFilterToTargetSchema:
    def test_drops_property_absent_from_target(self):
        properties = {
            "Meeting": {"title": [{"type": "text", "text": {"content": "Q1 sync"}}]},
            "External Org": {"select": {"name": "Poseidon"}},
        }
        target_schema = {
            "Meeting": {"type": "title"},
        }

        kept, dropped = _filter_to_target_schema(properties, target_schema)

        assert "External Org" not in kept
        assert "Meeting" in kept
        assert dropped == ["External Org"]

    def test_drops_property_with_mismatched_type(self):
        properties = {"Detail": {"multi_select": [{"name": "AI"}]}}
        target_schema = {"Detail": {"type": "select"}}

        kept, dropped = _filter_to_target_schema(properties, target_schema)

        assert kept == {}
        assert dropped == ["Detail(type:multi_select!=select)"]

    def test_keeps_property_present_and_typed_correctly(self):
        properties = {
            "Detail": {"multi_select": [{"name": "AI"}, {"name": "Tech"}]},
            "Owner": {"people": [{"id": "user-1"}]},
        }
        target_schema = {
            "Detail": {"type": "multi_select"},
            "Owner": {"type": "people"},
        }

        kept, dropped = _filter_to_target_schema(properties, target_schema)

        assert kept == properties
        assert dropped == []


class TestEnsureSelectOptionsOnTarget:
    def test_adds_missing_select_option_with_source_color(self):
        client = MagicMock()
        # Target has "AI" but not "Tech".
        target_schema = _multi_select_schema(
            "Detail",
            [{"id": "opt-ai", "name": "AI", "color": "blue"}],
        )
        properties_to_clone = {
            "Detail": {"multi_select": [{"name": "AI"}, {"name": "Tech"}]},
        }
        # Source page response carries colors inline.
        source_props = {
            "Detail": {
                "multi_select": [
                    {"id": "src-ai", "name": "AI", "color": "blue"},
                    {"id": "src-tech", "name": "Tech", "color": "purple"},
                ],
            },
        }

        _ensure_select_options_on_target(
            client=client,
            target_db_id="target-db",
            target_schema=target_schema,
            properties_to_clone=properties_to_clone,
            source_props=source_props,
            route_label="poseidon",
        )

        client.update_data_source.assert_called_once()
        args, kwargs = client.update_data_source.call_args
        assert args[0] == "target-db"
        body = args[1]
        assert "Detail" in body
        options = body["Detail"]["multi_select"]["options"]
        # Existing option preserved with id, new option appended without id +
        # with source color.
        assert {"id": "opt-ai", "name": "AI", "color": "blue"} in options
        assert {"name": "Tech", "color": "purple"} in options
        new_entries = [o for o in options if "id" not in o]
        assert new_entries == [{"name": "Tech", "color": "purple"}]

    def test_noop_when_all_options_exist(self):
        client = MagicMock()
        target_schema = _select_schema(
            "External Org",
            [{"id": "opt-1", "name": "Poseidon", "color": "green"}],
        )
        properties_to_clone = {"External Org": {"select": {"name": "Poseidon"}}}
        source_props = {
            "External Org": {"select": {"id": "src-1", "name": "Poseidon", "color": "green"}},
        }

        _ensure_select_options_on_target(
            client=client,
            target_db_id="target-db",
            target_schema=target_schema,
            properties_to_clone=properties_to_clone,
            source_props=source_props,
            route_label="poseidon",
        )

        client.update_data_source.assert_not_called()

    def test_skips_property_absent_from_target(self):
        client = MagicMock()
        # Target has no External Org column at all.
        target_schema = {"Detail": {"type": "multi_select", "multi_select": {"options": []}}}
        properties_to_clone = {
            # External Org should have already been filtered out, but defensive
            # check: even if it's here we shouldn't try to ensure options on a
            # non-existent target prop.
            "External Org": {"select": {"name": "Poseidon"}},
        }
        source_props = {
            "External Org": {"select": {"id": "src-1", "name": "Poseidon", "color": "green"}},
        }

        _ensure_select_options_on_target(
            client=client,
            target_db_id="target-db",
            target_schema=target_schema,
            properties_to_clone=properties_to_clone,
            source_props=source_props,
            route_label="poseidon",
        )

        client.update_data_source.assert_not_called()


class TestCloneOrMergeIntegration:
    def test_clone_filters_unknown_props_and_adds_missing_option(self):
        """End-to-end: source has External Org + Detail, target only has Detail
        (multi_select) with one missing option.

        Expected: External Org dropped, target schema PATCHed to add the
        missing Detail option, then pages.create called with the filtered
        properties only.
        """
        client = MagicMock()

        # Mirror does not yet exist.
        client.query_database.return_value = {"results": []}

        # Target schema: Meeting (title), Date (date), Detail (multi_select with "AI" only),
        # Owner (people), Primary Source URL (url). No External Org.
        client.retrieve_data_source.return_value = {
            "properties": {
                "Meeting": {"type": "title"},
                "Date": {"type": "date"},
                "Detail": {
                    "type": "multi_select",
                    "multi_select": {
                        "options": [{"id": "opt-ai", "name": "AI", "color": "blue"}],
                    },
                },
                "Owner": {"type": "people"},
                "Primary Source URL": {"type": "url"},
            },
        }

        # pages.create returns a mirror dict.
        client._client.pages.create.return_value = {"id": "mirror-page-id"}
        client._call_with_retry.side_effect = (
            lambda fn, *a, **kw: fn(*a, **kw)
        )

        route = Route(
            match_property="Detail",
            match_value="AI",
            target_db_id="target-db",
            label="poseidon",
            action=ACTION_MIRROR_TO_DB,
        )

        source_page = {
            "id": "source-page-id",
            "url": "https://www.notion.so/source",
            "created_time": "2026-05-21T10:00:00.000Z",
            "properties": {
                "Date": {
                    "type": "date",
                    "date": {"start": "2026-05-21"},
                },
                "Detail": {
                    "type": "multi_select",
                    "multi_select": [
                        {"id": "src-ai", "name": "AI", "color": "blue"},
                        {"id": "src-tech", "name": "Tech", "color": "purple"},
                    ],
                },
                "External Org": {
                    "type": "select",
                    "select": {"id": "src-eo", "name": "Poseidon", "color": "green"},
                },
            },
        }

        action = clone_or_merge(
            client=client,
            route=route,
            source_page=source_page,
            source_title="Q1 sync",
            source_date="2026-05-21",
            owner_user_id="user-1",
            owner_name="Guillermo",
        )

        # 1. Schema PATCH added "Tech" option (with source color), kept "AI".
        client.update_data_source.assert_called_once()
        patch_args = client.update_data_source.call_args
        assert patch_args[0][0] == "target-db"
        patch_body = patch_args[0][1]
        assert "Detail" in patch_body
        opts = patch_body["Detail"]["multi_select"]["options"]
        names = [o["name"] for o in opts]
        assert names == ["AI", "Tech"]
        assert {"name": "Tech", "color": "purple"} in opts

        # 2. pages.create called with properties dict that does NOT contain
        # External Org (target lacks it) but DOES contain Detail.
        create_kwargs = client._client.pages.create.call_args.kwargs
        props_sent = create_kwargs["properties"]
        assert "External Org" not in props_sent
        assert "Detail" in props_sent
        assert "Meeting" in props_sent
        assert create_kwargs["template"] == {
            "type": "template_id", "template_id": "source-page-id",
        }

        from src.topic_mirror.outcome import MirrorAction
        assert action == MirrorAction.CLONED
