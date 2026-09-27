# Desktop email implementation plan

Updated: 2026-09-25. Status: proposed work, not implemented features.

Build for one person and one Gmail account, on Windows and macOS. Complete the desktop
product before beginning a server-hosted website. The email component should
answer: What changed? What needs my attention? What should I do, by when, and
what could I write in response?

Example: an email requests a proposal by Friday. MailBrief summarizes it,
identifies the source deadline, proposes preparation steps and a Thursday target,
and generates an editable reply. An accepted action remains visible tomorrow.
A later calendar can schedule work without changing the deadline.

## Release boundaries

- **Email MVP (M0–M5):** connect → sync → review shortlist → analyze → saved daily
  brief → open source.
- **Complete personal email workspace (M6–M9):** persistent actions, suggested
  target dates, editable action plans, local email/note/message drafts, follow-up,
  history, preferences, backup, and reliable personal use.
- **Optional M10:** save reviewed drafts into Gmail. Local drafting and copy/export
  satisfy the committed drafting requirement; direct sending is deferred.

Full calendar, general project management, analytics, and the eventual website
are rough designs in [ecosystem-roadmap.md](ecosystem-roadmap.md).

## Verified starting point

Repository inspection on 2026-09-25 found:

| Area | Present | Still needed |
| --- | --- | --- |
| Provider | Gmail identity/default, neutral protocol, retained Microsoft adapter | Gmail package is a placeholder |
| Storage | SQLite, migrations, account/message/analysis/digest/sync repositories | Actions, drafts, preferences, follow-up records |
| Pipeline | Local-day boundaries, metadata sync, deterministic ranking, shortlist service | Gmail adapter, AI and digest orchestration |
| Analysis | Request/result models and AI protocol | Concrete adapter, consent, validation and cache integration |
| UI | PySide6 shell | Connected workflow, editor, settings and error states |
| Calendar | Day-boundary utilities | No event calendar or scheduler exists |

Tests were not rerun for this documentation change. Verify the actual baseline
at M0. Preserve Microsoft source, tests, dependencies, diagnostics, configuration,
cache paths and stored provider identities.

## Architecture and data rules

Retain Python, PySide6, SQLite and existing provider-neutral ports. Keep provider
JSON/SDK types inside adapters, business rules in services, and persistence in
repositories. No server, HTTP layer, or UI rewrite is needed for this phase.

| Data | Ownership and retention |
| --- | --- |
| Mail metadata | Account-scoped local cache; mailbox remains authoritative |
| Full incoming body | In memory for the operation; never SQLite, logs or temporary files |
| Preview, evidence, summary | Bounded derived content; sensitive and subject to retention controls |
| AI suggestion | Versioned candidate with source links; not yet a commitment |
| Accepted action and steps | User-owned, editable, durable independently of source mail |
| Draft/note | Intentionally saved user artifact; never a disguised incoming-body cache |
| Credentials | OS credential store; excluded from database and exports |
| Preferences | Validated local settings; distinguish device settings from portable preferences |

Before saving drafts, clarify ADR 0005 and workspace guidance: generated or
user-authored draft text may be persisted, while full incoming bodies may not.
Exclude quoted incoming history from autosaved drafts by default. Local storage
does not imply local AI processing or database encryption.

Give new user-owned records stable application IDs, creation/update timestamps
and revisions. Keep existing integer database keys where useful. Do not implement
cross-device sync or event sourcing merely to anticipate the website.

### Model changes required by the expanded vision

1. Current analysis supports one action per message. M6 must support multiple
   candidates and separate generated suggestions from accepted actions.
2. Distinguish extracted deadline, AI-suggested target, user-selected target and
   future scheduled work. Date-only deadlines must not become invented midnight
   timestamps. Preserve exact instants, timezone, unresolved phrases and evidence.
3. Current digest validation allows one item per message. Prefer one message card
   containing multiple suggestions; deliberately migrate if this changes. Do not
   silently violate the digest-item composite key.
4. Current configuration allows more body characters than `AnalysisRequest`.
   Align configuration, trimming and schema limits during M4.
5. Current analysis uniqueness does not include all version/context fields.
   Audit cache identity before relying on schema-version invalidation.
6. Track current Inbox membership and later thread state; old cached metadata
   must not imply an archived/deleted message is still in the Inbox.

Provider identity changes alone need no migration. New persisted features need
additive Alembic migrations and upgrade tests; preserve the initial migration.

## M0 — Establish the baseline

**Work**

- Run README setup and quality checks. Resolve or explicitly record existing
  failures before attributing failures to Gmail work.
- Verify provider identity/default tests and retained Microsoft coverage.
- Add sanitized Gmail fixtures and nonsecret configuration examples. Use fake
  providers in ordinary tests; live credentials and paid AI are never CI inputs.
