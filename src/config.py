"""Configuration for the Nzyme AI-driven task extraction engine."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class SyncConfig(BaseModel):
    """Validated runtime configuration."""

    notion_api_token: str = Field(..., description="Notion integration token")
    # Light calls (classifier, fundraising summary, embeddings) → OpenAI
    openai_api_key: str = Field(..., description="OpenAI API key — used for light calls (classifier, fundraising summary, embeddings)")
    openai_model: str = Field("gpt-5-mini", description="OpenAI model for light calls (classifier + fundraising summary)")
    openai_base_url: str | None = Field(None, description="Deprecated; unused by pipeline routing. Retained for backward compat.")
    # Heavy calls (transcript correction, task extraction) → Gemini via OpenAI-compatible endpoint
    gemini_api_key: str | None = Field(None, description="Google Gemini API key — used for heavy calls (transcript correction, task extraction)")
    gemini_model: str = Field("gemini-3-flash-preview", description="Gemini model for heavy calls")
    gemini_base_url: str = Field(
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        description="Gemini OpenAI-compatible base URL for heavy calls",
    )
    # OpenRouter (experimental — diagnostic --openrouter flag on --extract).
    # Lets us A/B the merged-extract call against any OpenRouter-hosted model
    # (DeepSeek, Qwen, etc.) without changing the production routing.
    openrouter_api_key: str | None = Field(
        None,
        description="OpenRouter API key — diagnostic only, enables --openrouter on transcript_pipeline.",
    )
    openrouter_base_url: str = Field(
        "https://openrouter.ai/api/v1",
        description="OpenRouter OpenAI-compatible base URL.",
    )
    # Per-stage model overrides (manual CLI runs / experiments).
    # When set, take precedence over openai_model / gemini_model for that
    # stage. Provider is inferred from the model name prefix:
    # `gemini-*` → Gemini key + base URL, anything else → OpenAI key + base URL.
    extraction_model: str | None = Field(None, description="Override model for task extraction stage")
    classification_model: str | None = Field(None, description="Override model for task classification stage")
    # CLI override for the per-member `Auto-extract Tasks` Org Chart flag.
    # When None, the registry value applies (default True). When True/False,
    # forces every page in this run onto that path regardless of the Org
    # Chart. Used for debugging — not loaded from .env.
    auto_extract_tasks_override: bool | None = Field(
        None,
        description=(
            "CLI override for the Org Chart `Auto-extract Tasks` flag. "
            "When set, applies to every page processed in this run."
        ),
    )
    meeting_notes_db_id: str | None = Field(
        None,
        description=(
            "Single Meeting Notes DB ID — overrides Org Chart discovery when "
            "set. Useful for tests and dev runs against one DB. In production, "
            "leave unset and let `meeting_db_registry.discover_meeting_dbs` "
            "read the per-member URLs from the Org Chart."
        ),
    )
    team_tracker_db_id: str = Field(..., description="Team Task Tracker DB ID")
    task_archive_db_id: str | None = Field(
        None,
        description=(
            "Team Task Tracker — Archive DB ID. Destination for Done tasks "
            "swept by the weekly Sunday job. When unset, the weekly archive "
            "job is a no-op."
        ),
    )
    buffer_hours: int = Field(2, description="Hours to wait after meeting date")
    logfire_token: str | None = Field(None, description="Logfire write token for LLM observability")
    log_level: str = Field("INFO", description="Logging level")
    dry_run: bool = Field(False, description="Log tasks but don't write to Notion")
    include_ai_notes: bool = Field(False, description="Include AI-generated meeting notes in extraction")
    meeting_template_page_id: str | None = Field(None, description="Notion template page ID for meeting notes")
    inject_template: bool = Field(True, description="Whether to inject the meeting note template into new pages")
    watch_interval: int = Field(10, description="Seconds between template injection checks in watch mode")
    sync_interval: int = Field(300, description="Seconds between sync runs in watch mode")
    # Deal context (Investment Team)
    deal_workplans_db_id: str | None = Field(None, description="Deal Workplans DB ID (enables deal-aware extraction)")
    # Semantic dedup
    semantic_dedup_threshold: float = Field(0.80, description="Cosine similarity threshold for semantic dedup (0.0-1.0)")
    # Transcript pipeline
    terminology_db_id: str | None = Field(None, description="Terminology Dictionary DB ID (transcript correction)")
    org_chart_db_id: str | None = Field(None, description="Org Chart DB ID (transcript speaker identification)")
    classifier_prompt_page_id: str | None = Field(None, description="Notion page ID for transcript classifier prompt")
    merged_transcript_extraction_prompt_page_id: str = Field(
        ...,
        description=(
            "Notion page ID for the merged transcript-extraction system prompt. "
            "Required — loaded once per sync tick by the transcript path. The "
            "pipeline raises at startup if unset, and at ctx-load if the page "
            "is empty/inaccessible."
        ),
    )
    literal_notes_extraction_prompt_page_id: str | None = Field(
        None,
        description=(
            "Notion page ID for the literal-notes extraction prompt — used "
            "when an Org Chart row has `Auto-extract Tasks = false`. The "
            "prompt instructs the model to return one task per `## Action "
            "Items` bullet with the title kept verbatim."
        ),
    )
    # Webhook / Lambda mode
    webhook_path_token: str | None = Field(None, description="Secret URL token for webhook auth")
    idle_minutes: int = Field(3, description="Minutes of inactivity before AI extraction triggers")
    aws_region: str = Field("eu-west-1", description="AWS region for Lambda deployment")
    # Google Calendar (service account + Domain-Wide Delegation)
    google_service_account_file: str | None = Field(
        None,
        description="Local path to service-account.json (dev). Mutually exclusive with secret ARN.",
    )
    google_service_account_secret_arn: str | None = Field(
        None,
        description="AWS Secrets Manager ARN holding the service-account JSON (Lambda).",
    )
    gcal_delegated_user_default: str | None = Field(
        None,
        description=(
            "Default Workspace email for SA impersonation when the meeting creator "
            "can't be resolved (e.g., bot-created pages, ex-employees)."
        ),
    )
    gcal_proxy_delegated_user: str | None = Field(
        None,
        description=(
            "In-domain Workspace email to impersonate for meetings owned by an "
            "out-of-domain member (see gcal_proxy_domains). The proxy must have "
            "'see all event details' access to those members' calendars; the SA "
            "then reads the member's calendar by id via the proxy. Empty = behave "
            "as before (impersonate the owner directly)."
        ),
    )
    gcal_proxy_domains: frozenset[str] = Field(
        default_factory=frozenset,
        description=(
            "Email domains that domain-wide delegation can't impersonate directly "
            "(e.g. {'nzalpha.com'}). Meetings owned by these domains are read via "
            "gcal_proxy_delegated_user. Lower-cased."
        ),
    )

    @property
    def gcal_enabled(self) -> bool:
        """GCal lookup is active when a credential source AND a default user are configured."""
        has_creds = bool(self.google_service_account_file or self.google_service_account_secret_arn)
        return has_creds and bool(self.gcal_delegated_user_default)
    # Fundraising → Affinity branch (optional; opt-in via FUNDRAISING_BRANCH_ENABLED)
    fundraising_branch_enabled: bool = Field(
        False,
        description="Enable Affinity sync for meetings tagged 'Meeting type = Fundraising'",
    )
    affinity_api_key: str | None = Field(
        None, description="Affinity API key (HTTP basic auth, empty username)",
    )
    affinity_lp_funnel_list_id: int = Field(
        168609, description="Affinity list ID for the Nzyme - LP Funnel list",
    )
    # Meeting Mirrors feature (opt-in via TOPIC_MIRROR_ENABLED).
    # Clones tagged meetings (Macro Work Block / Detail / External Org) into
    # topic-specific Notion DBs. Routing rules live in the Meeting Rules
    # DB so joiners/leavers/new topics don't require a redeploy.
    topic_mirror_enabled: bool = Field(
        False,
        description="Enable cloning of tagged meetings into Topic Mirror DBs",
    )
    meeting_rules_db_id: str | None = Field(
        None,
        description=(
            "Notion DB ID for the Meeting Rules registry (was: Topic Mirror "
            "Routes). Each row maps a tag (Match Property + Match Value) to "
            "an Action: 'Mirror to DB' (clone the meeting into a target DB) "
            "or 'Fire Affinity LP Funnel (no transcript)' / '... (with "
            "transcript)' (drive the Fundraising branch, optionally appending "
            "the raw meeting transcript to the Affinity note). "
            "Required when topic_mirror_enabled is True or "
            "fundraising_branch_enabled is True."
        ),
    )
    # Hierarchy DB (source of truth for the Macro Work Block taxonomy).
    # Drives the daily Hierarchy sync (Tier 0 → member DB `Macro Work Block`
    # options).
    hierarchy_db_id: str | None = Field(
        None,
        description=(
            "Notion DB ID for the 'Meeting Notes & Task Tracker Hierarchy' "
            "database. Source of truth for Tier 0 Macro Work Blocks (sync'd "
            "into every member Meeting Notes DB's `Macro Work Block` select)."
        ),
    )
    detail_options_db_id: str | None = Field(
        None,
        description=(
            "Notion DB ID for the 'Detail Options' Settings DB. Source of "
            "truth for member-DB `Detail` multi-select options (sync'd by "
            "detail_canonical_mirror_sync → detail_rows → detail_applier_sync)."
        ),
    )
    external_orgs_db_id: str | None = Field(
        None,
        description=(
            "DEPRECATED 2026-06-02 — dead config, read by nothing. Was the "
            "'🏢 External Orgs' Settings DB mirror target; replaced by "
            "deal_hierarchy_sync (Hierarchy DB rows) + external_org_applier_sync "
            "(member-DB External Org fan-out). Retained only to avoid breaking "
            "existing .env / SAM wiring; safe to remove in a future cleanup."
        ),
    )


def load_config() -> SyncConfig:
    """Build a validated SyncConfig from environment variables."""
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    return SyncConfig(
        notion_api_token=os.environ["NOTION_API_TOKEN"],
        openai_api_key=os.environ["OPENAI_API_KEY"],
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        openai_base_url=os.getenv("OPENAI_BASE_URL"),  # deprecated; unused
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3-flash-preview"),
        gemini_base_url=os.getenv(
            "GEMINI_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
        ),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
        openrouter_base_url=os.getenv(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1",
        ),
        extraction_model=os.getenv("EXTRACTION_MODEL") or None,
        classification_model=os.getenv("CLASSIFICATION_MODEL") or None,
        meeting_notes_db_id=os.getenv("MEETING_NOTES_DB_ID") or None,
        team_tracker_db_id=os.environ["TEAM_TRACKER_DB_ID"],
        task_archive_db_id=os.getenv("TASK_ARCHIVE_DB_ID") or None,
        logfire_token=os.getenv("LOGFIRE_TOKEN"),
        buffer_hours=int(os.getenv("BUFFER_HOURS", "2")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        dry_run=os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes"),
        include_ai_notes=os.getenv("INCLUDE_AI_NOTES", "false").lower() in ("true", "1", "yes"),
        meeting_template_page_id=os.getenv("MEETING_TEMPLATE_PAGE_ID"),
        inject_template=os.getenv("INJECT_TEMPLATE", "true").lower() in ("true", "1", "yes"),
        deal_workplans_db_id=os.getenv("DEAL_WORKPLANS_DB_ID"),
        semantic_dedup_threshold=float(os.getenv("SEMANTIC_DEDUP_THRESHOLD", "0.80")),
        terminology_db_id=os.getenv("TERMINOLOGY_DB_ID"),
        org_chart_db_id=os.getenv("ORG_CHART_DB_ID"),
        classifier_prompt_page_id=os.getenv("CLASSIFIER_PROMPT_PAGE_ID"),
        merged_transcript_extraction_prompt_page_id=os.environ[
            "MERGED_TRANSCRIPT_EXTRACTION_PROMPT_PAGE_ID"
        ],
        literal_notes_extraction_prompt_page_id=os.getenv(
            "LITERAL_NOTES_EXTRACTION_PROMPT_PAGE_ID",
        ),
        watch_interval=int(os.getenv("WATCH_INTERVAL", "10")),
        sync_interval=int(os.getenv("SYNC_INTERVAL", "300")),
        webhook_path_token=os.getenv("WEBHOOK_PATH_TOKEN"),
        idle_minutes=int(os.getenv("IDLE_MINUTES", "3")),
        aws_region=os.getenv("AWS_REGION", "eu-west-1"),
        google_service_account_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"),
        google_service_account_secret_arn=os.getenv("GOOGLE_SERVICE_ACCOUNT_SECRET_ARN"),
        gcal_delegated_user_default=os.getenv("GCAL_DELEGATED_USER_DEFAULT"),
        gcal_proxy_delegated_user=os.getenv("GCAL_PROXY_DELEGATED_USER"),
        gcal_proxy_domains=frozenset(
            d.strip().lower()
            for d in os.getenv("GCAL_PROXY_DOMAINS", "").split(",")
            if d.strip()
        ),
        fundraising_branch_enabled=os.getenv("FUNDRAISING_BRANCH_ENABLED", "false").lower()
        in ("true", "1", "yes"),
        affinity_api_key=os.getenv("AFFINITY_API_KEY"),
        affinity_lp_funnel_list_id=int(os.getenv("AFFINITY_LP_FUNNEL_LIST_ID", "168609")),
        topic_mirror_enabled=os.getenv("TOPIC_MIRROR_ENABLED", "false").lower()
        in ("true", "1", "yes"),
        # Prefer the new env var; fall back to the old name for one deploy
        # cycle. Drop TOPIC_MIRROR_ROUTES_DB_ID once Lambda + .env are
        # both updated.
        meeting_rules_db_id=(
            os.getenv("MEETING_RULES_DB_ID")
            or os.getenv("TOPIC_MIRROR_ROUTES_DB_ID")
            or None
        ),
        hierarchy_db_id=os.getenv("HIERARCHY_DB_ID") or None,
        detail_options_db_id=os.getenv("DETAIL_OPTIONS_DB_ID") or None,
        external_orgs_db_id=os.getenv("EXTERNAL_ORGS_DB_ID") or None,
    )
