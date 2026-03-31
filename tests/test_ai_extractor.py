import json
from unittest.mock import MagicMock, patch

from src.ai_extractor import AIExtractor


def _mock_tool_call(name: str, arguments: dict) -> MagicMock:
    tc = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    return tc


def _mock_response(tool_calls: list | None = None) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.tool_calls = tool_calls
    return response


class TestAIExtractor:
    @patch("src.ai_extractor.OpenAI")
    def test_extract_returns_tasks(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response([
            _mock_tool_call("create_task", {
                "title": "Review term sheet",
                "assignee_id": "user-1",
                "priority": "High",
                "category": "Sourcing / Investing / Divesting",
                "due_date": "2026-04-01",
            }),
        ])

        extractor = AIExtractor(api_key="sk-test", model="gpt-4.1")
        tasks = extractor.extract(
            meeting_title="Deal Review - Acme",
            meeting_date="2026-03-28",
            meeting_type="Deal review",
            meeting_content="@Santiago to review term sheet by April 1",
            attendees=[{"id": "user-1", "name": "Santiago"}],
            team_members=[{"id": "user-1", "name": "Santiago"}, {"id": "user-2", "name": "Reyes"}],
            playbook="Extract action items from to-do blocks",
            hierarchy=[{"id": "cat1", "title": "Dealflow", "children": []}],
            categories=["Sourcing / Investing / Divesting", "Other"],
        )

        assert len(tasks) == 1
        assert tasks[0]["title"] == "Review term sheet"
        assert tasks[0]["assignee_id"] == "user-1"
        assert tasks[0]["priority"] == "High"

    @patch("src.ai_extractor.OpenAI")
    def test_extract_no_tool_calls_returns_empty(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response(None)

        extractor = AIExtractor(api_key="sk-test")
        tasks = extractor.extract(
            meeting_title="Standup",
            meeting_date="2026-03-28",
            meeting_type="Standup",
            meeting_content="No action items today",
            attendees=[],
            team_members=[],
            playbook="rules",
            hierarchy=[],
            categories=["Other"],
        )

        assert tasks == []

    @patch("src.ai_extractor.OpenAI")
    def test_extract_skips_invalid_json(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        tc = MagicMock()
        tc.function.name = "create_task"
        tc.function.arguments = "invalid json{{"
        mock_client.chat.completions.create.return_value = _mock_response([tc])

        extractor = AIExtractor(api_key="sk-test")
        tasks = extractor.extract(
            meeting_title="Test",
            meeting_date="2026-03-28",
            meeting_type="Other",
            meeting_content="content",
            attendees=[],
            team_members=[],
            playbook="rules",
            hierarchy=[],
            categories=["Other"],
        )

        assert tasks == []

    @patch("src.ai_extractor.OpenAI")
    def test_extract_passes_categories_to_tool(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response(None)

        extractor = AIExtractor(api_key="sk-test")
        extractor.extract(
            meeting_title="Test",
            meeting_date="2026-03-28",
            meeting_type="Other",
            meeting_content="content",
            attendees=[],
            team_members=[],
            playbook="rules",
            hierarchy=[],
            categories=["Cat A", "Cat B"],
        )

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        tool_def = call_kwargs["tools"][0]["function"]["parameters"]
        assert "Cat A" in tool_def["properties"]["category"]["enum"]
        assert "Cat B" in tool_def["properties"]["category"]["enum"]
