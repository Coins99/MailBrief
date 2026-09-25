> Archived on 2026-09-25. This is the original Microsoft-first MVP plan, kept as the
> reference for the dormant Outlook provider. The current plan is `docs/mvp-plan.md`.

# MailBrief MVP Implementation Plan

## 1. Objective

Build a Windows-first desktop MVP that lets a user connect one Microsoft
account, retrieve the current day's Inbox messages, rank them locally, analyze
only the most relevant messages with a cloud AI provider, and display a daily
brief with links back to the original messages.

The roadmap is ordered by milestones and tasks. Task numbers express dependency
order, not a promise that every task has equal effort.

## 2. MVP Scope

### Included

- Python 3.13 desktop application.
- One Microsoft 365, Outlook, or personal Microsoft account.
- Manual **Generate today's brief** workflow.
- Inbox messages received during the user's local calendar day.
- Local SQLite metadata, ranking, analysis, and digest cache.
- Deterministic local ranking before any email body is sent to AI.
- One cloud AI implementation behind the `AIProvider` interface.
- Highlights, action items, decisions, and deadlines.
- Open the source message using its Microsoft Graph `webLink`.
- Windows `onedir` distribution produced with PyInstaller.

### Deferred

- Gmail, IMAP, and multiple accounts.
- Local AI models.
- Background synchronization and scheduled briefs.
- Attachments and attachment analysis.
- Configurable ranking rules.
- Outlook add-in support.
- Signed installer and automatic updates.

## 3. Required Software

Install the following machine-level tools:

| Tool | Purpose |
| --- | --- |
| Git | Source control |
| `uv` | Python installation, virtual environment, dependency management, and locking |
| Python 3.13 x64 | Application runtime |
| Microsoft Entra account | Microsoft desktop app registration |
| OpenAI API key | Cloud analysis during the MVP |

Pin the project to Python `>=3.13,<3.14`. The selected `qasync` release supports
Python 3.13 but not Python 3.14. A separate Qt installation is not required;
PySide6 wheels include the necessary Qt binaries.

## 4. Python Packages

### 4.1 Runtime dependencies

These are direct project dependencies and must be declared in `pyproject.toml`.
Exact resolved direct and transitive versions must be recorded in `uv.lock`.

| Package | Version constraint | Purpose |
| --- | --- | --- |
| `PySide6` | `>=6.11.2,<6.12` | Qt desktop UI |
| `qasync` | `>=0.28,<0.29` | Integrate `asyncio` with the Qt event loop |
| `httpx` | `>=0.28.1,<0.29` | Async Microsoft Graph HTTP client |
| `pydantic` | `>=2.13.5,<3` | Domain models and AI response validation |
| `pydantic-settings` | `>=2.15,<3` | Validated nonsecret configuration |
| `SQLAlchemy[asyncio]` | `>=2.0.52,<2.1` | Database models and async repositories |
| `aiosqlite` | `>=0.22.1,<1` | Async SQLite driver |
| `alembic` | `>=1.19.1,<2` | Database schema migrations |
| `msal` | `>=1.38,<2` | Microsoft interactive OAuth |
| `msal-extensions[portalocker]` | `>=1.3.1,<2` | Encrypted, locked MSAL token-cache persistence |
| `keyring` | `>=25.7,<26` | Store the AI API key in the operating-system credential store |
| `openai` | `>=3.6,<4` | Async Responses API and structured AI output |

Install the runtime dependencies with:

```powershell
uv init --package --python 3.13
uv add "PySide6>=6.11.2,<6.12" "qasync>=0.28,<0.29" "httpx>=0.28.1,<0.29" "pydantic>=2.13.5,<3" "pydantic-settings>=2.15,<3" "SQLAlchemy[asyncio]>=2.0.52,<2.1" "aiosqlite>=0.22.1,<1" "alembic>=1.19.1,<2" "msal>=1.38,<2" "msal-extensions[portalocker]>=1.3.1,<2" "keyring>=25.7,<26" "openai>=3.6,<4"
```

### 4.2 Development and release dependencies

