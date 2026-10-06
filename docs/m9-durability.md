# M9 durability

Status: implementation complete, awaiting owner live acceptance. See the
[live-validation checklist and journal](m9-live-validation.md). M9 is not accepted
until real-record recovery and the complete personal-email scenario pass.

## Desktop controls

Open **Data and recovery** from the main window. All operations run offline; neither
Gmail, Groq nor the vault is contacted. Close action/draft editors and let pending
saves finish first. A failed draft save must be resolved before data management.
Scheduled refresh pauses while the data dialog is open.

Create backup, export writing and export diagnostics require a new destination outside
the app's data folder. Existing files are refused, including a file created during
publication. Verify checks an archive without changing your saved data.

Restore validates before showing its explicit confirmation, then closes MailBrief,
drains pending work and disposes SQLite before replacing the database. The folder
lock stays held throughout. Recovery revalidates and requires the same reviewed
archive metadata/checksum; swapping even another valid backup after confirmation
is refused. The result shows the retained pre-restore path. Relaunch afterward.
Failed shutdown prevents restore. The Help and setup tab is bundled in the package,
with setup, failure recovery and keyboard instructions.

## Portable writing exports

**Export all writing** produces UTF-8 JSON, format `mailbrief-writing`, version 1.
It includes every action and draft/note/message, including completed and soft-deleted
writing, ordered steps, source snapshots, proposals, saved versions and generation
records. It excludes cached mail/analyses/briefs, account records, settings and secrets.
Incoming full bodies are never downloaded by export. Text typed or pasted by the
owner is exported exactly as stored.

The envelope contains `format`, `version`, `created_at_utc` and `records`. Each
`records` key is a table name: `actions`, `action_steps`, `action_sources`,
`action_proposals`, `drafts`, `draft_versions`, `draft_sources`, `draft_generations`.
Rows contain their named columns, preserving IDs and foreign-key relationships;
timestamps/dates are ISO 8601 and nulls remain JSON null. Rows are ordered by primary
key. This is a readable, portable writing export, not an import or database restore.
Use a backup for restoration. Individual drafts still export as plain text or Markdown.
Backups and writing exports are unencrypted and contain private writing.

## Manual retention

There is no automatic deletion. Select an operation, account where applicable and age
(90 days by default), then **Preview cleanup**. Nothing is deleted until confirmation.
The preview shows affected message, brief, sync, account, deleted-writing, version and
orphaned-decision counts. It is bound to the saved data; intervening changes require
a new preview. Apply rechecks under a SQLite write lock and commits atomically.

| Operation | Removes | Preserves |
| --- | --- | --- |
| Disconnect | Gmail vault credentials | All database records and writing |
| Old/all account cache | Selected messages and derived analysis/suggestion/brief membership, selected brief headers and sync records; resets last-sync watermark | Account, consent, writing, source snapshots, preferences and remembered decisions |
| Remove saved account data | Account, all its cache, sync records and account AI consent | Vault credentials, writing, snapshots, preferences and remembered decisions |
| Permanently remove old deleted writing | Only soft-deleted actions/drafts older than the cutoff, with their steps/sources/proposals/versions/generation records | All live/completed writing; drafts linked to removed actions retain their title snapshot |
| Forget old orphaned decisions | Old Gmail decisions with no saved account identity and no linked action | Writing and all other decisions; forgotten suggestions may appear again after reconnecting |

Cache age compares message receipt time, brief generation time and sync start time to
an aware UTC cutoff. Removing a message removes it from every saved brief and makes
its surviving source snapshots unavailable; it does not erase those snapshots.
Permanently deleted writing cannot be undone without a backup. A failed commit rolls
back the cleanup; a successful cleanup followed by a view failure is reported as
completed, with a restart needed to refresh.

## Diagnostics and packaging

**Export safe diagnostics** writes a new JSON file containing only format/version,
application version, OS platform and Python version. It excludes profile paths,
account identity, settings, historical log text, mail and writing. Desktop logs remain
bounded and contain known codes, exception types and automatic-run counts only.

