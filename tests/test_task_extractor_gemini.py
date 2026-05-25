"""Tests for the Gemini-native ``extract_from_raw`` path.

The native ``google-genai`` SDK is mocked end-to-end so the tests run
offline. We verify:
- the routing on model name prefix ``gemini-*`` picks the native path;
- the stable system prefix is uploaded to ``caches.create`` once and
  reused across calls;
- ``response_schema`` is set on every call;
- a cache NOT_FOUND on the call site triggers a single retry without
  the stale handle;
- the returned shape matches the OpenAI-compat path.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.transcript_pipeline import task_extractor as te
from src.transcript_pipeline.task_extractor import TaskExtractor


@pytest.fixture(autouse=True)
def _clear_module_state():
    """Reset module-level Gemini caching / schema flags between tests."""
    te._GEMINI_CACHE_REGISTRY.clear()
    te._GEMINI_CACHE_DISABLED = False
    te._GEMINI_SCHEMA_DISABLED = False
    yield
    te._GEMINI_CACHE_REGISTRY.clear()
    te._GEMINI_CACHE_DISABLED = False
    te._GEMINI_SCHEMA_DISABLED = False


def _stub_response(payload: dict, *, prompt_tokens: int = 5000,
                   cached_tokens: int = 0, output_tokens: int = 200):
    """Build a fake google-genai GenerateContentResponse."""
    resp = MagicMock()
    resp.text = json.dumps(payload)
    resp.usage_metadata = MagicMock()
    resp.usage_metadata.prompt_token_count = prompt_tokens
    resp.usage_metadata.cached_content_token_count = cached_tokens
    resp.usage_metadata.candidates_token_count = output_tokens
    return resp


def _wire_genai_client(extractor: TaskExtractor, *, cache_name="caches/abc123"):
    """Attach a MagicMock google-genai client to the extractor."""
    fake = MagicMock()

    fake_cache = MagicMock()
    fake_cache.name = cache_name
    fake.caches.create.return_value = fake_cache

    extractor._genai_client = fake
    return fake


_LARGE_TERMINOLOGY = "term:" + ("x" * 5000)  # exceeds _GEMINI_CACHE_MIN_CHARS


def test_routes_gemini_model_to_native_path():
    ext = TaskExtractor(api_key="k", model="gemini-3-flash-preview")
    # Replace the real OpenAI client with a mock so we can assert no
    # call hit the OpenAI path.
    ext._client = MagicMock()
    fake = _wire_genai_client(ext)

    fake.models.generate_content.return_value = _stub_response(
        {"domain_corrections": [], "speaker_resolutions": [], "tasks": []}
    )

    ext.extract_from_raw(
        transcript="Santiago: hi\nJacob: hello",
        attendees=[{"id": "1", "name": "Santiago"}],
        terminology=_LARGE_TERMINOLOGY,
        meeting_title="Test", meeting_date="2026-05-11",
    )

    assert fake.models.generate_content.call_count == 1
    assert ext._client.chat.completions.create.call_count == 0


def test_cache_is_created_once_and_reused():
    ext = TaskExtractor(api_key="k", model="gemini-3-flash-preview")
    fake = _wire_genai_client(ext)
    fake.models.generate_content.return_value = _stub_response(
        {"domain_corrections": [], "speaker_resolutions": [], "tasks": []}
    )

    for _ in range(3):
        ext.extract_from_raw(
            transcript="Santiago: hi",
            attendees=[],
            terminology=_LARGE_TERMINOLOGY,
            meeting_title="A", meeting_date="2026-05-11",
        )

    # Same system prefix → caches.create only called once total.
    assert fake.caches.create.call_count == 1
    assert fake.models.generate_content.call_count == 3
    # Every call passes the cached content handle on the config.
    for call in fake.models.generate_content.call_args_list:
        cfg = call.kwargs["config"]
        assert cfg.cached_content == "caches/abc123"


def test_response_schema_set_on_every_call():
    ext = TaskExtractor(api_key="k", model="gemini-3-flash-preview")
    fake = _wire_genai_client(ext)
    fake.models.generate_content.return_value = _stub_response(
        {"domain_corrections": [], "speaker_resolutions": [], "tasks": []}
    )

    ext.extract_from_raw(
        transcript="x",
        attendees=[],
        terminology=_LARGE_TERMINOLOGY,
        meeting_title="t", meeting_date="2026-05-11",
    )

    cfg = fake.models.generate_content.call_args.kwargs["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_schema is not None


def test_skips_cache_for_small_system_prefix():
    ext = TaskExtractor(api_key="k", model="gemini-3-flash-preview")
    fake = _wire_genai_client(ext)
    fake.models.generate_content.return_value = _stub_response(
        {"domain_corrections": [], "speaker_resolutions": [], "tasks": []}
    )

    # No terminology/org_chart → system prefix is just MERGED_SYSTEM_PROMPT,
    # which is large but borderline — pass a small artificial threshold by
    # leaving terminology empty when MERGED_SYSTEM_PROMPT is already huge.
    # The minimum guard fires when prefix < _GEMINI_CACHE_MIN_CHARS; we
    # simulate that by patching the threshold up.
    original = te._GEMINI_CACHE_MIN_CHARS
    te._GEMINI_CACHE_MIN_CHARS = 10**9  # force-skip caching
    try:
        ext.extract_from_raw(
            transcript="hi",
            attendees=[],
            meeting_title="t", meeting_date="2026-05-11",
        )
    finally:
        te._GEMINI_CACHE_MIN_CHARS = original

    # Cache create skipped → systemInstruction sent inline.
    assert fake.caches.create.call_count == 0
    cfg = fake.models.generate_content.call_args.kwargs["config"]
    assert cfg.system_instruction is not None
    assert cfg.cached_content is None


def test_retries_without_cache_on_expired_cached_content():
    ext = TaskExtractor(api_key="k", model="gemini-3-flash-preview")
    fake = _wire_genai_client(ext, cache_name="caches/stale")

    # First call: raise a NOT_FOUND-style error. Second call (retry): succeed.
    fake.models.generate_content.side_effect = [
        Exception("404 NOT_FOUND: cached content gone"),
        _stub_response({"domain_corrections": [], "speaker_resolutions": [], "tasks": []}),
    ]

    ext.extract_from_raw(
        transcript="hi",
        attendees=[],
        terminology=_LARGE_TERMINOLOGY,
        meeting_title="t", meeting_date="2026-05-11",
    )

    assert fake.models.generate_content.call_count == 2
    # First call used the cache, second sent the system inline.
    cfg_first = fake.models.generate_content.call_args_list[0].kwargs["config"]
    assert cfg_first.cached_content == "caches/stale"
    cfg_retry = fake.models.generate_content.call_args_list[1].kwargs["config"]
    assert cfg_retry.cached_content is None
    assert cfg_retry.system_instruction is not None
    # Stale cache handle was evicted from the registry.
    assert "caches/stale" not in te._GEMINI_CACHE_REGISTRY.values()


def test_returns_task_list_in_extractor_shape():
    ext = TaskExtractor(api_key="k", model="gemini-3-flash-preview")
    fake = _wire_genai_client(ext)
    fake.models.generate_content.return_value = _stub_response({
        "domain_corrections": ["civic lend→Civislend"],
        "speaker_resolutions": [{"label": "Speaker 1", "name": "Santiago", "evidence": "topic"}],
        "tasks": [
            {
                "title": "Send FDD comments",
                "assignee": "Jacob",
                "internal_assignees": ["Jacob"],
                "external_assignees": [],
                "commitment_type": "hard",
                "priority": "High",
                "due_date": "2026-05-15",
                "confidence": "high",
            }
        ],
    })

    tasks = ext.extract_from_raw(
        transcript="x",
        attendees=[],
        terminology=_LARGE_TERMINOLOGY,
        meeting_title="t", meeting_date="2026-05-11",
    )

    assert len(tasks) == 1
    assert tasks[0]["title"] == "Send FDD comments"
    assert tasks[0]["commitment_type"] == "hard"
    assert tasks[0]["internal_assignees"] == ["Jacob"]


def test_falls_back_when_cache_create_fails():
    ext = TaskExtractor(api_key="k", model="gemini-3-flash-preview")
    fake = _wire_genai_client(ext)
    fake.caches.create.side_effect = RuntimeError("rate limited")
    fake.models.generate_content.return_value = _stub_response(
        {"domain_corrections": [], "speaker_resolutions": [], "tasks": []}
    )

    ext.extract_from_raw(
        transcript="hi",
        attendees=[],
        terminology=_LARGE_TERMINOLOGY,
        meeting_title="t", meeting_date="2026-05-11",
    )

    # Cache creation failed → call still happens with systemInstruction inline.
    cfg = fake.models.generate_content.call_args.kwargs["config"]
    assert cfg.cached_content is None
    assert cfg.system_instruction is not None


def test_free_tier_429_disables_caching_quietly():
    ext = TaskExtractor(api_key="k", model="gemini-3-flash-preview")
    fake = _wire_genai_client(ext)
    fake.caches.create.side_effect = RuntimeError(
        "429 RESOURCE_EXHAUSTED: TotalCachedContentStorageTokensPerModelFreeTier "
        "limit exceeded for model gemini-3-flash: limit=0, requested=4330"
    )
    fake.models.generate_content.return_value = _stub_response(
        {"domain_corrections": [], "speaker_resolutions": [], "tasks": []}
    )

    # Two extractions in a row. First sets the sticky flag; second should
    # skip caches.create entirely.
    for _ in range(2):
        ext.extract_from_raw(
            transcript="hi",
            attendees=[],
            terminology=_LARGE_TERMINOLOGY,
            meeting_title="t", meeting_date="2026-05-11",
        )

    assert fake.caches.create.call_count == 1  # disabled after the first 429
    assert fake.models.generate_content.call_count == 2
    assert te._GEMINI_CACHE_DISABLED is True


def test_schema_rejection_retries_without_schema_and_sticks():
    ext = TaskExtractor(api_key="k", model="gemini-3-flash-preview")
    fake = _wire_genai_client(ext)

    # First call: schema rejected. Retry: succeed. Second extraction:
    # schema already disabled — only one call, no retry.
    ok_response = _stub_response(
        {"domain_corrections": [], "speaker_resolutions": [], "tasks": []}
    )
    fake.models.generate_content.side_effect = [
        Exception('400 INVALID_ARGUMENT: Unknown name "additional_properties" '
                  "at 'generation_config.response_schema': Cannot find field."),
        ok_response,
        ok_response,
    ]

    ext.extract_from_raw(
        transcript="hi", attendees=[], terminology=_LARGE_TERMINOLOGY,
        meeting_title="t", meeting_date="2026-05-11",
    )
    ext.extract_from_raw(
        transcript="hi2", attendees=[], terminology=_LARGE_TERMINOLOGY,
        meeting_title="t", meeting_date="2026-05-11",
    )

    assert te._GEMINI_SCHEMA_DISABLED is True
    # First extraction: 2 calls (rejected + retry). Second extraction: 1.
    assert fake.models.generate_content.call_count == 3
    # Second extraction never sent responseSchema.
    last_cfg = fake.models.generate_content.call_args_list[-1].kwargs["config"]
    assert last_cfg.response_schema is None


def test_system_prompt_override_replaces_hardcoded_prompt():
    ext = TaskExtractor(api_key="k", model="gemini-3-flash-preview")
    fake = _wire_genai_client(ext)
    fake.models.generate_content.return_value = _stub_response(
        {"domain_corrections": [], "speaker_resolutions": [], "tasks": []}
    )

    custom = "CUSTOM PROMPT TEXT FROM NOTION"
    ext.extract_from_raw(
        transcript="hi",
        attendees=[],
        terminology=_LARGE_TERMINOLOGY,
        meeting_title="t", meeting_date="2026-05-11",
        system_prompt_override=custom,
    )

    create_kwargs = fake.caches.create.call_args.kwargs
    sys_inst = create_kwargs["config"].system_instruction
    assert sys_inst.startswith(custom)
    assert te.MERGED_SYSTEM_PROMPT not in sys_inst


def test_non_gemini_model_uses_openai_path():
    ext = TaskExtractor(api_key="k", model="gpt-5-mini")
    # The OpenAI client is real — replace it with a mock for this test.
    ext._client = MagicMock()
    fake_completion = MagicMock()
    fake_completion.choices = [MagicMock()]
    fake_completion.choices[0].message.content = json.dumps(
        {"domain_corrections": [], "speaker_resolutions": [], "tasks": []}
    )
    fake_completion.usage = MagicMock(
        prompt_tokens=1, completion_tokens=1, prompt_tokens_details=None,
    )
    ext._client.chat.completions.create.return_value = fake_completion

    ext.extract_from_raw(
        transcript="x",
        attendees=[],
        terminology=_LARGE_TERMINOLOGY,
        meeting_title="t", meeting_date="2026-05-11",
    )

    assert ext._client.chat.completions.create.call_count == 1
    # The native client must not have been touched.
    assert ext._genai_client is None
