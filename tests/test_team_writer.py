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
            "meeting_page_id": "meeting-1",
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
        assert props["Meeting - Relation"]["relation"][0]["id"] == "meeting-1"

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
            "meeting_page_id": "meeting-1",
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