CI builds on Windows and macOS, checks required services for at least 90% combined
statement/branch coverage, and runs each frozen package in two independent processes
at 100%/200% scale, using disposable synthetic data. Checks exercise migrations,
settings/preferences restoration, metadata display, saved notes/versions, recovery
controls, backup validation/restore, portable export and writing preservation after
cache cleanup. A glyph check catches missing text rendering, not just a nonempty image.
The distribution audit refuses private/development filenames (databases, logs, OAuth
client JSON, token files, device settings, `.env`, `.git`, `.venv`). It is not a forensic
scan of arbitrary binary content. Native file pickers and real display behavior remain
part of the owner's platform acceptance.

## Create a backup

Close MailBrief to include the latest autosaved writing, then run:

```powershell
uv run python -m mailbrief.diagnostics.backup PATH_TO_DATABASE PATH_TO_NEW_BACKUP.zip
```

The command opens the source read-only and uses SQLite's snapshot API, including
committed WAL pages. It checks database integrity, foreign keys, supported schema and
unexpected storage objects before publishing
a finished archive. Existing destinations are refused. A failure leaves the source
and any existing backup intact. Backups, exports and restores work on drives without hard
links, such as exFAT or FAT32 USB sticks: where the filesystem can't publish atomically,
the finished file is copied exclusively (never replacing an existing file) and flushed. A
crash during that copy can leave a partial archive, which fails `--verify`. A crash while
restoring to a new database file on such a drive can likewise leave a partial database,
which nothing checks before it is opened: delete it and restore again. Once flushed,
the file counts as saved even if its temporary copy can't be deleted (an antivirus scan or
a drive error can leave a hidden `.mailbrief-` file or folder beside it), and a drive that
can't sync folders doesn't fail the save.

Format 1 contains exactly `database.sqlite3` and `metadata.json`. Metadata records
the UTC creation time, schema revisions and SHA-256 of the database. The snapshot
contains local metadata, briefs, actions, drafts, versions and preferences, including
soft-deleted records. It does not contact Gmail, Groq or the OS credential vault.
OAuth tokens and API keys are excluded by the existing database storage policy.
The archive is not encrypted: keep it private, because it includes your writing.

## Verify and restore

```powershell
uv run python -m mailbrief.diagnostics.backup --verify PATH_TO_BACKUP.zip
uv run python -m mailbrief.diagnostics.backup --restore PATH_TO_BACKUP.zip PATH_TO_NEW_DATABASE
# Replace an existing database only after preserving a separate copy:
uv run python -m mailbrief.diagnostics.backup --restore PATH_TO_BACKUP.zip PATH_TO_DATABASE --replace
```

Close the desktop and diagnostic commands first; let the desktop finish saving drafts
and exit normally. Other tools using the database must also be closed. Restore acquires
the desktop's data-folder lock; Gmail diagnostic database commands now hold that same
lock for their entire run. A running desktop or diagnostic command refuses recovery.

That shared lock also changes everyday use of `mailbrief-gmail-diagnostic`. A command
that uses the database (`sync`, `bodies`, `brief`, `actions`, `drafts`, `preferences`
and the others) exits with code 5 while the desktop is open, and launching the desktop
while such a command runs reports that MailBrief or a diagnostic command is using its
data. The lock file is created beside the database, so `--database` must point into a
folder you can write, even for commands that only read.

The archive must contain exactly two members, with valid format-1 metadata, a UTC
timestamp, no credentials, a matching SHA-256 and a known schema revision. Unknown
formats/revisions, extra or duplicate members, invalid SQLite relationships, views
and triggers are refused. Metadata is limited to 16 KiB and the snapshot to 1 GiB.
The outer file and directory are bounded before ZIP parsing. Only stored/deflated,
unencrypted members are accepted; ZIP64 and multi-disk archives are refused.
No member paths are extracted. A checksum establishes integrity, not authenticity.