| Package | Purpose |
| --- | --- |
| `pytest` | Test runner |
| `pytest-asyncio` | Async service tests |
| `pytest-qt` | PySide6 widget and signal tests |
| `pytest-cov` | Coverage reporting |
| `respx` | Mock Microsoft Graph `httpx` requests |
| `time-machine` | Deterministic date, time-zone, and DST tests |
| `ruff` | Formatting and linting |
| `mypy` | Static type checking |
| `pyinstaller` | Windows application distribution |

Install the development dependencies with:

```powershell
uv add --dev pytest pytest-asyncio pytest-qt pytest-cov respx time-machine ruff mypy pyinstaller
```

### 4.3 Packages intentionally excluded

Do not add the following to the MVP unless the scope changes:

- Microsoft Graph SDK; call the Graph REST API directly with `httpx`.
- Gmail or IMAP libraries.
- Local-model libraries.
- HTML parsers; request shortlisted Graph bodies as plain text.
- Scheduler libraries.
- Dependency-injection frameworks.
- `python-dotenv`; store the AI key in the operating-system credential store.
- Generic retry libraries; Graph retries must explicitly honor `Retry-After`.

## 5. Target Repository Structure

```text
MailBrief/
|-- pyproject.toml
|-- uv.lock
|-- .python-version
|-- README.md
|-- docs/
|   |-- architecture.puml
|   `-- mvp-plan.md
|-- migrations/
|   |-- env.py
|   `-- versions/
|-- packaging/
|   `-- mailbrief.spec
|-- src/
|   `-- mailbrief/
|       |-- __init__.py
|       |-- __main__.py
|       |-- app.py
|       |-- config.py
|       |-- domain/
|       |   |-- messages.py
|       |   |-- analysis.py
|       |   `-- digests.py
|       |-- ports/
|       |   |-- email_provider.py
|       |   `-- ai_provider.py
|       |-- providers/
|       |   |-- microsoft/
|       |   |   |-- auth.py
|       |   |   |-- graph_client.py
|       |   |   `-- mapper.py
|       |   `-- openai/
|       |       `-- provider.py
|       |-- services/
|       |   |-- application.py
|       |   |-- sync.py
|       |   |-- ranking.py
|       |   `-- digest.py
|       |-- storage/
|       |   |-- database.py
|       |   |-- tables.py
|       |   `-- repositories.py
|       `-- ui/
|           |-- main_window.py
|           |-- connection_view.py
|           |-- progress_view.py
|           `-- digest_view.py
`-- tests/
    |-- unit/
    |-- integration/
    |-- ui/
    `-- fixtures/
```

## 6. Domain Contracts

Create the following Pydantic models before provider or UI implementation:

- `NormalizedMessage`
- `RankedMessage`
- `MessageAnalysis`
- `DigestItem`
- `DailyDigest`
- `SyncProgress`
- `SyncResult`

The `EmailProvider` contract must expose operations equivalent to:

- Connect or restore an account session.
- Return the connected account identity.
- Fetch normalized message metadata for a UTC interval.
- Fetch a plain-text body for one message.
- Disconnect and delete provider credentials.

The `AIProvider` contract must accept a collection of shortlisted messages and
return validated `MessageAnalysis` objects without exposing provider-specific
response types to application services.

## 7. Database Design

### 7.1 `accounts`

- `id`
- `provider`
- `provider_account_id`
- `email_address`
- `display_name`
- `tenant_id`
- `created_at_utc`
- `last_sync_at_utc`

### 7.2 `messages`

- `id`
- `account_id`
- `provider_message_id`
- `internet_message_id`
- `conversation_id`
- `subject`
- `sender_name`
- `sender_address`
- `to_recipients_json`
- `received_at_utc`
- `is_read`
- `importance`
- `has_attachments`
- `body_preview`
- `web_link`
- `rank_score`
- `rank_reasons_json`
- `synced_at_utc`

Add a unique constraint on `(account_id, provider_message_id)`. Do not persist
full message bodies.

### 7.3 `analyses`

- `id`
- `message_id`
- `input_hash`
- `provider`
- `model`
- `prompt_version`
- `schema_version`
- `category`
- `summary`
- `action_required`
- `action_text`
- `deadline_text`
- `deadline_at_utc`
- `confidence`
- `analyzed_at_utc`

Add a unique constraint on
`(message_id, input_hash, model, prompt_version)`.

### 7.4 `digests`

- `id`
- `account_id`
- `local_date`
- `timezone_name`
- `status`
- `generated_at_utc`

Add a unique constraint on `(account_id, local_date)`.

### 7.5 `digest_items`

- `digest_id`
- `message_id`
- `analysis_id`
- `position`
- `section`

### 7.6 `sync_runs`

- `id`
- `account_id`
- `range_start_utc`
- `range_end_utc`
- `started_at_utc`
- `completed_at_utc`
- `page_count`
- `message_count`
- `status`
- `sanitized_error_code`

## 8. Microsoft Integration Specification

Register MailBrief as a public desktop client:

- Support organizational and personal Microsoft accounts.
- Register `http://localhost` as the desktop redirect URI.
- Request delegated `User.Read` and `Mail.Read` permissions.
- Do not create or distribute a client secret.
- Use `PublicClientApplication`.
- Attempt silent acquisition before interactive acquisition.
- Persist the token cache with `msal-extensions`.
- Never store access or refresh tokens in SQLite.

