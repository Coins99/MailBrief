# MailBrief agent guide

This is the single source of truth for AI agents working in this repository. Read it
first, then only the files your task needs. If another document disagrees with this
file, this file wins. `docs/archive/` holds superseded plans and notes for reference only.

## Product and status

- Personal desktop app for Windows and macOS that turns today's important Gmail into a
  short brief. Both platforms are supported, from source or as native packages (M5).
- Gmail is the active provider. Microsoft (Outlook/Graph) code is dormant: keep it
  compiling and its tests passing, but do not extend it unless a task says so.
- Done: M1 Gmail OAuth and secure restore; M2 Inbox metadata sync, ranking and
  shortlist review (`mailbrief-gmail-diagnostic sync`).
- Implemented, live acceptance recorded in `docs/m3-validation.md`: M3 shortlisted-body
  reading and preparation (`mailbrief-gmail-diagnostic bodies`, `docs/gmail-bodies.md`).
- Implemented, awaiting live acceptance: M4 consented AI analysis and the saved daily brief
  (`mailbrief-gmail-diagnostic brief`, `docs/ai-analysis.md`).
- Implemented, awaiting platform/human acceptance: M5 workflow, settings, full-Inbox
  shortlist review, offline metadata browser and native packages. See `docs/m5-desktop.md`
  and `docs/m5-acceptance.md` for validation and the remaining checks. Scope remains
  in `docs/mvp-plan.md` and `docs/email-implementation-plan.md`.
- Implemented, awaiting live acceptance: M6 AI action suggestions, accepted actions with
  editable plans and target dates, open/waiting/completed lists and undo (desktop, plus
  `brief --show` and `mailbrief-gmail-diagnostic actions`). See `docs/m6-actions.md` and
  ADR 0011.
- M7 implemented, awaiting live acceptance: replies, emails, notes and messages with
  autosave, versions, copy and export, and AI drafting with Groq from context the owner
  chooses (desktop, plus `mailbrief-gmail-diagnostic drafts`). See `docs/m7-drafts.md`,
  ADR 0012 and ADR 0013.
- M8 implemented (merged in PR #17), awaiting live acceptance, built in nine parts
  (`docs/m8-daily-operation.md`); preferences (Parts 1–2) implemented, awaiting live
  acceptance: time zone, messages per brief, sender exclusions, drafting defaults and AI
  limits, in the Settings Preferences tab and `mailbrief-gmail-diagnostic preferences show`.
  See ADR 0014. Brief history and bounded catch-up (Part 3) implemented, awaiting live
  acceptance: the desktop Briefs page and `brief --date`, `briefs list` and
  `briefs show`. Thread tracking backend (Part 4) implemented, awaiting live acceptance:
  the threads of open actions are checked after today's sync (`actions list`,
  `actions seen`). See ADR 0015. Thread activity in the desktop and "Add to" an existing
  action (Part 5) implemented, awaiting live acceptance: continuations and Add to in the
  brief, Mark seen in the actions pane, and `actions accept N --into`. Follow-up proposals
  backend (Part 6) implemented, awaiting live acceptance: analysis schema 7's follow-up
  signal, proposals the owner applies or dismisses (`actions proposals`,
  `apply-proposal`, `dismiss-proposal`) and a ranking bonus for replies in tracked threads.
  See ADR 0016. Follow-up proposals in the desktop and replies outside today's Inbox
  (Part 7) implemented, awaiting live acceptance: proposals in the brief, the actions pane
  and a Proposals dialog with Undo, and up to 3 tracked replies that aren't in today's
  Inbox joining the review and the brief. Daily operation (Part 8) implemented, awaiting
  live acceptance: refresh on launch and every 1, 2 or 4 hours while the desktop is open,
  automatic analysis only with an explicit permission of 1–10 messages on the consent, and
  remembered declines. See ADR 0017. Closeout (Part 9): runs through a day are cumulative,
  with one brief per day. The consolidated live acceptance and known limits are in
  `docs/m8-daily-operation.md`.