- Establish stable error codes for authentication, sync, analysis and cancellation.

**Files:** tests, configuration examples and setup documentation. Preserve
unrelated untracked `out/` artifacts.

**Exit:** reproducible environment, recorded baseline, no blocking unexplained
failures. This does not count as completed Gmail integration.

## M1 — Gmail authentication and diagnostic

**Work**

- Document Google Cloud setup: Gmail API, consent screen/test user, Desktop OAuth
  client, local client-file path and expected reauthorization behavior.
- Implement system-browser OAuth, PKCE, state validation and a loopback callback
  with timeout/cancellation and guaranteed listener cleanup.
- Request `gmail.readonly`; securely store refresh credentials in the OS store.
  Handle refresh, revocation and unavailable credential storage explicitly.
- Validate mailbox identity before associating credentials with cached records.
- Add diagnostic `fetch`, `--silent-only`, and `disconnect` modes. Silent mode
  must not open a browser. Log only redacted operational information.
- Distinguish local disconnect, Google-side revocation and local data deletion.

**Files:** `providers/gmail/{auth,cache,factory}.py`, `diagnostics/gmail.py`,
`docs/gmail-setup.md`, paths, CLI entry points and dependency/lockfile updates.

**Tests:** callback state mismatch, denied consent, timeout, cancellation, expired
and revoked tokens, refresh-token preservation, wrong account, OS-store failure,
repeated disconnect and absence of secrets in logs.

**Exit:** real personal account connects, restores after process restart without
interaction, disconnects and recovers after revoked authorization.