Fetch metadata from:

```text
GET /v1.0/me/mailFolders/inbox/messages
```

Use these query parameters:

```text
$select=id,internetMessageId,conversationId,subject,sender,toRecipients,
        receivedDateTime,isRead,importance,hasAttachments,bodyPreview,webLink
$filter=receivedDateTime ge {utc_start} and receivedDateTime lt {utc_end}
$orderby=receivedDateTime desc
$top=50
```

Follow the complete `@odata.nextLink` until it is absent or the operation is
cancelled. Validate that pagination URLs use the expected Microsoft Graph host.

Retrieve a body only after a message enters the shortlist:

```text
GET /v1.0/me/messages/{message_id}?$select=id,body
Prefer: outlook.body-content-type="text"
```

Never fetch attachments during the MVP.

Apply this error, timeout, and retry policy:

- Shared attempt budget: up to 3 retries maximum across all retryable categories.
- Wall-clock deadlines & budgets:
  * Per-HTTP request deadline: 45 seconds maximum. Minimum viable budget floor is 2.0 seconds; if remaining time is below 2.0s, raise `ProviderTimeoutError` immediately to avoid burning attempts on doomed calls.
  * Per-sync run-level budget: 180 seconds of active work, owned and monitored by `SyncService`. Provider-mandated sleeps are excluded from this 180s work budget.
- Provider-mandated rate-limit wait ceiling:
  * In pagination, single `Retry-After` waits up to 60 seconds are honored by pausing and retrying the current page.
  * If `Retry-After > 60s`, the provider raises `ProviderRateLimitError`, ending the run cleanly as throttled (`PARTIAL` if pages were committed, `FAILED` otherwise) with error code `RATE_LIMIT_EXCEEDED`, rather than hanging the desktop app.
- Per-attempt timeouts: Connection timeout 10 seconds, read timeout 30 seconds (each clamped to `min(limit, remaining_deadline)`).
- Host security & pagination: Continuation URLs (`@odata.nextLink`) must strictly match the configured `base_url` host (case-folded) over HTTPS, with no userinfo. Sync pagination is capped at 200 pages maximum to prevent circular loops.
- `401`: Silently renew once via `get_access_token(force_refresh=True)`, then raise `AuthenticationRequiredError`.
- `403`: Raise `ProviderPermissionError` carrying structured correlation IDs (`client_request_id`, `server_request_id`, and `provider_error_code`).
- `429`: Parse `Retry-After` (integer delta-seconds or RFC 2822 HTTP date). Clamped to the 45s request deadline; if sleeping exceeds deadline or attempts are exhausted, raise `ProviderRateLimitError` immediately.
- `503`: Inspect `Retry-After` header first; if present and within remaining deadline, honor it. If absent, fallback to transient delays (1, 2, and 4 seconds).
- `500`, `502`, `504`: Retry after 1, 2, and 4 seconds within the shared retry budget and deadline.
- Network transport errors (`httpx.TransportError`): Retries after 1, 2, and 4 seconds within the shared retry budget and request deadline.
- Other `4xx` responses: Do not retry. Raise `ProviderResponseError`.
- Progress callbacks: `on_retry` callback invocations are wrapped to swallow and log callback exceptions, ensuring UI reporting issues never derail the HTTP retry loop or mask provider errors.

