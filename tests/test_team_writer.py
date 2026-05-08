from unittest.mock import MagicMock

from src.tracker.team_writer import TeamTaskTrackerWriter


class TestTeamTaskTrackerWriter:
    def test_create_task_full(self):
        client = MagicMock()
        client.create_page.return_value = {"id": "new-page-1"}
        writer = TeamTaskTrackerWriter(client, "db-tracker")

        task = {
            "title": "Review term sheet",
            "assignee_id": "user-1",
            "due_date": "2026-04-01",
            "priority": "High",
            "category": "Sourcing / Investing / Divesting",
            "parent_task_id": "parent-1",
            "status": "Not Started",
        }
        result = writer.create_task(task)

        assert result == {"id": "new-page-1"}
        call_args = client.create_page.call_args
        props = call_args.args[1]
        assert props["Task"]["title"][0]["text"]["content"] == "Review term sheet"
        assert props["Status"]["status"]["name"] == "Not Started"
        assert props["Assignee (edit access)"]["people"][0]["id"] == "user-1"
        assert props["Due Date"]["date"]["start"] == "2026-04-01"
        assert props["Priority"]["select"]["name"] == "High"
        assert props["Category"]["select"]["name"] == "Sourcing / Investing / Divesting"
        assert props["Parent item"]["relation"][0]["id"] == "parent-1"
        assert "Meeting - Relation" not in props

    def test_create_task_with_deal_relation(self):
        client = MagicMock()
        client.create_page.return_value = {"id": "new-page"}
        writer = TeamTaskTrackerWriter(client, "db-tracker")

        task = {
            "title": "FDD: Send report to A&M",
            "priority": "High",
            "category": "Sourcing / Investing / Divesting",
            "deal_page_id": "deal-citadel-123",
            "parent_task_id": "tracker-citadel-456",
        }
        writer.create_task(task)

        props = client.create_page.call_args.args[1]
        assert props["Deal Relation (only for deal tasks)"]["relation"][0]["id"] == "deal-citadel-123"
        assert props["Parent item"]["relation"][0]["id"] == "tracker-citadel-456"

    def test_create_task_without_deal_relation(self):
        client = MagicMock()
        client.create_page.return_value = {"id": "new-page"}
        writer = TeamTaskTrackerWriter(client, "db-tracker")

        task = {"title": "General task", "priority": "Low", "category": "Operations"}
        writer.create_task(task)

        props = client.create_page.call_args.args[1]
        assert "Deal Relation (only for deal tasks)" not in props

    def test_create_task_minimal(self):
        client = MagicMock()
        client.create_page.return_value = {"id": "new-page-2"}
        writer = TeamTaskTrackerWriter(client, "db-tracker")

        task = {"title": "Simple task", "priority": "Low", "category": "Other"}
        result = writer.create_task(task)

        props = client.create_page.call_args.args[1]
        assert "Assignee (edit access)" not in props
        assert "Due Date" not in props
        assert "Parent item" not in props
        assert "Meeting - Relation" not in props

    def test_dry_run_does_not_write(self):
        client = MagicMock()
        writer = TeamTaskTrackerWriter(client, "db-tracker", dry_run=True)

        result = writer.create_task({"title": "Test", "priority": "Low", "category": "Other"})

        assert result is None
        client.create_page.assert_not_called()

    def test_write_batch(self):
        client = MagicMock()
        client.create_page.return_value = {"id": "new-page"}
        writer = TeamTaskTrackerWriter(client, "db-tracker")

        tasks = [
            {"title": "Task 1", "priority": "High", "category": "Operations"},
            {"title": "Task 2", "priority": "Low", "category": "Other"},
        ]
        results = writer.write_batch(tasks)

        assert len(results) == 2
        assert client.create_page.call_count == 2

    def test_write_batch_continues_on_error(self):
        client = MagicMock()
        client.create_page.side_effect = [Exception("API error"), {"id": "page-2"}]
        writer = TeamTaskTrackerWriter(client, "db-tracker")

        tasks = [
            {"title": "Fail task", "priority": "High", "category": "Other"},
            {"title": "OK task", "priority": "Low", "category": "Other"},
        ]
        results = writer.write_batch(tasks)

        assert len(results) == 1

    def test_external_assignees_prepend_title(self):
        client = MagicMock()
        client.create_page.return_value = {"id": "new-page"}
        writer = TeamTaskTrackerWriter(client, "db-tracker")

        task = {
            "title": "Call Enrique about SAREB",
            "priority": "High",
            "category": "Value Creation (Portfolio)",
            "external_assignees": ["Miguel Serrano", "Alvaro"],
        }
        writer.create_task(task)

        props = client.create_page.call_args.args[1]
        written = props["Task"]["title"][0]["text"]["content"]
        assert written == "[Ext: Miguel Serrano, Alvaro] Call Enrique about SAREB"

    def test_external_assignees_empty_list_unchanged(self):
        client = MagicMock()
        client.create_page.return_value = {"id": "new-page"}
        writer = TeamTaskTrackerWriter(client, "db-tracker")

        task = {
            "title": "Plain task",
            "priority": "Low",
            "category": "Other",
            "external_assignees": [],
        }
        writer.create_task(task)

        props = client.create_page.call_args.args[1]
        assert props["Task"]["title"][0]["text"]["content"] == "Plain task"

    def test_external_assignees_respect_title_length_cap(self):
        client = MagicMock()
        client.create_page.return_value = {"id": "new-page"}
        writer = TeamTaskTrackerWriter(client, "db-tracker")

        long_title = "A" * 3000
        task = {
            "title": long_title,
            "priority": "Low",
            "category": "Other",
            "external_assignees": ["Ext Person"],
        }
        writer.create_task(task)

        props = client.create_page.call_args.args[1]
        written = props["Task"]["title"][0]["text"]["content"]
        assert written.startswith("[Ext: Ext Person] ")
        assert len(written) <= 2000

    def test_link_tasks_to_meeting_writes_relation(self):
        client = MagicMock()
        client.get_page.return_value = {"properties": {}}
        writer = TeamTaskTrackerWriter(client, "db-tracker")

        writer.link_tasks_to_meeting("meeting-1", ["task-a", "task-b"])

        update_kwargs = client.update_page.call_args.kwargs
        assert update_kwargs["page_id"] == "meeting-1"
        rel = update_kwargs["properties"]["Task - Relation"]["relation"]
        assert [r["id"] for r in rel] == ["task-a", "task-b"]

    def test_link_tasks_to_meeting_merges_with_existing(self):
        client = MagicMock()
        client.get_page.return_value = {
            "properties": {
                "Task - Relation": {
                    "relation": [{"id": "existing-1"}, {"id": "task-a"}],
                },
            },
        }
        writer = TeamTaskTrackerWriter(client, "db-tracker")

        writer.link_tasks_to_meeting("meeting-1", ["task-a", "task-b"])

        rel = client.update_page.call_args.kwargs["properties"]["Task - Relation"]["relation"]
        assert [r["id"] for r in rel] == ["existing-1", "task-a", "task-b"]

    def test_link_tasks_to_meeting_dry_run_skips(self):
        client = MagicMock()
        writer = TeamTaskTrackerWriter(client, "db-tracker", dry_run=True)

        writer.link_tasks_to_meeting("meeting-1", ["task-a"])

        client.get_page.assert_not_called()
        client.update_page.assert_not_called()

    def test_link_tasks_to_meeting_empty_list_skips(self):
        client = MagicMock()
        writer = TeamTaskTrackerWriter(client, "db-tracker")

        writer.link_tasks_to_meeting("meeting-1", [])

        client.get_page.assert_not_called()
        client.update_page.assert_not_called()

    def test_link_tasks_to_meeting_swallows_api_errors(self):
        client = MagicMock()
        client.get_page.side_effect = Exception("API down")
        writer = TeamTaskTrackerWriter(client, "db-tracker")

        # Must not raise — failure is logged but pipeline continues
        writer.link_tasks_to_meeting("meeting-1", ["task-a"])

        client.update_page.assert_not_called()
