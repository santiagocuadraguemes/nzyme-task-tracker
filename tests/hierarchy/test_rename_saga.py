"""Tests for src.hierarchy._rename_saga (shared 5-step option-rename saga)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.hierarchy._rename_saga import (
    DropIntent,
    RenameIntent,
    execute_drop_saga,
    execute_rename_saga,
    materialize_final_options,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_response(prop_name: str, prop_type: str, options: list[dict]) -> dict:
    """Mimic the shape of `data_sources.update` / `retrieve_data_source`."""
    return {"properties": {prop_name: {"type": prop_type, prop_type: {"options": options}}}}


def _query_response(pages: list[dict]) -> dict:
    return {"results": pages, "has_more": False, "next_cursor": None}


def _select_page(page_id: str, prop: str, opt_id: str, opt_name: str) -> dict:
    return {
        "id": page_id,
        "properties": {
            prop: {"select": {"id": opt_id, "name": opt_name}},
        },
    }


def _multi_select_page(page_id: str, prop: str, entries: list[dict]) -> dict:
    """`entries` are full multi-select option dicts {id, name, color?}."""
    return {
        "id": page_id,
        "properties": {
            prop: {"multi_select": entries},
        },
    }


# ---------------------------------------------------------------------------
# TestExecuteRenameSagaSelect
# ---------------------------------------------------------------------------


class TestExecuteRenameSagaSelect:
    """Happy path + failure modes for a `select` property."""

    def test_select_happy_path_executes_5_steps(self):
        """Add new option → query 2 tagged pages → migrate each → drop old."""
        client = MagicMock()
        current = [
            {"id": "opt-old", "name": "Sourcing"},
            {"id": "opt-other", "name": "Standup"},
        ]
        # PATCH 1: Notion adds the new option, assigns id `opt-new`.
        # PATCH 2: Notion drops `opt-old`.
        client.update_data_source.side_effect = [
            _patch_response("Work area", "select", [
                {"id": "opt-old", "name": "Sourcing"},
                {"id": "opt-other", "name": "Standup"},
                {"id": "opt-new", "name": "WWW Sourcing"},
            ]),
            _patch_response("Work area", "select", [
                {"id": "opt-other", "name": "Standup"},
                {"id": "opt-new", "name": "WWW Sourcing"},
            ]),
        ]
        client.query_database.return_value = _query_response([
            _select_page("p-1", "Work area", "opt-old", "Sourcing"),
            _select_page("p-2", "Work area", "opt-old", "Sourcing"),
        ])

        intent = RenameIntent(
            old_option_id="opt-old",
            old_name="Sourcing",
            desired_name="WWW Sourcing",
            canonical_id="h-1",
        )
        new_id, post_state, details = execute_rename_saga(
            client=client,
            member_db_id="mdb-1",
            property_name="Work area",
            property_type="select",
            intent=intent,
            current_state=current,
        )

        assert new_id == "opt-new"
        # PATCH 1 then PATCH 2.
        assert client.update_data_source.call_count == 2
        # Query was issued with select-equals filter.
        client.query_database.assert_called_once()
        _, kwargs = client.query_database.call_args
        assert kwargs["filter"] == {
            "property": "Work area",
            "select": {"equals": "Sourcing"},
        }
        # Both pages were migrated.
        assert client.update_page.call_count == 2
        calls = [c.kwargs for c in client.update_page.call_args_list]
        assert all(
            c["properties"] == {"Work area": {"select": {"id": "opt-new"}}}
            for c in calls
        )
        # post_state: old gone, new with desired name present.
        assert {o["id"] for o in post_state} == {"opt-other", "opt-new"}
        new_entry = next(o for o in post_state if o["id"] == "opt-new")
        assert new_entry["name"] == "WWW Sourcing"
        # Saga emitted detail lines describing what it did.
        assert any("migrating 2 page" in d for d in details)
        assert any("saga complete" in d for d in details)

    def test_resume_skips_patch1_when_new_option_already_present(self):
        """Mid-saga state: PATCH 1 already ran; new option exists."""
        client = MagicMock()
        current = [
            {"id": "opt-old", "name": "Sourcing"},
            {"id": "opt-new", "name": "WWW Sourcing"},  # added by prior tick
        ]
        # Only PATCH 2 should be called.
        client.update_data_source.return_value = _patch_response(
            "Work area", "select",
            [{"id": "opt-new", "name": "WWW Sourcing"}],
        )
        client.query_database.return_value = _query_response([
            _select_page("p-1", "Work area", "opt-old", "Sourcing"),
        ])

        intent = RenameIntent(
            old_option_id="opt-old",
            old_name="Sourcing",
            desired_name="WWW Sourcing",
            canonical_id="h-1",
        )
        new_id, post_state, details = execute_rename_saga(
            client=client,
            member_db_id="mdb-1",
            property_name="Work area",
            property_type="select",
            intent=intent,
            current_state=current,
        )

        assert new_id == "opt-new"
        # PATCH 1 skipped → only PATCH 2 was issued.
        assert client.update_data_source.call_count == 1
        # Detail line records the resume.
        assert any("resume detected" in d for d in details)
        # The 1 remaining page got migrated.
        assert client.update_page.call_count == 1

    def test_patch1_failure_raises(self):
        client = MagicMock()
        client.update_data_source.side_effect = RuntimeError("patch1 boom")
        intent = RenameIntent(
            old_option_id="opt-old",
            old_name="X",
            desired_name="Y",
            canonical_id="h-1",
        )
        with pytest.raises(RuntimeError, match="step 1"):
            execute_rename_saga(
                client=client,
                member_db_id="mdb-1",
                property_name="Work area",
                property_type="select",
                intent=intent,
                current_state=[{"id": "opt-old", "name": "X"}],
            )
        # Query never issued, no pages migrated.
        client.query_database.assert_not_called()
        client.update_page.assert_not_called()

    def test_page_migration_failure_raises_and_skips_patch2(self):
        client = MagicMock()
        client.update_data_source.return_value = _patch_response(
            "Work area", "select", [
                {"id": "opt-old", "name": "X"},
                {"id": "opt-new", "name": "Y"},
            ],
        )
        client.query_database.return_value = _query_response([
            _select_page("p-1", "Work area", "opt-old", "X"),
        ])
        client.update_page.side_effect = RuntimeError("page migration boom")

        intent = RenameIntent(
            old_option_id="opt-old",
            old_name="X",
            desired_name="Y",
            canonical_id="h-1",
        )
        with pytest.raises(RuntimeError, match="step 3"):
            execute_rename_saga(
                client=client,
                member_db_id="mdb-1",
                property_name="Work area",
                property_type="select",
                intent=intent,
                current_state=[{"id": "opt-old", "name": "X"}],
            )
        # PATCH 1 happened (one call); PATCH 2 did NOT (second call missing).
        assert client.update_data_source.call_count == 1

    def test_patch2_failure_raises(self):
        client = MagicMock()
        client.update_data_source.side_effect = [
            _patch_response("Work area", "select", [
                {"id": "opt-old", "name": "X"},
                {"id": "opt-new", "name": "Y"},
            ]),
            RuntimeError("patch2 boom"),
        ]
        client.query_database.return_value = _query_response([])

        intent = RenameIntent(
            old_option_id="opt-old",
            old_name="X",
            desired_name="Y",
            canonical_id="h-1",
        )
        with pytest.raises(RuntimeError, match="step 4"):
            execute_rename_saga(
                client=client,
                member_db_id="mdb-1",
                property_name="Work area",
                property_type="select",
                intent=intent,
                current_state=[{"id": "opt-old", "name": "X"}],
            )

    def test_no_tagged_pages_still_proceeds_to_drop(self):
        client = MagicMock()
        client.update_data_source.side_effect = [
            _patch_response("Work area", "select", [
                {"id": "opt-old", "name": "X"},
                {"id": "opt-new", "name": "Y"},
            ]),
            _patch_response("Work area", "select",
                            [{"id": "opt-new", "name": "Y"}]),
        ]
        client.query_database.return_value = _query_response([])

        intent = RenameIntent(
            old_option_id="opt-old",
            old_name="X",
            desired_name="Y",
            canonical_id="h-1",
        )
        new_id, post_state, details = execute_rename_saga(
            client=client,
            member_db_id="mdb-1",
            property_name="Work area",
            property_type="select",
            intent=intent,
            current_state=[{"id": "opt-old", "name": "X"}],
        )

        assert new_id == "opt-new"
        client.update_page.assert_not_called()
        assert any("no pages tagged" in d for d in details)


# ---------------------------------------------------------------------------
# TestExecuteRenameSagaMultiSelect
# ---------------------------------------------------------------------------


class TestExecuteRenameSagaMultiSelect:
    """Multi-select saga: page migration must preserve every other tag."""

    def test_multi_select_swap_preserves_other_tags_on_same_page(self):
        client = MagicMock()
        client.update_data_source.side_effect = [
            _patch_response("Detail", "multi_select", [
                {"id": "opt-old", "name": "Legal DD", "color": "blue"},
                {"id": "opt-other", "name": "Tech DD", "color": "default"},
                {"id": "opt-new", "name": "Legal Due Diligence", "color": "blue"},
            ]),
            _patch_response("Detail", "multi_select", [
                {"id": "opt-other", "name": "Tech DD", "color": "default"},
                {"id": "opt-new", "name": "Legal Due Diligence", "color": "blue"},
            ]),
        ]
        # Page is tagged with BOTH old and another option.
        client.query_database.return_value = _query_response([
            _multi_select_page("p-1", "Detail", [
                {"id": "opt-old", "name": "Legal DD"},
                {"id": "opt-other", "name": "Tech DD"},
            ]),
        ])

        intent = RenameIntent(
            old_option_id="opt-old",
            old_name="Legal DD",
            desired_name="Legal Due Diligence",
            desired_color="blue",
            canonical_id="d-1",
        )
        new_id, _, _ = execute_rename_saga(
            client=client,
            member_db_id="mdb-1",
            property_name="Detail",
            property_type="multi_select",
            intent=intent,
            current_state=[
                {"id": "opt-old", "name": "Legal DD", "color": "blue"},
                {"id": "opt-other", "name": "Tech DD", "color": "default"},
            ],
        )

        assert new_id == "opt-new"
        # Migration call: array contains BOTH the preserved tag and the new id;
        # the old id/name is dropped.
        client.update_page.assert_called_once()
        kwargs = client.update_page.call_args.kwargs
        new_array = kwargs["properties"]["Detail"]["multi_select"]
        ids = [e["id"] for e in new_array]
        assert "opt-other" in ids
        assert "opt-new" in ids
        assert "opt-old" not in ids
        assert len(new_array) == 2

    def test_multi_select_filter_uses_contains(self):
        client = MagicMock()
        client.update_data_source.side_effect = [
            _patch_response("Detail", "multi_select", [
                {"id": "opt-old", "name": "X"},
                {"id": "opt-new", "name": "Y"},
            ]),
            _patch_response("Detail", "multi_select",
                            [{"id": "opt-new", "name": "Y"}]),
        ]
        client.query_database.return_value = _query_response([])

        intent = RenameIntent(
            old_option_id="opt-old",
            old_name="X",
            desired_name="Y",
            canonical_id="d-1",
        )
        execute_rename_saga(
            client=client,
            member_db_id="mdb-1",
            property_name="Detail",
            property_type="multi_select",
            intent=intent,
            current_state=[{"id": "opt-old", "name": "X"}],
        )

        _, kwargs = client.query_database.call_args
        assert kwargs["filter"] == {
            "property": "Detail",
            "multi_select": {"contains": "X"},
        }

    def test_multi_select_deduplicates_when_new_id_already_on_page(self):
        """Operator manually tagged the new option before saga ran — dedupe."""
        client = MagicMock()
        client.update_data_source.side_effect = [
            _patch_response("Detail", "multi_select", [
                {"id": "opt-old", "name": "Legal DD"},
                {"id": "opt-new", "name": "Legal Due Diligence"},
            ]),
            _patch_response("Detail", "multi_select", [
                {"id": "opt-new", "name": "Legal Due Diligence"},
            ]),
        ]
        # Page already has BOTH the old AND the new option.
        client.query_database.return_value = _query_response([
            _multi_select_page("p-1", "Detail", [
                {"id": "opt-old", "name": "Legal DD"},
                {"id": "opt-new", "name": "Legal Due Diligence"},
            ]),
        ])

        intent = RenameIntent(
            old_option_id="opt-old",
            old_name="Legal DD",
            desired_name="Legal Due Diligence",
            canonical_id="d-1",
        )
        execute_rename_saga(
            client=client,
            member_db_id="mdb-1",
            property_name="Detail",
            property_type="multi_select",
            intent=intent,
            current_state=[
                {"id": "opt-old", "name": "Legal DD"},
                {"id": "opt-new", "name": "Legal Due Diligence"},
            ],
        )

        kwargs = client.update_page.call_args.kwargs
        new_array = kwargs["properties"]["Detail"]["multi_select"]
        ids = [e["id"] for e in new_array]
        assert ids == ["opt-new"]  # exactly once


# ---------------------------------------------------------------------------
# TestMaterializeFinalOptions
# ---------------------------------------------------------------------------


class TestMaterializeFinalOptions:
    def test_no_sagas_passes_through(self):
        new_options = [{"id": "opt-1", "name": "X"}]
        result = materialize_final_options(
            new_options=new_options, renames=[], saga_results={},
        )
        assert result == new_options

    def test_swaps_old_id_to_new_id_for_completed_saga(self):
        intent = RenameIntent(
            old_option_id="opt-old", old_name="X",
            desired_name="Y", canonical_id="h-1",
        )
        new_options = [
            {"id": "opt-old", "name": "Y"},
            {"id": "opt-other", "name": "Other"},
        ]
        result = materialize_final_options(
            new_options=new_options,
            renames=[intent],
            saga_results={"h-1": "opt-new"},
        )
        assert result == [
            {"id": "opt-new", "name": "Y"},
            {"id": "opt-other", "name": "Other"},
        ]

    def test_skips_swap_for_failed_saga(self):
        """If saga didn't complete, the canonical_id isn't in saga_results."""
        intent = RenameIntent(
            old_option_id="opt-old", old_name="X",
            desired_name="Y", canonical_id="h-1",
        )
        new_options = [{"id": "opt-old", "name": "Y"}]
        # Empty saga_results → no swap.
        result = materialize_final_options(
            new_options=new_options, renames=[intent], saga_results={},
        )
        assert result == new_options


# ---------------------------------------------------------------------------
# TestExecuteDropSaga
# ---------------------------------------------------------------------------


class TestExecuteDropSaga:
    """Drop saga: clear tagged pages → PATCH options array minus the option."""

    def test_select_drop_clears_tagged_pages_then_drops(self):
        client = MagicMock()
        client.query_database.return_value = _query_response([
            _select_page("p-1", "Work area", "opt-gone", "Old"),
            _select_page("p-2", "Work area", "opt-gone", "Old"),
        ])
        client.update_data_source.return_value = _patch_response(
            "Work area", "select",
            [{"id": "opt-other", "name": "Standup"}],
        )

        intent = DropIntent(
            old_option_id="opt-gone",
            old_name="Old",
            canonical_id="h-1",
        )
        post_state, details = execute_drop_saga(
            client=client,
            member_db_id="mdb-1",
            property_name="Work area",
            property_type="select",
            intent=intent,
            current_state=[
                {"id": "opt-other", "name": "Standup"},
                {"id": "opt-gone", "name": "Old"},
            ],
        )

        # Both pages cleared (select → None).
        assert client.update_page.call_count == 2
        for c in client.update_page.call_args_list:
            assert c.kwargs["properties"] == {"Work area": {"select": None}}
        # PATCH sent the array MINUS opt-gone.
        client.update_data_source.assert_called_once()
        patch_args = client.update_data_source.call_args.args
        patch_opts = patch_args[1]["Work area"]["select"]["options"]
        assert "opt-gone" not in [o.get("id") for o in patch_opts]
        # post_state matches.
        assert {o["id"] for o in post_state} == {"opt-other"}
        # Detail line announces the clear count and the drop.
        assert any("clearing" in d and "2 page" in d for d in details)
        assert any("drop complete" in d for d in details)

    def test_multi_select_drop_strips_entry_preserving_other_tags(self):
        client = MagicMock()
        client.query_database.return_value = _query_response([
            _multi_select_page("p-1", "Detail", [
                {"id": "opt-gone", "name": "Old"},
                {"id": "opt-keep", "name": "Tech DD"},
            ]),
        ])
        client.update_data_source.return_value = _patch_response(
            "Detail", "multi_select",
            [{"id": "opt-keep", "name": "Tech DD"}],
        )

        intent = DropIntent(
            old_option_id="opt-gone",
            old_name="Old",
            canonical_id="d-1",
        )
        execute_drop_saga(
            client=client,
            member_db_id="mdb-1",
            property_name="Detail",
            property_type="multi_select",
            intent=intent,
            current_state=[
                {"id": "opt-keep", "name": "Tech DD"},
                {"id": "opt-gone", "name": "Old"},
            ],
        )

        # Page's other tag is preserved; gone entry is removed.
        client.update_page.assert_called_once()
        new_array = client.update_page.call_args.kwargs["properties"][
            "Detail"]["multi_select"]
        ids = [e["id"] for e in new_array]
        assert ids == ["opt-keep"]

    def test_drop_with_no_tagged_pages_still_patches(self):
        client = MagicMock()
        client.query_database.return_value = _query_response([])
        client.update_data_source.return_value = _patch_response(
            "Work area", "select", [],
        )

        intent = DropIntent(
            old_option_id="opt-gone",
            old_name="Old",
            canonical_id="h-1",
        )
        execute_drop_saga(
            client=client,
            member_db_id="mdb-1",
            property_name="Work area",
            property_type="select",
            intent=intent,
            current_state=[{"id": "opt-gone", "name": "Old"}],
        )

        client.update_page.assert_not_called()
        client.update_data_source.assert_called_once()

    def test_drop_page_migration_failure_raises_and_skips_patch(self):
        client = MagicMock()
        client.query_database.return_value = _query_response([
            _select_page("p-1", "Work area", "opt-gone", "Old"),
        ])
        client.update_page.side_effect = RuntimeError("page clear boom")

        intent = DropIntent(
            old_option_id="opt-gone",
            old_name="Old",
            canonical_id="h-1",
        )
        with pytest.raises(RuntimeError, match="step 2"):
            execute_drop_saga(
                client=client,
                member_db_id="mdb-1",
                property_name="Work area",
                property_type="select",
                intent=intent,
                current_state=[{"id": "opt-gone", "name": "Old"}],
            )
        # PATCH never issued — option stays on Notion, next tick retries.
        client.update_data_source.assert_not_called()


# ---------------------------------------------------------------------------
# TestUnsupportedPropertyType
# ---------------------------------------------------------------------------


def test_unsupported_property_type_raises():
    intent = RenameIntent(
        old_option_id="opt-old", old_name="X",
        desired_name="Y", canonical_id="h-1",
    )
    with pytest.raises(ValueError, match="unsupported property_type"):
        execute_rename_saga(
            client=MagicMock(),
            member_db_id="mdb-1",
            property_name="Other",
            property_type="status",  # not supported
            intent=intent,
            current_state=[],
        )