- M9 implemented (merged in PR #19, review fixes in PR #20), awaiting owner live acceptance:
  Data and recovery provides consistent backup, archive verification, restore after
  shutdown, portable action/draft/note exports, previewed manual retention, safe
  diagnostics and bundled help. Offline backup/verify/restore is also available through
  `python -m mailbrief.diagnostics.backup`. Restore retains a pre-restore snapshot,
  clears automatic AI permission and never touches credentials. Gmail diagnostic
  database commands hold `desktop.lock` for their whole run: they exit 5 while the desktop
  is open, and the desktop reports that MailBrief is busy while one runs. CI enforces the
  service coverage target and checks native packages at 100%/200% scale with synthetic
  backup/recovery/export/cleanup.
  See `docs/m9-durability.md` and `docs/m9-live-validation.md`. Do not claim M9 accepted
  until the owner completes real-record recovery and the 7–14-day release scenario.
- The desktop UI is the three-pane workspace (PR #21); see `.claude/skills/mailbrief-ui/`.

## Layout (ports and adapters)

- `src/mailbrief/domain/`: frozen Pydantic models; no I/O.
- `src/mailbrief/ports/`: `EmailProvider` / `AIProvider` / `DraftingProvider` protocols
  (`drafting.py`), `ThreadReader` (`threads.py`) and provider-neutral errors.
- `src/mailbrief/providers/gmail/`: active adapter. `providers/microsoft/`: dormant adapter.
  `providers/groq/`: active AI adapter (Structured Outputs), API key store and factory.
- `src/mailbrief/services/`: calendar, sync, ranking, bodies, application, and for M4:
  analysis, deadlines, digest and brief; for M6: actions; for M7: drafts and drafting
  (AI drafting: parts, preview, consent, validation).
- `src/mailbrief/domain/messages.py`: normalized messages, `own_addresses` (the primary
  address plus the aliases; every owner address set is built with it) and
  `is_own_message`, the one definition of the owner's own message, shared by ranking,
  thread activity, outside replies and proposals.
- `src/mailbrief/domain/drafts.py`: draft kinds, limits, placeholders and exports;
  `domain/drafting.py`: what AI drafting sends and gets back; `domain/preferences.py`: the
  owner's preferences, time zone rule and sender rules.
- `src/mailbrief/services/preferences.py`: read, save and reset preferences, apply the
  saved AI limits (`effective_settings`) and the owner's zone (`owner_zone`).
- `src/mailbrief/services/history.py`: saved briefs by day, missed days, the catch-up date
  rule (`check_brief_date`) and each brief's `coverage_line`.
- `src/mailbrief/services/threads.py`: checks the threads of open actions and caches their
  later messages' metadata, forgetting those Gmail has in Trash or Spam (ADR 0015), and
  finds the cached replies today's Inbox sync can't see (`outside_replies`, ADR 0016).
- `src/mailbrief/services/proposals.py`: derives follow-up proposals from a saved brief's
  analyses, and applies, undoes, dismisses and restores them (ADR 0016).
- `src/mailbrief/storage/`: async SQLAlchemy + aiosqlite, repositories, `migrate.py`,
  `actions.py` for suggestions, decisions and accepted actions, `proposals.py` for
  follow-up proposals, `drafts.py` for drafts, their versions and source snapshots, and
  `preferences.py` for the preferences row. Alembic revisions live in
  `migrations/versions/`.
- `src/mailbrief/text/`: provider-neutral text helpers for untrusted email (HTML to text,
  quote trimming, length limits, and `matching.py` for checking quotes against the email).
- `src/mailbrief/infra/`: HTTP retry classification, `vault.py`, the explicit OS
  credential vault, and `files.py`, atomic writes for exports the owner chooses.
- `src/mailbrief/diagnostics/`: developer CLIs. `src/mailbrief/ui/`: PySide6 workflow,
  desktop service composition, saved-brief display, the actions pane and editor, the
  drafts pane (`drafts_view.py`), editor (`draft_editor.py`) and its Write with AI panel
  (`drafting_panel.py`), the Settings Preferences tab (`preferences_view.py`) and the
  Briefs page for saved briefs and missed days (`history_view.py`), the Proposals dialog
  for one action and the wording of a proposal's effect (`proposals_view.py`); `lists.py`
  holds `ActivatingList`, where Return or Enter activates a row once on every platform.
- `ui/scheduler.py` (`RefreshScheduler`, the minute tick behind timed refresh) and
  `ui/auto_send_view.py` (the automatic-analysis permission dialog); `services/consent.py`
  reads and sets that permission.
- `ui/theme/`: design tokens (`tokens.py`, the only place UI colours are defined), the
  Fusion palette and stylesheet (`style.py`, `apply_theme`), bundled Inter under the SIL
  OFL (`fonts/`) and Tabler icons under MIT (`icons/`). `ui/labels.py` holds
  `plain_label`, `wrap_label` (`WrapLabel`) and `button_label`. MainWindow hosts the three-pane workspace
  (`workspace.py`, with `hairline.py`, `brief_list.py` and `brief_detail.py`): the header
  (when Gmail was last checked, Sync and review, Cancel), the sidebar's pages and footer
  (Saved mail, Data, Settings, the Gmail and AI lines, Connect and Disconnect), the Today,
  run (review and consent), Actions, Drafts and Briefs pages, and a status strip with
  Undo.
- `src/mailbrief/app.py` owns the qasync loop.
- `scripts/build_desktop.py` and `scripts/check_package.py`: native PyInstaller builds
  and credential-free package checks. Build artifacts stay in `out/`.

## Invariants (never break these)

- Never write OAuth tokens or API keys to SQLite, logs, exceptions, printed output or
  files. Never write full email bodies there either, beyond the short preview snippet Gmail
  supplies, which SQLite has kept since M2; for a very short email that snippet can be the
  whole text. The one exception is `mailbrief-gmail-diagnostic bodies --show-text`, which
  prints prepared text to the owner's terminal on explicit request. `brief --show` prints
  derived brief content (sender, subject, summary, action, deadline, link, suggestion
  titles, targets and steps, and the titles and IDs of the actions an email continues) on
  explicit request, never evidence or bodies.
- Derived content is bounded: a summary is at most 240 characters, an action at most
  1,000, and evidence at most 300 and strictly under 80% of the body. An email has at most
  five suggestions, each with a title of at most 120 characters, at most five steps of at
  most 120, and evidence of at most 160, and one follow-up signal with a quote of at most
  160. All stored evidence for one email totals at most 600 characters and stays under 80%
  of its body. A summary of a very short email may restate it.
- Text the AI writes itself (summary, action text, suggestion titles and steps) and
  deadline phrases are stripped of control and format characters before they are stored.
  Mail and AI text reaches the UI only through plain-text widgets or escaped HTML, and only
  `https://mail.google.com` message links are ever opened.
- Accepted actions belong to the owner: AI runs never write to them, and they survive
  message, cache and account deletion through their source snapshots. The owner's
  decisions on suggestions survive the same deletions: they are keyed by provider account,
  provider message ID and title fingerprint, never by a cached row. Every change bumps
  the action's revision, and every change except restoring a deleted action needs the
  revision the caller saw. Change decisions and actions through ORM objects, never bulk
  UPDATE or DELETE statements, which leave loaded rows stale.
- Drafts and notes belong to the owner (ADR 0012). They may store the owner's writing
  (title or subject, recipients as typed, body and its versions), a source-email snapshot
  (subject, sender address, link, received time) and an action link with a title snapshot.
  They never store a downloaded incoming body, and MailBrief never inserts quoted incoming
  history; text the owner types or pastes is stored as entered. They have no account
  foreign key and survive message, cache, account and action deletion; delete is soft and
  restorable. A draft keeps at most 100 versions. Draft text never reaches logs,
  exceptions or files other than an export the owner chooses. Drafting never writes to the
  mailbox, has no "sent" state and never changes an action. Every change needs the
  revision the caller saw, except restoring a deleted draft; drafts and versions change
  only through ORM objects.
- Owner preferences are one revisioned SQLite row (ADR 0014); every save needs the revision
  the caller saw, and the row changes only through ORM objects. Sender exclusions are
  enforced by services, never only the UI: a message from an excluded sender is never
  selected, included, downloaded, analyzed or offered to AI drafting. Every accepted sender
  rule can match a sender (`normalize_exclusion`); anything else is refused. Sender rules
  are the owner's text: show them to the owner, but log only their count. Unreadable
  preferences fail closed (`PreferencesUnavailableError`) wherever data could be sent; display-only
  views fall back to the system time zone.
- Precedence: for AI limits, an explicit `MAILBRIEF_*` variable, then the saved
  preference, then the default; only `AI_LIMIT_FIELDS` are read from preferences, and only
  when absent from `Settings.model_fields_set`. For CLI `--timezone`, `--tone` and
  `--length`, the flag, then the saved preference, then the default.
- A brief covers one local day in the owner's zone. Briefing a past day is explicit, one
  day per run, at most 7 days back, and its coverage line says what it covers; nothing
  briefs past days automatically. `BriefService` enforces the date bounds before Gmail is
  contacted or anything is written, and only today's window advances the account's last
  complete sync.
- Thread tracking (ADR 0015) reads metadata only (`threads.get`, `format=metadata`), for the
  live, open actions of the connected account: at most 25 threads and the newest 20
  messages per thread after the action's latest source, during today's sync or brief only.
  Drafts, trash and spam are ignored, and no folder or label (the Sent folder included) is
  ever listed. A cached message a thread check sees in Trash or Spam is deleted from that
  account's cache, and a body labelled TRASH or SPAM is never read. A reply never changes
  an action: activity is derived, and only the owner's `mark_thread_seen` moves the
  watermark. A failed or stopped check never fails the sync.
- Follow-up proposals are the only AI-derived path to change an action, and only the owner
  applies them; applying never touches title, notes, steps or ownership, and never moves a
  target date the owner changed. Deriving proposals never changes an action, applying and
  undoing make one revision each, and a dismissed proposal is never proposed again.
- An email proposes to at most 3 actions ever: the actions that already have a proposal
  from it, in any state, use up its slots; a message the owner sent, or that comes from one
  of the account's addresses, proposes nothing.
- Replies outside today's Inbox (ADR 0016) join only today's run, only when a thread
  service is composed: cached messages in tracked threads, newer than the thread's
  baseline, never the owner's own, never from an excluded sender, not yet analyzed at the
  current schema, at most 3 a run, found from the cache with no Gmail call. They go
  through the same review, consent and messages-per-brief limit. They come only from the
  threads the same run's check read successfully. They are labelled in the review, in
  their own last brief section and in the coverage line.
- Only the owner adds an email to an existing action (`accept_into`), and only to a live,
  open one; the brief offers it only for actions with a source in the email's thread in the
  same account, it makes one revision, and its undo works only while the action is
  unchanged and never removes an action's last source.
- Runs through a day are cumulative: the day's saved brief is carried forward ahead of the
  automatic selection, within messages per brief. A carried message drops out only when it is
  uncached, was an Inbox message since archived, is blocked or is declined; one the brief
  listed as an outside reply keeps that status. "Ready" counts only new messages.
- Automatic runs (ADR 0017) send nothing unless the active consent carries an explicit
  permission (`auto_send_limit`, 1–10, cleared by revoking); with none they only sync, check
  threads, rank and count. The permission belongs to the connected account and at most one
  account holds one: granting clears every other account's, and 0 clears them all.
  `brief --automatic` implies `--silent-only`. They brief today only, run only while the
  desktop is open, coalesce, never open panels, never download bodies without permission,
  and defer messages over the cap. A message the owner left out (an unchecked pick or
  `--exclude`) is never auto-selected.
- Credentials live only in the OS credential store (Gmail: Windows Credential Manager or
  the macOS Keychain, chosen explicitly, with no plaintext or automatic fallback).
- The Groq API key lives only in that OS vault, under `MailBrief.Groq`.
- Provider JSON stays inside its adapter; services and storage use domain models only.
- Timestamps are timezone-aware UTC. Never use naive datetimes or `datetime.utcnow()`.
- Gmail access is read-only (`gmail.readonly`). No mailbox writes or sends.
- Email content is untrusted input. It can never authorize actions, settings changes,
  mail writes or sending other messages to the AI.
- Schema changes need a new additive Alembic revision; never edit an existing revision.
- No blocking network or database work on the Qt main thread (M5).

## AI analysis rules (M4)

- Send only shortlisted messages, and only these seven minimized fields: `message_key` (a
  random per-run key), `subject`, `sender`, `received_local`, `time_zone`,
  `body_truncated` and `body` (truncated plain text).
- Never send tokens, account or tenant IDs, provider message IDs, source links or
  attachments.
- Require explicit first-use consent, and disable provider-side response storage where
  supported; enable Groq Zero Data Retention in Console Data Controls before live mail use.
- Accept only responses that validate against the versioned Pydantic schema. Cache by
  input hash, provider, model, prompt version and schema version.
- Store evidence at most 300 characters long and never as a whole body (under 80% of it).
- Each result may carry up to five action suggestions (schema version 6). A suggestion
  whose evidence does not quote the email is dropped; a deadline phrase that does not quote
  it drops only that suggestion's deadline. Target dates come from Python, never the model.
- Each result also carries a follow-up signal (schema version 7, ADR 0016), judged from the
  email alone: nothing about the owner's actions is ever sent. Its quote is checked before
  the suggestions and counts toward the evidence total; a signal that doesn't check out
  becomes none and never fails the message.

## AI drafting rules (M7, ADR 0013)

- Send only the parts the owner ticks for that generation: the email being replied to
  (subject, sender name only, received time in the owner's zone, and its body, downloaded
  then, prepared like the brief's and never stored); the linked action (title, ownership,
  target date, deadline phrase, steps in order up to 2,000 characters in all, at most 2,000
  characters of notes); the draft's current title and body (at most 8,000 characters).
  Always: kind, tone, length, the owner's instructions (at most 1,000 characters) and
  today's date.
- Never send the sender's address, To or Cc, other emails, attachments, other drafts or
  actions, Gmail or MailBrief IDs, or credentials. The chosen parts are sent as written, so
  addresses, links or numbers inside them are sent too, and the consent text says so.
- Every generation shows a preview of every part with its size and waits for approval.
  First use needs explicit consent, recorded per provider for the owner (not per account)
  before anything is sent; revoking AI consent revokes it too. Declining sends nothing.
- The output schema has no recipients. Python cleans the text, removes quoted history,
  bounds it (body 1 to 8,000 characters, subject 200 and only for emails and notes, at most
  five missing-context items of 200) and rejects a body that copies 200 or more characters
  of the source email. Missing facts stay as `[[placeholders]]`.
- The owner's text is saved as a version before the call; a result becomes a "generated"
  version with its generation record. A failed, declined or cancelled generation leaves the
  text unchanged.
- Groq's HTTP 400 `json_validate_failed` is an incomplete answer (retried once), not a
  rejected request, for briefs and drafting alike.

## Dependencies

- Python 3.13 only, managed with `uv`; direct dependencies in `pyproject.toml`, exact
  versions in `uv.lock`. Update both together.
- Development environment: keep the checkout and its `.venv` out of folders synced by
  iCloud Drive (Desktop & Documents), OneDrive or Dropbox. Those tools set the macOS hidden
  flag on `.venv` files, and then Python 3.13 skips hidden `.pth` files and Qt skips hidden
  plugins.
- Do not add: Google API client libraries, the Microsoft Graph SDK, `python-dotenv`,
  generic retry libraries, HTML parsing libraries (use the standard library if
  HTML-to-text is needed), or local-model libraries.

## Code and tests

- Strict mypy and Ruff (100-character lines). Annotate every function.
- Tests mirror `src/` under `tests/`. Use `tests/factories.py`, `respx` for HTTP and
  injected sleeps/clocks. Tests never touch external networks (localhost is fine), real
  credentials, real mailboxes or paid AI.
- Coverage: at least 80% overall and 90% for the synchronization, ranking, body, digest,
  drafts, drafting, preferences, history, threads, proposals, consent and data services.
  `scripts/check_coverage.py` enforces the 90% combined statement/branch service gate.

## UI work

- Use the `/mailbrief-ui` skill (`.claude/skills/mailbrief-ui/`) for any change under
  `src/mailbrief/ui`.
- Colours come only from `ui/theme/tokens.py`; mail and AI text only through
  `plain_label()`, `wrap_label()` or a delegate's `drawText()`; button text through
  `button_label()`.
- Render the screenshot gallery with
  `MAILBRIEF_UI_SHOTS=scratch/ui-shots uv run pytest tests/ui/test_workspace.py -q --no-cov`
  and compare it with `docs/ui/mockup-three-pane-dark.png`.

## Dormant Microsoft notes

- Microsoft `provider_account_id` is always `{oid}.{tid}` (the MSAL `home_account_id`),
  never the bare Graph object ID.
- Graph is called directly with httpx. The original Microsoft-first specification is
  `docs/archive/microsoft-mvp-plan.md`.

## Commands

```bash
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run mypy --platform win32 src tests
uv run mypy --platform darwin src tests
uv run pytest
```

CI type-checks on both Windows and macOS, so run mypy for both platforms before pushing.

## Git workflow

- Branch from an up-to-date `main`; one pull request per plan; pull requests target `main`.
- Conventional commit messages (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Stage explicit paths. Never commit `.venv/`, `out/`, coverage files, databases,
  OAuth client JSON files or anything from the credential store.
