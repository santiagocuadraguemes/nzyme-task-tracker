# Handover: plan the Meeting Mirrors carve-out (its own Lambda)

> **For a fresh session.** Your job is to produce a **detailed implementation plan**
> (via the planning skill) for extracting the **Meeting Mirrors** feature out of the
> `nzyme-task-tracker` monolith into its own Lambda — the next step in an ongoing
> "split the monolith into focused Lambdas" migration. **Plan only — do not build,
> edit code, deploy, or run the pipeline in the planning session.**

## 1. The big picture (why this exists)

The project is migrating from one Lambda-that-does-everything into focused programs.
Read **`docs/architecture-lambda-split.md`** first — it has the full target (6 programs)
and the rollout order. Short version:

- **Notion is the editing front-end.** Everything is mirrored into **Supabase (Neo
  project `yphbrpbwpakjduhmoimw`)**, which is the **read surface** for consumer Lambdas.
- Target programs: **Sync** (Notion→Supabase, done+deployed), **Fundraising** (done, see
  below), **Extraction** (still in monolith), **Meeting Mirrors** (still in monolith — THIS),
  **Housekeeping** (hierarchy/detail/external-org appliers + archive, still in monolith),
  **Webhook** (template injection, still in monolith).

Relevant memories (loaded via MEMORY.md): `lambda-split-architecture`,
`affinity-claim-table`, `supabase-sync`.

## 2. The reference implementation: Fundraising (already carved out)

Fundraising was the first consumer carved out and is the **template to follow**. It lives
in a **separate repo: `C:\Users\Santiago Cuadra\vscode_projects\nzyme-fundraising`**
(SAM stack `nzyme-fundraising`, company AWS account, eu-west-1, `rate(15 minutes)`).
Study it — especially `src/candidates.py` (readiness + candidate selection from the
mirror), `src/state.py` (claim-before-post table), `src/runner.py`, `src/config.py`
(env-driven config), `template.yaml`, `scripts/deploy.sh`.

