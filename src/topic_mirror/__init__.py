"""Meeting Rules registry package.

The **Meeting Mirrors** feature (clone tagged meetings into topic-specific
Notion DBs, merge later contributors' notes) was carved out of this monolith
into the standalone ``nzyme-meeting-mirrors`` Lambda (Lambda-split migration,
2026-06-08). The orchestrator (``mirror_to_topic_dbs``), ``writer``,
``notes_extractor``, ``confidentiality``, and ``outcome`` modules were removed
when the in-monolith branch was retired.

What remains is :mod:`src.topic_mirror.route_registry`, which is **still used**
by ``src.config_mirror_sync`` (to mirror the Meeting Rules DB → Supabase) and
defines the Affinity LP-funnel action constants consumed by the fundraising
path. Do not delete it.
"""