## 9. Local Ranking Specification

Calculate the initial score from locally cached metadata:

| Condition | Score adjustment |
| --- | ---: |
| High importance | `+20` |
| Low importance | `-10` |
| Unread | `+8` |
| User is directly in `toRecipients` | `+8` |
| Has attachments | `+3` |
| Action or deadline terms in subject/preview | Up to `+20` |
| Approval, reply, or request terms | `+10` |
| Received within the last 3 hours | `+8` |
| Received within the last 8 hours | `+5` |
| Sender contains `no-reply` or `noreply` | `-12` |

Sort by score descending, received time descending, and provider message ID
ascending. Select up to ten messages with a score of at least 10. If messages
exist but fewer than three qualify, fill the shortlist with the next
highest-scoring messages until it contains three.

Persist both the score and readable reasons such as `unread`, `direct recipient`,
and `deadline language`.

> [!NOTE]
> Direct recipient matching tests case-folded membership across the user's known account addresses (`account_addresses` collected from `mail`, `userPrincipalName`, and session credentials) to ensure identical ranking between fresh sign-in and disk-restored sessions.
> **Known limitation**: Aliases and distribution lists that do not match these explicit addresses will miss the direct-recipient bonus without LDAP/GAL expansion.

## 10. AI Analysis Specification

Analyze shortlisted messages in batches of five. For each message, send only:

- A temporary request-local index.
- Subject.
- Sender display name and address.
- Received timestamp.
- Local ranking reasons.
- Plain-text body limited to 8,000 characters.

Never send:

- Microsoft access or refresh tokens.
- Tenant identifiers.
- Graph message identifiers.
- `webLink` values.
- Attachments.
- Unshortlisted messages.

Require this structured result for every analyzed message:

- `category`: `action`, `deadline`, `decision`, or `information`.
- `summary`: at most 240 characters.
- `action_required`: boolean.
- `action_text`: nullable string.
- `deadline_text`: nullable string.
- `deadline_at`: nullable ISO-8601 timestamp.
- `confidence`: number from 0 through 1.
- `evidence`: a short excerpt supporting the result.

Use the Responses API with JSON-schema Structured Outputs and `store=False`.
Validate the result with Pydantic. Reject deadlines without supporting input
text. Cache analyses using a SHA-256 hash of normalized input plus the provider,
model, prompt version, and schema version.

If one batch fails, preserve successful results and present a partial digest
with a visible failure notice.

## 11. Implementation Roadmap

### Milestone 1: Foundation and contracts

#### Task 1: Freeze the MVP scope

- Record included and deferred functionality.
- Define supported Windows and Python versions.
- Add architectural decision records for `uv`, direct Graph REST, OpenAI,
  SQLite, and Windows-first packaging.
- Define the release acceptance criteria from Section 12.

#### Task 2: Scaffold the Python project

- Create `pyproject.toml`, `uv.lock`, and `.python-version`.
- Create the `src/mailbrief` and `tests` layouts.
- Add the `mailbrief` application entry point.
- Configure Ruff, mypy, pytest, and coverage.
- Add CI checks for lockfile consistency, linting, typing, and tests.

#### Task 3: Implement domain models and provider ports

- Implement all Pydantic contracts from Section 6.
- Implement `EmailProvider` and `AIProvider` protocols.
- Add serialization, invalid-input, UTC timestamp, and enum tests.

#### Task 4: Implement the database foundation

- Create the SQLAlchemy async engine and session factory.
- Store the database under the Qt application-data directory.
- Implement every table and constraint from Section 7.
- Initialize Alembic and create the first migration.

#### Task 5: Implement repositories