The shared pattern for a consumer Lambda:
- reads candidates from the Supabase mirror (no Notion polling for *discovery*),
- owns a **Supabase claim table** (one row per meeting page) for idempotency; fail-closed,
- has an explicit **readiness rule** (don't rely on Notion `Processed` flags),
- deploys/monitors/fails independently.

## 3. CRITICAL feasibility finding — Meeting Mirrors is NOT a clean copy of Fundraising

Fundraising was **zero-Notion** (read Supabase → post to Affinity). **Meeting Mirrors is
fundamentally a Notion→Notion operation** and cannot be zero-Notion. Confirmed by reading
`src/topic_mirror/__init__.py` + `src/topic_mirror/writer.py`:

- The "literal full copy" uses **Notion's native page duplication**:
  `template: {type: 'template_id', template_id: <source_page_id>}`. Notion server-side
  copies the whole `meeting_notes` block (transcript, AI summary, attendees, notes). It is
  **not** reconstructed from text — so it needs the **source Notion page to exist** and be
  referenced by id. (The mirror DOES store that `page_id`, so the consumer has what it needs
  to call the clone.)
- The **merge path** (2nd/3rd contributor to the same meeting): their `## Notes` are
  appended INSIDE the existing mirror page under a `### <Name>'s Notes` heading — another
  Notion write/read.

**So the carve-out shape is "brains from Supabase, hands in Notion":** read Supabase to
*decide* what/whether to mirror; call Notion to *do* the clone/merge.

### What the consumer can DECIDE from Supabase (already available — verify columns)
- **Which meetings are tagged** → `meeting_transcripts.macro_work_block` / `detail` /
  `external_org` + the routing rules in `meeting_rule_rows` (action `Mirror to DB`,
  `target_db_id`).
- **Cross-DB dedup/merge discovery** (find every contributor's copy of the same meeting,
  keyed on `normalize(title) + date[:10]`) → query `meeting_transcripts` across all DBs.
- **Confidentiality gate** → `meeting_transcripts.confidential` ("Confidential"/"Shareable",
  blank=inherit) + `org_chart_rows.default_mirror_visibility` ("Private"/"Shared", NULL→"Shared").
- **Readiness** → `meeting_transcripts.last_edited_time` (see the trap below).
- **Owner / attendees** → `org_chart_rows`, `meeting_transcripts.attendee_emails`,
  `created_by_id`/`created_by_name`.

### What MUST stay on Notion (inherent)
- The clone itself (`template_id` duplication into the target topic DB) — needs source page id.
- The contributor note-merge (append into the mirror page's notes block).
- Possibly reading the source page's notes block for the merge (check whether the mirror's
  `notes` text is sufficient, or whether block-level fidelity is required — **open question**).

## 4. Key files to read in the monolith (the current implementation)

- `src/topic_mirror/__init__.py` — `mirror_to_topic_dbs` orchestrator (routing, confidentiality gate, per-route loop).
- `src/topic_mirror/writer.py` — `clone_or_merge` (the template_id clone + merge mechanics). **Read this closely.**
- `src/topic_mirror/route_registry.py` — `load_routes`/`match_routes`, the `Mirror to DB` action, dedup. NOTE: this also defines the Affinity actions now consumed by `config_mirror_sync` — don't propose changing it casually.
- `src/topic_mirror/confidentiality.py` — `read_confidential` / `mirror_allowed`.
- `src/topic_mirror/notes_extractor.py`, `src/topic_mirror/outcome.py`.
- Call site: `src/pipeline.py` → `run_sync_for_page` → `_run_topic_mirror` (how it's invoked today, what it's passed).
- `docs/meeting-mirrors.md` — the feature doc (template_id mechanism, cross-DB dedup, contributor merge, Owner resolution, confidentiality).

## 5. Supabase tables the plan will rely on (verify with the Supabase MCP, project `yphbrpbwpakjduhmoimw`)

- `public.meeting_transcripts` — full per-meeting replica. Columns include `page_id` (PK,
  uuid), `db_id`, `owner_name`, `title`, `macro_work_block`, `detail`, `external_org`,
  `confidential`, `created_by_id/_name`, `meeting_start`, `meeting_end`, `last_edited_time`,
  `attendee_emails`, `notes`, `notion_summary`, `transcript`, `task_page_ids`.
- `public.org_chart_rows` — member config incl. `default_mirror_visibility`, `meeting_notes_db_id`, `active`.
- `public.meeting_rule_rows` — routing rules incl. `action`, `match_property`, `match_value`, `target_db_id`, `active`.
- `public.affinity_meeting_posts` — the fundraising **claim table** (study its shape as the model for a new mirror-claim table).

## 6. HARD-WON LESSONS from the fundraising carve-out (do not repeat these)

1. **`meeting_end` is ALWAYS NULL in the mirror** (0/275 rows — meetings only have
   `meeting_start` + `last_edited_time`). Readiness MUST be **page-quiet** (page untouched for
   N minutes via `last_edited_time`), NOT "meeting ended X ago." This bug shipped to prod in
   fundraising and posted nothing until fixed.
2. **Deploy bug A:** unquoted `.env` values with spaces/`&` break `source .env` in deploy.sh —
   quote them.
3. **Deploy bug B:** SAM `--parameter-overrides Key=Value` splits values on spaces (truncated
   "Investor Relations & Fundraising" → "Investor"). The fundraising `deploy.sh` `add_param`
   was fixed to inner-quote: `PARAMS+=("${key}=\"${value}\"")`. Copy that.
4. **Cutover approach that worked:** deploy first in a no-write mode (the fundraising Lambda
   has `DRY_RUN` / `SHADOW`), watch CloudWatch for a clean tick on real data, *then* flip to
   live. Plan an equivalent dry-run/shadow mode for Mirrors (e.g. "decide and log, don't
   write to Notion").
5. **Claim table prevents double-acting.** A first-ever-run consumer with no validation data
   is risky; the claim table + a no-write mode are how you de-risk without a long parallel period.

## 7. Open questions the PLAN must resolve

- **Readiness rule** for Mirrors (page-quiet minutes? interaction with the cross-DB merge —
  a 2nd contributor can arrive much later; how long does the "merge window" stay open?).
- **Idempotency/claim + dedup model:** Mirrors already dedups cross-DB on title+date and
  tracks primary-vs-merge. Design the Supabase claim/state table: what's keyed (the mirror
  page? the source page? the dedup key?), how "primary cloned / contributor merged" is
  recorded, how re-runs are safe, how a late contributor triggers a merge not a re-clone.
- **What stays a Notion call vs Supabase read** — enumerate precisely. Confirm whether the
  merge needs the source notes *block* (Notion) or whether `meeting_transcripts.notes` text
  is sufficient.
- **Confidentiality gate** sourced from Supabase (`confidential` + `default_mirror_visibility`)
  rather than live Notion reads.
- **Routing** from `meeting_rule_rows` (the `Mirror to DB` rules + `target_db_id`).
- **Owner / attendee resolution** for the mirror page's `Owner` people property and the
  `### <Name>'s Notes` headings.
- **Cutover plan** (no long parallel period; dry-run/shadow first; how to disable the
  in-monolith `_run_topic_mirror` — likely a `TOPIC_MIRROR_ENABLED=false` flag + redeploy —
  and the cleanup that follows, mirroring how fundraising was retired).
- **Repo/stack shape:** new standalone repo (like `nzyme-fundraising`) vs a module; SAM stack
  name; schedule cadence; env config; shared `../venv`.

## 8. Constraints / project rules (apply to the eventual implementation; note them in the plan)

- Santiago runs on **Windows + PowerShell**; the Bash tool is available; venv is at `../venv/`.
- **Never run the pipeline** (`python -m src.main`, the consumer CLIs) — that's Santiago's;
  give commands. **Never deploy via blind apply** — production deploys go through the
  CloudFormation **changeset review** gate (build with `--no-execute-changeset`, review, then
  `aws cloudformation execute-change-set`); the `--no-confirm-changeset` blind-apply is blocked.
- **No @mentions** in any Notion content.
- **No silent failures** — let errors surface; the existing soft-fail-per-page-with-a-logged-
  outcome pattern (`MirrorOutcome`) is the intended exception, keep that shape.
- **Do NOT break** `config_mirror_sync` or the `route_registry` Affinity actions (they back the
  meeting-rules mirror), and **keep `include_inactive=True`** on the Supabase sync.
- Keep docs in sync (`docs/meeting-mirrors.md`, `docs/architecture-lambda-split.md`) when built.

## 9. Deliverable for the planning session

A concrete, reviewable implementation plan covering: the consumer's shape (read-Supabase-
decide / call-Notion-act), the readiness rule, the claim/dedup state table (with SQL), the
exact Notion-vs-Supabase boundary, the dry-run/shadow + changeset-gated cutover sequence, the
repo/stack/config, the test strategy, and the monolith retirement + cleanup steps. **Produce
the plan only — no code changes, no deploys, no pipeline runs.**
