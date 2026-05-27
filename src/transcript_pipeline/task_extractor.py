"""Task extraction from meeting transcripts.

``extract_from_raw`` is the single entry point: it runs the merged
correction + extraction flow in one LLM call. Domain-term correction and
speaker resolution happen inline; the model emits tasks only (the
corrected transcript is not returned).

When the configured model is a ``gemini-*`` one, the call uses the
native ``google-genai`` SDK so that (a) the stable system prefix can be
sent once via explicit context caching, and (b) the output shape is
enforced via ``response_schema``. Non-Gemini models keep the existing
OpenAI-compatible JSON-object path.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from openai import OpenAI

from src.transcript_pipeline.schemas import MergedExtractionOutput
from src.utils.llm_dump import dump_call
from src.utils.llm_logging import log_usage, log_usage_genai

logger = logging.getLogger(__name__)

# Module-level cache of {sha256(system_message) -> cached_content_name}.
# Lives for the lifetime of the Python process — re-used across meetings
# in a sync tick and (in Lambda) across warm invocations of the same
# container until the server-side TTL expires.
_GEMINI_CACHE_REGISTRY: dict[str, str] = {}

# Cache TTL — keep long enough to span a full sync tick and a healthy run
# of Lambda invocations. Gemini's NOT_FOUND on expiry is handled by
# falling back to a fresh cache on the next call.
_GEMINI_CACHE_TTL = "3600s"

# Minimum tokens before caching is even worth attempting. Gemini's caching
# tier has a model-dependent minimum (≈1024–4096 input tokens). Below
# that, ``caches.create`` rejects with ``INVALID_ARGUMENT`` so we skip
# the cache attempt entirely and fall back to a plain call.
_GEMINI_CACHE_MIN_CHARS = 4000

# Sticky flag: set the first time we discover this API tier doesn't allow
# caching at all (free tier returns 429 RESOURCE_EXHAUSTED with limit=0).
# Once set, we stop attempting cache creation for the rest of the process
# so the noisy traceback only ever fires once.
_GEMINI_CACHE_DISABLED = False

# Sticky flag: set the first time Gemini rejects ``responseSchema`` so we
# stop sending it for the rest of the process. The model still emits JSON
# via ``responseMimeType``; the prompt's output section guides the shape.
_GEMINI_SCHEMA_DISABLED = False

# Measurement-only override. ``scripts/compare_candidate.py`` calls
# ``set_response_schema_override(MergedExtractionOutputNoSR)`` (or another
# variant from ``schemas.CANDIDATE_SCHEMAS``) to test a candidate against
# the corpus. Production code never sets this; default behaviour uses
# ``MergedExtractionOutput``.
_RESPONSE_SCHEMA_OVERRIDE: Any = None


def set_response_schema_override(schema: Any) -> None:
    """Force the next merged calls to use ``schema`` instead of the default.

    Pass ``None`` to clear. Only used by measurement scripts.
    """
    global _RESPONSE_SCHEMA_OVERRIDE
    _RESPONSE_SCHEMA_OVERRIDE = schema

_TASK_FIELD_MAP = {
    "t": "title",
    "ia": "internal_assignees",
    "ea": "external_assignees",
    "ct": "commitment_type",
    "p": "priority",
    "dd": "due_date",
}

# Confidence is derived from commitment_type rather than emitted directly
# (plan A3). Keeps the downstream contract — task["confidence"] — intact.
_CT_TO_CONFIDENCE = {
    "hard": "high",
    "conditional": "medium",
    "soft": "medium",
    "group": "low",
}


class TaskExtractor:
    """Extracts action items from a corrected transcript via LLM."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        # The native Gemini client is created lazily on first use so that
        # non-Gemini runs don't import or initialise it. Stored on the
        # instance once created.
        self._genai_client: Any | None = None
        # Set by the merged extractor after each call. Holds the raw
        # short-key JSON (tasks + scratch fields) so measurement scripts
        # can reconstruct what the model actually emitted, byte-for-byte.
        # Not part of the public contract — diagnostic only.
        self._last_raw_data: dict | None = None

    @property
    def _is_gemini(self) -> bool:
        return self._model.startswith("gemini-")

    def _get_genai_client(self) -> Any:
        """Return a memoised native google-genai client for this extractor."""
        if self._genai_client is None:
            from google import genai

            self._genai_client = genai.Client(api_key=self._api_key)
        return self._genai_client

    def _build_merged_messages(
        self,
        transcript: str,
        attendees: list[dict[str, str]],
        system_prompt: str,
        org_chart: str = "",
        terminology: str = "",
        meeting_title: str = "",
        meeting_date: str = "",
        enriched_attendee_str: str = "",
        notes_text: str = "",
    ) -> tuple[str, str]:
        """Construct (system_message, user_prompt) for the merged call.

        Separated from the transport so the OpenAI-compat path and the
        native Gemini path see byte-identical inputs. ``system_prompt`` is
        the instructions loaded from the Notion playbook page (required —
        no in-code fallback).
        """
        if not enriched_attendee_str:
            attendee_names = [a["name"] for a in attendees]
            enriched_attendee_str = (
                ", ".join(attendee_names) if attendee_names else "(unknown)"
            )

        # Stable prefix — system message holds the instructions plus
        # terminology + org chart. Reused across every meeting in a sync
        # tick. The native-Gemini path caches it explicitly; the
        # OpenAI-compat path relies on prefix auto-caching.
        system_sections = [system_prompt]
        if terminology:
            system_sections.append(f"=== TERMINOLOGY DICTIONARY ===\n{terminology}")
        if org_chart:
            system_sections.append(
                f"=== ORG CHART (team roles & responsibilities) ===\n{org_chart}"
            )
        system_message = "\n\n".join(system_sections)

        sections: list[str] = []
        if meeting_title:
            sections.append(f"=== MEETING ===\nTitle: {meeting_title}")
            if meeting_date:
                sections[-1] += f"\nDate: {meeting_date}"
        elif meeting_date:
            sections.append(f"=== MEETING ===\nDate: {meeting_date}")

        if meeting_date:
            sections.append(
                f"Today's date is {meeting_date}. Resolve relative dates "
                f"('tomorrow', 'next week', 'el viernes') relative to this date."
            )

        sections.append(f"=== MEETING ATTENDEES ===\n{enriched_attendee_str}")

        if notes_text:
            sections.append(
                f"=== HUMAN NOTES (high-priority context from the note-taker) ===\n{notes_text}"
            )

        sections.append(f"=== RAW TRANSCRIPT ===\n{transcript}")

        return system_message, "\n\n".join(sections)

    def _get_or_create_gemini_cache(self, system_message: str) -> str | None:
        """Resolve a cached_content name for this system prefix.

        Returns the cache name when caching is available, or ``None``
        when the prefix is too small to cache, when caching has been
        disabled for this process (free tier), or when cache creation
        failed transiently (we fall back to a regular non-cached call).
        """
        global _GEMINI_CACHE_DISABLED

        if _GEMINI_CACHE_DISABLED:
            return None
        if len(system_message) < _GEMINI_CACHE_MIN_CHARS:
            return None

        cache_key = hashlib.sha256(system_message.encode("utf-8")).hexdigest()
        cached_name = _GEMINI_CACHE_REGISTRY.get(cache_key)
        if cached_name:
            return cached_name

        try:
            from google.genai import types as genai_types

            client = self._get_genai_client()
            cached = client.caches.create(
                model=self._model,
                config=genai_types.CreateCachedContentConfig(
                    systemInstruction=system_message,
                    ttl=_GEMINI_CACHE_TTL,
                    displayName=f"merged-extraction:{cache_key[:8]}",
                ),
            )
        except Exception as e:
            msg = str(e)
            # Free tier: TotalCachedContentStorageTokensPerModelFreeTier
            # limit=0. Stop trying for the rest of the process.
            if "RESOURCE_EXHAUSTED" in msg or "limit=0" in msg:
                _GEMINI_CACHE_DISABLED = True
                logger.info(
                    "Gemini context caching unavailable on this API tier "
                    "(free tier has no cache quota); proceeding without cache "
                    "for the rest of the process."
                )
            else:
                # Other reasons: prefix below provider's minimum, transient
                # failure, model doesn't support caching. Quiet one-liner —
                # the call still works without a cache.
                first_line = msg.splitlines()[0] if msg else type(e).__name__
                logger.warning(
                    "Gemini cache creation failed; falling back to non-cached "
                    "call: %s", first_line,
                )
            return None

        _GEMINI_CACHE_REGISTRY[cache_key] = cached.name
        logger.info(
            "Gemini cache created: model=%s, %d chars, ttl=%s",
            self._model, len(system_message), _GEMINI_CACHE_TTL,
        )
        return cached.name

    def _extract_from_raw_gemini(
        self, system_message: str, user_prompt: str,
    ) -> list[dict]:
        """Native google-genai transport: explicit cache + response_schema."""
        global _GEMINI_SCHEMA_DISABLED
        from google.genai import types as genai_types

        client = self._get_genai_client()
        cache_name = self._get_or_create_gemini_cache(system_message)

        def _build_cfg(*, use_cache: bool, use_schema: bool) -> Any:
            cfg: dict[str, Any] = {"responseMimeType": "application/json"}
            if use_schema:
                cfg["responseSchema"] = (
                    _RESPONSE_SCHEMA_OVERRIDE or MergedExtractionOutput
                )
            if use_cache and cache_name:
                cfg["cachedContent"] = cache_name
            else:
                cfg["systemInstruction"] = system_message
            return genai_types.GenerateContentConfig(**cfg)

        def _call(*, use_cache: bool, use_schema: bool) -> Any:
            return client.models.generate_content(
                model=self._model,
                contents=user_prompt,
                config=_build_cfg(use_cache=use_cache, use_schema=use_schema),
            )

        use_schema = not _GEMINI_SCHEMA_DISABLED
        try:
            response = _call(use_cache=True, use_schema=use_schema)
        except Exception as e:
            msg = str(e)
            msg_upper = msg.upper()
            # Stale cache between registration and call → drop handle + retry.
            if cache_name and "NOT_FOUND" in msg_upper:
                logger.warning(
                    "Gemini cached_content %s expired; retrying without cache",
                    cache_name[:24],
                )
                for k, v in list(_GEMINI_CACHE_REGISTRY.items()):
                    if v == cache_name:
                        _GEMINI_CACHE_REGISTRY.pop(k, None)
                response = _call(use_cache=False, use_schema=use_schema)
            # Schema rejected by Gemini's parser → disable for the rest of
            # the process and retry once without responseSchema. The model
            # still emits JSON via responseMimeType; the prompt's output
            # section describes the shape.
            elif use_schema and (
                "response_schema" in msg.lower()
                or "responseschema" in msg.lower()
            ):
                _GEMINI_SCHEMA_DISABLED = True
                logger.warning(
                    "Gemini rejected response_schema; disabling structured "
                    "output for the rest of the process. First line: %s",
                    msg.splitlines()[0] if msg else "?",
                )
                response = _call(use_cache=True, use_schema=False)
            else:
                raise

        raw = getattr(response, "text", None) or "{}"
        data = json.loads(raw)
        self._last_raw_data = data
        usage_info = log_usage_genai(
            response, self._model, stage="MergedExtraction", logger=logger
        )
        dump_call(
            stage="MergedExtraction",
            model=self._model,
            system=system_message,
            user=user_prompt,
            raw_response=raw,
            usage=usage_info,
        )
        return self._unpack_merged_response(data)

    def _extract_from_raw_openai(
        self, system_message: str, user_prompt: str,
    ) -> list[dict]:
        """OpenAI-compat transport (fallback / non-Gemini models)."""
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)
        self._last_raw_data = data
        usage_info = log_usage(
            response, self._model, stage="MergedExtraction", logger=logger
        )
        dump_call(
            stage="MergedExtraction",
            model=self._model,
            system=system_message,
            user=user_prompt,
            raw_response=raw,
            usage=usage_info,
        )
        return self._unpack_merged_response(data)

    @staticmethod
    def _unpack_merged_response(data: dict) -> list[dict]:
        """Pull tasks out of the merged-call JSON.

        Re-derives ``assignee`` (from ia+ea — plan A2) and ``confidence``
        (from commitment_type — plan A3) so downstream consumers
        (classifier, writer) see the same dict shape they always have.
        """
        tasks = data.get("tasks", []) or []
        # Scratch fields (domain_corrections, speaker_resolutions) are no
        # longer emitted (plan A4). data.get(...) returns [] silently if
        # an older payload still includes them — log them when present so
        # we can spot regressions during the rollout.
        corrections = data.get("domain_corrections", []) or []
        resolutions = data.get("speaker_resolutions", []) or []
        if corrections:
            logger.debug("domain_corrections (legacy): %s", json.dumps(corrections, ensure_ascii=False))
        if resolutions:
            logger.debug("speaker_resolutions (legacy): %s", json.dumps(resolutions, ensure_ascii=False))

        unpacked: list[dict] = []
        for task in tasks:
            mapped = {_TASK_FIELD_MAP.get(k, k): v for k, v in task.items()}
            ia = mapped.get("internal_assignees") or []
            ea = mapped.get("external_assignees") or []
            mapped["assignee"] = ", ".join([*ia, *ea]) or "Team"
            ct = (mapped.get("commitment_type") or "").lower()
            mapped["confidence"] = _CT_TO_CONFIDENCE.get(ct, "medium")
            unpacked.append(mapped)
        return unpacked

    def extract_from_raw(
        self,
        transcript: str,
        attendees: list[dict[str, str]],
        system_prompt: str,
        org_chart: str = "",
        terminology: str = "",
        meeting_title: str = "",
        meeting_date: str = "",
        enriched_attendee_str: str = "",
        notes_text: str = "",
    ) -> list[dict]:
        """Merged correction + extraction in a single LLM call.

        The model does domain-term correction and speaker resolution
        inline and reports them as scratch fields (``domain_corrections``,
        ``speaker_resolutions``) alongside the extracted tasks. The full
        corrected transcript is NOT emitted.

        For ``gemini-*`` models the call goes through the native
        ``google-genai`` SDK with explicit context caching of the system
        prefix and ``response_schema``-enforced output. Other models go
        through the OpenAI-compat JSON-object path.

        Returns a list of task dicts with ``commitment_type``,
        ``confidence``, ``priority``, etc. The scratch fields are logged
        but not returned (they are diagnostic only).
        """
        system_message, user_prompt = self._build_merged_messages(
            transcript=transcript,
            attendees=attendees,
            system_prompt=system_prompt,
            org_chart=org_chart,
            terminology=terminology,
            meeting_title=meeting_title,
            meeting_date=meeting_date,
            enriched_attendee_str=enriched_attendee_str,
            notes_text=notes_text,
        )

        logger.debug(
            "Merged-extracting tasks from raw transcript with %s (%d chars, %d attendees)",
            self._model, len(transcript), len(attendees),
        )

        if self._is_gemini:
            return self._extract_from_raw_gemini(system_message, user_prompt)
        return self._extract_from_raw_openai(system_message, user_prompt)