Google documents the [desktop OAuth flow](https://developers.google.com/identity/protocols/oauth2/native-app).
External OAuth apps in Testing generally get seven-day refresh tokens for Gmail
scopes; handle that as reauthentication, not corrupted data. See
[token expiration](https://developers.google.com/identity/protocols/oauth2#expiration).

## M2 — Metadata sync and shortlist

**Work**

- Build authenticated REST requests with timeouts, bounded concurrency, rate-limit
  handling, finite retry/backoff, cancellation and safe error translation.
- Enumerate Inbox IDs for the local day using epoch boundaries. Query broadly
  enough to avoid boundary exclusion, then enforce the exact half-open UTC range
  locally using Gmail's received timestamp.
- Follow all pages, retrieve metadata only, map headers/addresses/labels/thread
  IDs and account-safe source links, and handle messages disappearing mid-run.
- Feed existing repositories, ranking and shortlisting. Reconcile current-window
  Inbox membership after successful enumeration; a partial run must not mark
  unseen messages absent.
- Expose retrieved/selected/failed counts and last successful refresh. Support
  user inclusion/exclusion before body retrieval and cloud analysis.

**Files:** `providers/gmail/{client,mapper,provider,factory}.py`, necessary minimal
sync/storage extensions, matching unit/integration fixtures.

**Tests:** multiple pages, duplicates, DST, exact boundaries, malformed headers,
429/transient errors, cancellation, archive between refreshes, account isolation
and zero full-body fetches during metadata sync.

**Exit:** today's metadata reaches SQLite and ranking; repeated runs create no
duplicates, and incomplete coverage is visible.

## M3 — Selected bodies and minimization

**Work**

- Fetch full content only for a reviewed shortlist or explicit user request.
- Decode nested MIME, base64url and charsets; prefer plain text and sanitize HTML
  fallback without remote images, executable content or link fetching.
- Exclude file attachments. Gmail may store textual MIME-part content separately;
  distinguish those from actual file attachments using MIME/disposition and caps.
- Bound decoded size and analysis length, mark truncation and missing context,
  and reduce repetitive quoted history without silently losing relevant content.
- Scope body lifetime to the operation; release references after completion or
  cancellation. Do not place bodies in logs, exceptions or disk caches.

**Files:** Gmail MIME/body helpers, application body pipeline, fixtures.

**Tests:** multipart alternatives, HTML-only, malformed encoding, non-UTF text,
empty/oversized content, nested parts, missing messages and attachment exclusion.
Inspect persisted rows and captured logs for synthetic full-body markers.

**Exit:** selected text is usable and bounded, with no persisted full incoming body.

## M4 — Analysis, consent and digest assembly

**Work**

- Implement the AI adapter behind the existing port and accepted ADR 0010. Keep
  credentials in the OS store and model selection configurable.
- Before transmitting content, show cloud-processing consent with included
  fields, selected count and truncation. Allow narrowing selection or cancelling.
- Validate versioned structured results. Reject unknown/duplicate message keys;
  handle omissions, refusals, invalid responses and partial batches explicitly.
- Treat mail as untrusted content: it cannot authorize tools, settings changes,
  mail writes or transmission of other messages.
- Separate extracted deadlines from suggestions; resolve relative phrases using
  message time and timezone and retain ambiguity for user review.
- Cache valid results by account/message, minimized-input hash, provider/model,
  prompt/schema versions and relevant interpretation context. Add migrations
  where existing cache uniqueness cannot represent that identity.
- Assemble deterministic complete/partial/empty briefs with source and evidence.
  Total analysis failure is a failure, not an empty Inbox. Retain coverage data.
- Bound batches, retries and per-run usage. Record usage when available; avoid
  claiming dollar costs without known pricing.

**Files:** analysis models/port, `providers/groq/provider.py`, analysis service,
`services/digest.py`, repositories and version/cache migrations.

**Tests:** fake AI results, schema/context cache invalidation, unchanged-input
reuse, missing results, malicious message instructions, ambiguous dates, consent
refusal, cancellation and partial failure.

**Exit:** a reviewed selection produces a saved, source-linked brief. Unchanged
inputs reuse valid analysis; verifying input freshness can still require body
refetch because incoming bodies are not cached.

## M5 — Desktop workflow and first package

**Work**

- Compose real providers, repositories and services at startup; integrate async
  work with Qt and close resources on exit. Prevent overlapping generation runs.
- Implement connection/settings, shortlist review, progress/cancel, brief, source
  opening and retry. Keep connection state distinct from AI readiness.
- Distinguish offline, expired auth, missing key, empty mailbox, partial results
  and cancellation. Preserve the last good brief during a failed refresh.
- Show saved-brief date/age. Offline viewing supports saved metadata and results;
  new source-body retrieval and cloud generation require connectivity.
- Provide keyboard navigation, readable typography, scaling and restrained
  visual hierarchy. Verify Gmail links when several Google accounts are signed in.
- Produce a Windows `onedir` package and a macOS app bundle, each built on its own OS,
  with safe migrations and correct data paths.
  Test on a machine/profile without the development environment.

**Files:** `app.py`, UI modules, application composition and packaging config.

**Tests:** UI state transitions, cancellation responsiveness, resource cleanup,
last-good-result preservation, repeat launch and packaged real-account checks.

**Exit / email MVP:** connect → sync → review → analyze → display → open source
works from the package, and restart restores the session and saved brief.

## M6 — Persistent actions, target dates and plans

**Work**

- Add candidate suggestion, accepted action, source-link and ordered-step models.
  One message may yield many actions; an action may link to multiple messages.
- Support accept/edit/dismiss. Accepted actions include title, mine/waiting-for
  ownership, status, optional effort, deadline, target date, notes and evidence.
- Generate short editable plans and explain suggested dates. Without an actual
  availability model, suggest target dates rather than claiming free time slots.
- Add open/waiting/completed views, reopen and undo. Carry open actions across
  days independently of which messages arrived today.
- Persist acceptance/dismissal fingerprints. Regeneration must not duplicate
  accepted work, recreate rejected suggestions or overwrite manual edits.
- Material source changes create a proposed revision. Preserve accepted actions
  when their source is deleted or cache cleanup runs; show source unavailable.

**Files:** new `domain/actions.py`, action service/repository, migrations, review
and action-list UI, versioned analysis extension. No scheduling engine yet.

**Tests:** multiple actions, repeated acceptance, dismiss/regenerate, manual edits
followed by refresh, source removal, waiting/reopen, restart, date-only semantics
and independent step completion.

**Exit:** accept a request, edit its target/steps, restart and carry it into the
next day without regeneration changing those decisions.

## M7 — Local drafts, notes and messages

**Work**

- Add a distinct drafting operation/service. Support reply, new email, note and
  copyable message formats using selected context, instructions, tone and length.
- Show missing context. Do not invent recipients, attachments, commitments or
  sent status; surface placeholders for review.
- Provide subject/recipient fields for email, editor, autosave, recoverable
  revisions, regenerate-as-new-version, copy and text/Markdown export.
- Apply the clarified storage policy to saved artifacts; consent covers newly
  included source content. Do not silently quote/persist entire incoming threads.
- Preserve edits on shutdown and provider failure. Draft generation/copying does
  not complete a task and does not send a message.

**Files:** `domain/drafts.py`, draft service/repository, AI drafting operation,
editor UI, migrations and storage ADR clarification.

**Tests:** autosave/restart, failed regeneration, revision recovery, context
selection, placeholders, export, and absence of mail-write/send requests.

**Exit:** create/edit/recover/export an email draft, note and message without
modifying Gmail or losing action state.

## M8 — Thread continuity and daily operation

**Work**

- Add selected-date digest history and bounded catch-up for missed days. Show the
  coverage window; never imply a whole-mailbox audit.
- Track accepted-action threads beyond today's Inbox. Fetch bounded relevant
  reply context during review, including sent replies where needed; do not
  silently import the whole Sent folder.
- Suggest changes for new deadlines, cancellations or possible resolutions.
  A reply is not proof of completion; user edits remain authoritative.
- Add history-based metadata reconciliation with paging, deduplication and a
  transactionally persisted cursor. On expired history, rescan configured
  coverage and tracked threads, explicitly excluding older untracked mail.
- Persist timezone, exclusions, shortlist size, tone, AI limits and refresh
  preferences. Apply exclusions before cloud processing.
- Offer on-launch generation and optional refresh while running, with explicit
  remembered consent controls. Coalesce missed timer runs after sleep; do not
  silently generate paid analysis for every missed day.
- Closed application means no scheduled jobs. Tray operation/OS scheduling can
  be later enhancements, not an implicit always-on promise.

**Files:** Gmail history support, sync checkpoints, follow-up service, history and
preferences UI. Extend shared ports only for genuinely provider-neutral needs.

**Tests:** late replies, changed deadlines, archive/delete/labels, expired cursor,
interrupted checkpoint, sleep/wake, timezone change, exclusion and deduplication.

**Exit:** a later reply proposes an update to an existing action without duplicate
work or lost edits; catch-up, offline and stale coverage are clear.
Google documents expired-cursor behavior in its
[synchronization guide](https://developers.google.com/workspace/gmail/api/guides/sync).

## M9 — Durability and complete personal email release

**Work**

- Implement consistent SQLite snapshot backup plus versioned metadata, excluding
  credentials. Validate restore compatibility and retain the old database until
  restoration succeeds. Reconnect credentials separately.
- Export actions, drafts and notes in documented portable formats. Add retention
  controls; distinguish disconnect, clear cache, delete account data and deletion
  of user-owned artifacts.
- Test upgrade of existing databases, disk-full/read-only failures and interrupted
  writes. Document recovery; never overwrite the only copy during restore.
- Harden packaging, redacted diagnostics, keyboard/high-DPI use, setup and help.
- Use the package for 7–14 days. Record missed important mail, wrong dates,
  duplicate actions, draft usefulness, latency and AI usage. Manually inspect a
  small labelled sample, including mail omitted by the shortlist.
- Resolve data loss, secret/full-body leakage, silent edit overwrites, unintended
  provider writes and duplicate acceptance before declaring completion. Review
  recurring misleading date/completion suggestions against the sample.

**Exit:** the release scenario below passes; real actions/drafts restore from
backup; personal use is reliable enough to move on to tasks/calendar.

## M10 — Optional Gmail draft saving

Implement only if local copy/export proves limiting. Keep compose capability
separate from the read-only provider workflow. Reauthorize with required scopes,
show the intended write, persist the remote draft ID, detect external edits and
reconcile uncertain request outcomes before retrying. Do not create duplicates
on timeout or blindly overwrite externally edited drafts. No sending is included.

`gmail.compose` includes sending as well as draft management; it is not a
permission limited to drafts. Installed apps also have authorization constraints;
do not assume web-style incremental authorization. See
[Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) and
[desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app).

**Exit:** explicitly save/update a reviewed test draft in the correct account,
including uncertain-outcome recovery, with no duplicate draft and no send call.

## Verification and delivery order

Dependency sequence: M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7 → M8 → M9.
Deliver each milestone in reviewable changes, then demonstrate its exit checks.
M10 is optional. Estimate the next milestone after the preceding live demo rather
than assigning unsupported calendar dates to the entire project.

Run focused tests for implementation changes, then README quality commands:

```powershell
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
```

Retain 80% overall and 90% service-coverage targets. Confirm CI enforces the
service-specific target; overall coverage alone does not enforce it. Live-mailbox
and paid-AI checks are explicit development checks, not ordinary CI tests.

### Complete-email acceptance scenario

1. Connect the real account, restart and restore silently.
2. Generate a multi-page daily brief; manually include an omitted message and
   verify displayed coverage and source links.
3. Accept two actions from one message, edit their dates/steps and dismiss another
   candidate. Regenerate and verify those choices persist.
4. Save a reply, note and message; restart and recover/export them.
5. Review a later reply changing a deadline without overwriting other edits;
   carry an unfinished action into tomorrow.
6. Exercise offline mode, cancellation, expired credentials and partial AI failure
   while preserving the last good brief and all user-owned work.
7. Back up, restore into a clean profile, reconnect and verify records.
8. Inspect database/logs for secrets and synthetic full incoming-body markers;
   inspect the distributable for accidental developer credentials.

**Next implementation change:** M0 verification and M1 Gmail authentication plus
live diagnostic. Calendar and website development remain outside this sequence.
