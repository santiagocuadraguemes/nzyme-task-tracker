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
            system_prompt="You are an assistant.",
            user_prompt="Extract tasks from: @Santiago review term sheet",
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
            system_prompt="You are an assistant.",
            user_prompt="No action items today",
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
            system_prompt="system",
            user_prompt="user",
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
            system_prompt="system",
            user_prompt="user",
            categories=["Cat A", "Cat B"],
        )

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        tool_def = call_kwargs["tools"][0]["function"]["parameters"]
        assert "Cat A" in tool_def["properties"]["category"]["enum"]
        assert "Cat B" in tool_def["properties"]["category"]["enum"]

    @patch("src.ai_extractor.OpenAI")
    def test_assignee_id_is_optional_in_schema(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response(None)

        extractor = AIExtractor(api_key="sk-test")
        extractor.extract(
            system_prompt="system",
            user_prompt="user",
            categories=["Other"],
        )

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        tool_def = call_kwargs["tools"][0]["function"]["parameters"]
        assert "assignee_id" not in tool_def["required"]
        assert "assignee_id" in tool_def["properties"]

    @patch("src.ai_extractor.OpenAI")
    def test_deal_page_id_in_tool_schema(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response(None)

        extractor = AIExtractor(api_key="sk-test")
        extractor.extract(
            system_prompt="system",
            user_prompt="user",
            categories=["Other"],
        )

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        tool_def = call_kwargs["tools"][0]["function"]["parameters"]
        assert "deal_page_id" in tool_def["properties"]
        assert tool_def["properties"]["deal_page_id"]["type"] == ["string", "null"]

    @patch("src.ai_extractor.OpenAI")
    def test_extract_preserves_deal_page_id(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response([
            _mock_tool_call("create_task", {
                "title": "FDD: Send report",
                "priority": "High",
                "category": "Sourcing / Investing / Divesting",
                "deal_page_id": "deal-123",
                "parent_task_id": "tracker-456",
            }),
        ])

        extractor = AIExtractor(api_key="sk-test")
        tasks = extractor.extract("system", "user", ["Other"])

        assert tasks[0]["deal_page_id"] == "deal-123"
        assert tasks[0]["parent_task_id"] == "tracker-456"

    @patch("src.ai_extractor.OpenAI")
    def test_prompts_passed_to_openai(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response(None)

        extractor = AIExtractor(api_key="sk-test")
        extractor.extract(
            system_prompt="My system prompt with rules",
            user_prompt="My user prompt with meeting content",
            categories=["Other"],
        )

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        messages = call_kwargs["messages"]
        assert messages[0]["content"] == "My system prompt with rules"
        assert messages[1]["content"] == "My user prompt with meeting content"