- Add account, message, analysis, digest, and sync-run repositories.
- Implement idempotent message upserts.
- Test commits, rollbacks, unique constraints, empty startup, and reruns.

**Milestone 1 exit criteria:** The application starts, creates or migrates its
SQLite database, and passes all domain and repository tests.

### Milestone 2: Microsoft authentication and Graph access

#### Task 6: Create the Entra app registration

- Configure the supported account types.
- Configure the desktop redirect URI.
- Add delegated `User.Read` and `Mail.Read` permissions.
- Document the client ID and tenant configuration process.

#### Task 7: Implement Microsoft authentication

- Create the MSAL public-client adapter.
- Attempt silent authentication first.
- Fall back to interactive browser authentication.
- Convert MSAL errors into application-level error types.

#### Task 8: Implement secure token persistence and account lifecycle

- Store the MSAL cache through encrypted `msal-extensions` persistence.
- Implement account discovery through `/me`.
- Implement connect, reconnect, disconnect, and token deletion.
- Confirm that SQLite and logs never contain token values.

#### Task 9: Implement the Graph HTTP client

- Create a shared async `httpx.AsyncClient`.
- Add bearer authentication, timeouts, retry rules, request correlation IDs,
  cancellation, and sanitized logging.
- Validate response status and JSON shape before mapping data.

#### Task 10: Complete the first live Graph retrieval

- Fetch one real page of Inbox metadata.
- Map the response into `NormalizedMessage` objects.
- Display account identity and fetched message count in a temporary developer
  view or diagnostic command.

**Milestone 2 exit criteria:** A real Microsoft account can authenticate, fetch
one Inbox page, restart the app, and reconnect silently from the encrypted cache.

### Milestone 3: Synchronization and local ranking

#### Task 11: Implement calendar boundaries

- Read the user's current local time zone.
- Compute local midnight and the next local midnight.
- Convert both boundaries to UTC for Graph filtering.
- Test midnight, empty-day, daylight-saving transition, and UTC-offset cases.

#### Task 12: Implement complete Graph pagination

- Apply the metadata query from Section 8.
- Follow complete `@odata.nextLink` URLs.
- Emit page and message progress events.
- Check cancellation between page requests.
- Prevent pagination to an unexpected host.

#### Task 13: Implement transactional synchronization

- Create a `sync_runs` record before retrieval.
- Upsert each successfully validated page in a transaction.
- Record page and message totals.
- Mark the run successful, cancelled, partial, or failed.
- Verify that repeated runs create no duplicate messages.

#### Task 14: Implement the local ranker

- Implement every scoring rule from Section 9.
- Save scores and ranking reasons.
- Apply deterministic sorting and shortlist fallback behavior.
- Add table-driven tests for every rule and tie-breaking condition.

#### Task 15: Complete the metadata-to-shortlist vertical slice

- Connect authentication, synchronization, storage, and ranking through the
  application service.
- Produce a deterministic shortlist for a real test mailbox.
- Verify empty Inbox and cancellation behavior.

**Milestone 3 exit criteria:** MailBrief fetches all of the current day's Inbox
metadata, caches it without duplicates, and produces a deterministic shortlist.

### Milestone 4: AI analysis and digest generation

#### Task 16: Retrieve shortlisted message bodies

- Fetch bodies only after ranking.
- Request plain-text content.
- Limit each body to 8,000 characters for AI input.
- Keep bodies in memory only and prove they are absent from SQLite and logs.

#### Task 17: Define the AI contract and consent flow

- Implement the structured schema from Section 10.
- Assign explicit prompt and schema versions.
- Add the first-use cloud-processing disclosure and consent state.
- Create provider-contract tests using a fake AI provider.

#### Task 18: Implement the OpenAI provider

- Retrieve the API key from the operating-system credential store.
- Use `AsyncOpenAI` and the Responses API.
- Request Structured Outputs with `store=False`.
- Map authentication, rate-limit, timeout, validation, and server errors into
  application-level errors.

#### Task 19: Implement batching and analysis caching