Restore validates in a temporary folder beside the target, upgrades that staged
database with the bundled migrations, verifies column types, nullability, primary/
foreign keys, uniqueness and indexes against the current schema (an extra or missing
index is refused), and
flushes it before publishing. For an existing target, `--replace` is required: a
consistent snapshot named `<database>.pre-restore-<unique-id>.sqlite3` is kept beside
it before the atomic replacement. The command prints that retained path. A failed
validation or upgrade does not replace the current database; a publication failure
keeps both the current database and any already published pre-restore copy. Temporary
files are cleaned up during ordinary failure; interrupted processes may leave a
`.mailbrief-restore-*` staging folder, which is never used automatically.

Restore refuses remaining WAL, shared-memory or journal files rather than allowing
old pages to be replayed into a new database. Do not delete sidecar files by hand:
close the clients, preserve the original database and resolve its SQLite state first.
If the current database is too damaged to snapshot, recovery refuses replacement;
restore the archive to a new filename while keeping the damaged database and sidecars.

Owner records, soft-deleted records, versions, preferences and manual AI consent are
preserved. Automatic AI-analysis permission is cleared, and must be re-enabled explicitly.
OS-vault credentials are neither read nor modified. On another computer reconnect
Gmail and configure the AI key separately. Existing credentials on this computer remain
in the vault; the backup does not supply them.

Verification establishes snapshot integrity and schema compatibility. A checksum is
not proof of the archive's origin: restore only a backup you intend to use.

## Remaining acceptance

The owner runs live mailbox/paid-AI checks, native platform usability, real-record
clean-profile recovery and 7–14 days of personal use. Use a separate OS test user or
computer for native clean-profile validation; moving the executable does not isolate
the app-data folder or vault. From source, the offline CLI can restore to an explicitly
chosen new database filename without touching the current profile.
No live mailbox, credentials, paid AI or real owner database was used in automated checks.

## Automated validation

Windows, 4 October 2026: reviewed full suite **2,762 passed, 1 skipped**, **97.09%**
combined statement/branch coverage; all required services meet 90%. Additional focused
checks passed for an actual subprocess exit immediately before restore publication
and a cleanup commit failing after SQL deletes were flushed. Ruff format/lint and
strict mypy (native, win32 and darwin) passed. The native Windows package built and
passed the distribution audit and two independent offline smoke processes at 100%/200%.
The data dialog was also visually inspected at 200% with an OS font.
Native macOS build/execution runs in CI; it cannot be performed locally on Windows.

Engineering review replaced quadratic cleanup previews and full-cache materialization
with SQL counts and bounded batches. Regression tests cover failed-save tracking per
draft, closed dialogs remaining closed after asynchronous work, recovery help without
opening unavailable storage, and archives with missing keys or incompatible columns.
Security review additionally bounds ZIP parsing and compression, refuses executable
schema objects, and updates PyJWT/urllib3. The OSV recheck found no known advisories
for the 69 locked PyPI packages; native components and unknown vulnerabilities are
outside that check.

Backup tests cover committed WAL data, migrated schema snapshots, existing-file
preservation, invalid/missing sources and publication failure cleanup. Recovery
tests preserve synthetic owner actions, steps, notes, draft versions, source
snapshots and preferences, and upgrade snapshots from revisions 0007 and 0009.
They also cover invalid metadata/checksums/revisions, duplicate/unexpected archive
members, size bounds, stale SQLite sidecars, migration/flush/publication failures,
explicit replacement permission under a destination race, and running database clients.
Folder-lock tests include a separate process with the desktop's application identity,
failed sign-in cleanup, preserved owner data, and an unavailable volume.
The hard-stop check retains the original and published pre-restore copy, leaves its
staging folder unused, and verifies that a fresh attempt recovers the dead process lock.
UI checks cover cancel/confirm, portable writing/diagnostic exports, keyboard Escape,
failed-storage recovery access, waiting for editors/writes, hidden private errors,
cleanup results after view failure, and restore only after shutdown while the lock
remains held. Retention checks cover exact cutoff selection, preserved owner records,
stale previews, transaction rollback, and explicit orphaned-decision removal.
