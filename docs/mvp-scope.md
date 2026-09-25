# Personal desktop scope

Updated: 2026-09-25. MailBrief is the email entry point for a future personal
task/calendar/analytics ecosystem. Build for the owner and one Gmail account, on Windows and
macOS. The server-hosted website comes after the desktop ecosystem.

## Email MVP: M0–M5

- Gmail read-only OAuth, secure restoration and disconnect.
- Today's Inbox metadata, pagination, exact local-day boundaries and ranking.
- Reviewable shortlist and body retrieval only for selected messages.
- Explicit cloud-AI consent, structured summaries/actions/deadlines, valid-result
  caching, saved briefs and Gmail source links.
- Responsive, cancellable PySide6 workflow, a Windows `onedir` package and a macOS app
  bundle.
- Existing Microsoft support retained but dormant.

## Complete personal email workspace: M6–M9

- Multiple action suggestions, durable accepted actions, editable plans,
  extracted deadlines and separate suggested/user-selected target dates.
- Carryover, waiting/completed states, thread updates and protection of user edits.
- Locally saved email, note and message drafts with editing, copy and export.
- History, bounded catch-up, local preferences and optional refresh while running.
- Backup/restore, retention controls, upgrades and personal-use validation.

Direct Gmail draft saving is optional M10. Sending is not required to finish the
email scope. A generated/copied draft is not evidence of sending or completion.

## Later work

General tasks/projects, an adjustable internal calendar with suggested work
blocks, richer notes and analytics follow email. See the provisional
[ecosystem roadmap](ecosystem-roadmap.md).

Website/server development, cross-device sync, public distribution, multiple
accounts/users, external calendar writes, attachments, autonomous mail changes,
local AI, installers and auto-update are not email-release requirements.

## Data and release rules

Persist metadata, bounded derived results, preferences and explicit user-owned
artifacts locally. Never store full incoming bodies or credentials in SQLite or
logs. Clarify the storage ADR before introducing saved drafts; their text is
user-owned work, not a mailbox-body cache. Credentials use the OS store.
Local storage does not imply local AI processing.

Provider identity changes require no migration; new actions, drafts and tracking
state require tested additive migrations. Preserve Microsoft data and tests.
Quality targets remain Ruff, strict mypy, 90% service coverage and 80% overall
coverage, with packaged-app and live-mailbox checks.

See [delivery sequence](mvp-plan.md) and the
[detailed implementation plan](email-implementation-plan.md) for dependencies,
file guidance and acceptance criteria. Planned features are not implemented yet.