- Analyze no more than five messages per request.
- Compute an input hash before each request.
- Reuse compatible cached results.
- Persist only validated structured analyses.
- Preserve successful batches if a later batch fails.

#### Task 20: Implement digest assembly

- Divide results into Highlights, Actions, Deadlines, and Decisions.
- Sort items deterministically.
- Persist the digest and item positions.
- Support complete, partial, and empty digests.

**Milestone 4 exit criteria:** A service-level workflow generates and caches a
validated daily digest without persisting full email bodies.

### Milestone 5: Desktop user interface

#### Task 21: Implement the application state machine

- Support `disconnected`, `idle`, `syncing`, `ranking`, `analyzing`, `success`,
  `partial`, `cancelled`, and `failed` states.
- Define legal state transitions.
- Ensure one generation operation can run at a time.

#### Task 22: Build first-run and connection views

- Add Microsoft connection controls.
- Add API-key entry backed by `keyring`.
- Add cloud-processing disclosure and explicit consent.
- Display the connected account without exposing tokens.

#### Task 23: Build progress and cancellation behavior

- Display synchronization page and message counts.
- Display ranking and AI batch progress.
- Add cancellation and disable conflicting controls.
- Ensure network and database work do not block Qt repainting.

#### Task 24: Build digest views

- Add Highlights, Actions, Deadlines, and Decisions sections.
- Render sender, subject, summary, ranking reasons, deadline, and confidence.
- Add empty, partial, cancelled, and error states.

#### Task 25: Implement source-message navigation and accessibility

- Open `webLink` through `QDesktopServices.openUrl`.
- Add keyboard navigation and visible focus states.
- Persist nonsecret window settings.
- Add UI tests for state changes and signals.

**Milestone 5 exit criteria:** A user can complete the full MVP workflow without
using a terminal, and the interface remains responsive throughout the process.

### Milestone 6: Automated verification and recovery

#### Task 26: Complete domain and service coverage

- Reach at least 90% coverage for ranking, synchronization, and digest services.
- Reach at least 80% overall coverage.
- Add regression tests for every corrected defect.

#### Task 27: Complete Microsoft Graph failure tests

- Test pagination, malformed pages, `401`, `403`, `429`, retryable `5xx`,
  nonretryable `4xx`, timeouts, and cancellation with `respx`.
- Verify `Retry-After` handling and retry limits.

#### Task 28: Complete AI failure tests

- Test invalid schemas, unsupported deadlines, missing results, authentication
  failures, rate limits, timeouts, server failures, partial batches, caching,
  and cancellation.

#### Task 29: Complete database and restart tests

- Test migration from an older schema.
- Test interrupted synchronization and analysis.
- Test application restart during incomplete work.
- Add a safe, actionable error for an unreadable or corrupted database.

#### Task 30: Run controlled end-to-end tests

- Use a dedicated Microsoft test mailbox with known messages.
- Verify date filtering, ranking order, AI categories, deadlines, cache reuse,
  restart behavior, and source links.
- Record failures without logging sensitive content.

**Milestone 6 exit criteria:** All automated checks pass, coverage targets are
met, and there are no known credential, data-loss, or migration defects.

### Milestone 7: Security, privacy, and packaging

#### Task 31: Audit data handling and logs

- Search logs and error paths for access tokens, refresh tokens, API keys, full
  bodies, Graph identifiers, tenant identifiers, and excessive personal data.
- Sanitize exception and diagnostics output.
- Verify that AI input is limited to the documented fields.

#### Task 32: Verify credential and account deletion

- Confirm that disconnect deletes the encrypted Microsoft token cache.
- Confirm that the AI key can be replaced and deleted.
- Define whether disconnect retains or deletes local message metadata.
- Make destructive deletion explicit and test it.

#### Task 33: Create the Windows package

- Add the PyInstaller specification.
- Include migrations, icons, and version metadata.
- Produce a Windows `onedir` artifact.
- Exclude development files and test fixtures.

#### Task 34: Test on a clean Windows environment

- Run on a clean Windows VM without Python or Qt installed.
- Test authentication callback behavior, database creation, credential storage,
  Graph access, AI access, source links, and uninstall cleanup documentation.

#### Task 35: Complete performance and accessibility review

- Measure startup time, synchronization time, memory use, and cancellation
  latency.
- Verify that no visible UI freeze exceeds 250 milliseconds.
- Test keyboard-only operation, focus order, scaling, and readable error text.

**Milestone 7 exit criteria:** The packaged application runs on a clean Windows
machine and passes the security, privacy, performance, and accessibility checks.

### Milestone 8: Release candidate and MVP release

#### Task 36: Resolve release-blocking defects

- Fix all critical and high-severity defects.
- Add a regression test for each fix.
- Re-run linting, typing, unit, integration, and UI suites.

#### Task 37: Execute the account and mailbox test matrix

- Test personal and organizational Microsoft accounts.
- Test consent denied, tenant policy restriction, expired session, empty Inbox,
  large Inbox, offline startup, and AI-provider failure.

#### Task 38: Produce and validate the release candidate

- Build from a clean checkout using the committed lockfile.
- Run the complete regression suite against the packaged artifact.
- Verify artifact version, included files, and repeatable checksums.

#### Task 39: Complete user and privacy documentation

- Update the README with installation and first-run instructions.
- Document Entra registration and required permissions.
- Document what is stored locally and what is sent to AI.
- Add troubleshooting, known limitations, and data-deletion instructions.

#### Task 40: Release version `0.1.0`

- Tag the tested commit.
- Publish the Windows artifact and checksum.
- Publish release notes and known limitations.
- Record deferred work as post-MVP issues.

**Milestone 8 exit criteria:** Version `0.1.0` is reproducible, documented,
packaged, tested, and ready for use by an MVP tester.

## 12. MVP Definition of Done

The MVP is complete only when all of the following are true:

- A user can connect one Microsoft account.
- Authentication persists securely across application restarts.
- All Inbox messages for the local calendar day are paginated and deduplicated.
- Local ranking is deterministic and produces readable reasons.
- Only shortlisted message bodies are sent to the AI provider.
- Full message bodies, access tokens, refresh tokens, and API keys are absent
  from SQLite and logs.
- Every AI result conforms to the validated schema.
- An unchanged message and prompt combination reuses cached analysis.
- The UI remains responsive and supports cancellation.
- Highlights open their original messages.
- Network failures produce understandable and recoverable states.
- The packaged application runs on a clean Windows machine.
- Ranking, synchronization, and digest services have at least 90% test coverage.
- Overall test coverage is at least 80%.
- The README, privacy disclosure, limitations, and troubleshooting guide are
  complete.

## 13. Effort Estimate and Checkpoints

Estimated effort for one experienced Python developer is 29 to 40 focused
developer-days, normally six to eight full-time calendar weeks. The roadmap is
nevertheless controlled by task and milestone completion rather than calendar
labels.

Expected checkpoints:

- First live Microsoft Graph retrieval: Task 10.
- Complete metadata-to-shortlist vertical slice: Task 15.
- Service-level daily digest: Task 20.
- Full desktop workflow: Task 25.
- Feature-complete and verified build: Task 30.
- Clean-machine Windows package: Task 35.
- MVP release: Task 40.

The primary schedule risks are Microsoft tenant consent, authentication cache
persistence, Graph throttling, malformed AI output, Qt event-loop integration,
and Windows packaging behavior.

## 14. Reference Documentation

- [Qt for Python: Getting Started](https://doc.qt.io/qtforpython-6/gettingstarted.html)
- [qasync project documentation](https://github.com/CabbageDevelopment/qasync)
- [uv locking and synchronization](https://docs.astral.sh/uv/concepts/projects/sync/)
- [MSAL Python token acquisition](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens)
- [MSAL Extensions](https://pypi.org/project/msal-extensions/)
- [Microsoft Graph list messages](https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0)
- [Microsoft Graph throttling guidance](https://learn.microsoft.com/en-us/graph/throttling)
- [SQLAlchemy SQLite and aiosqlite](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html)
- [OpenAI Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
- [PyInstaller documentation](https://pyinstaller.org/en/stable/index.html)
