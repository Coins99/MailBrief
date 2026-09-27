# M5 desktop implementation progress

M5 implementation is ready for platform and human acceptance on `feat/m5-desktop`,
based on `main` after PR #12 merged (`a1fc1b4`). Desktop workflow, graphical settings,
full-Inbox shortlist review, offline metadata browsing and native packaging are
implemented. See [the remaining acceptance checklist](m5-acceptance.md).

## Run and configure

From source, use Python 3.13:

```powershell
uv sync --locked --all-groups
uv run mailbrief
```

Open **Settings** to choose a Google **Desktop app** OAuth client JSON file and
enter a Groq model supporting Structured Outputs. Save those settings, then enter
and save the Groq API key. Existing keys are never displayed. See
[Gmail setup](gmail-setup.md) for Google project configuration and
[AI analysis](ai-analysis.md) for Groq setup. Enable Groq Zero Data Retention
before approving transmission of real mail.

The OAuth file path and model are saved atomically in `desktop-settings.json`
beside the profile database. The OAuth file itself is not copied. API keys and
OAuth tokens remain exclusively in Windows Credential Manager or macOS Keychain.
Vault and settings I/O run off the Qt thread. Closing during a settings write
waits for the write to finish before disposing application resources.

On first use, the two desktop fields inherit `MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH`
and `MAILBRIEF_GROQ_MODEL` when present. Once saved, desktop settings override
those environment values, including deliberately blank fields. Other analysis
limits still come from the existing environment configuration. Diagnostic CLIs
continue to use their own environment settings.

Settings also supports key removal and revocation of Groq consent for all locally
stored Gmail accounts. Neither operation removes saved briefs or cached analyses.
Each actual transmission still requires approval; revocation restores the first-use
disclosure for subsequent transmissions.

## Daily workflow

The app migrates local storage and restores the latest saved Gmail brief before
attempting silent sign-in. Offline viewing of saved metadata and results does
not require provider credentials. Gmail and AI configuration have separate status
labels.

Connect Gmail, then choose **Sync and review**. Review all ranked messages from
today's Inbox. Automatic suggestions are prechecked; add or remove messages,
up to ten in total, and continue. Rank scores and reasons appear in row tooltips.
The service independently rejects invalid IDs, duplicates and oversized selections.
Only checked messages have bodies retrieved, with no second sync
between review and analysis. Approve or decline the transmission disclosure;
cached analyses can be reused without a new AI call.

The brief shows account, date, save time/age, coverage, partial or empty status,
summaries, actions and resolved deadlines. Gmail source links retain their
account routing. Email text is escaped and cannot create additional links.

Only one operation runs at a time. Network work, shortlist review and consent can
be cancelled. Failed or cancelled refreshes preserve the displayed saved brief.
Closing cancels pending workflow work, closes provider/session resources, and
then disposes the database. **Sync and review** also retries incomplete runs.
An in-flight worker-thread migration is joined before shutdown or another operation
can access the database. An incomplete sync with no usable messages also preserves
the last good brief rather than saving a misleading empty one.

**Browse saved mail (offline)** opens a read-only view of locally cached Gmail
metadata. Choose an account and received date in your local timezone, and use
100-message pages. The browser includes messages no longer marked as being in the
Inbox, labels the snapshot as potentially stale, and shows the account's last
complete sync time. Sender, subject, received time and the existing Gmail preview
are available without credentials, body retrieval, AI or network access. Opening
a source in Gmail is an explicit separate action. Local-day filtering respects
daylight-saving boundaries.

Keyboard navigation includes Space to toggle a focused review row. The main
workflow scrolls when space is limited so review/consent controls remain reachable.

## Build and check packages

Build on the target OS using the locked development environment:

```powershell
uv run python scripts/build_desktop.py
uv run python scripts/check_package.py
```

- Windows: `out/dist/MailBrief/MailBrief.exe` inside an **onedir** package. Keep
  the entire `MailBrief` folder together, including `_internal`.
- macOS: `out/dist/MailBrief.app`, built on macOS for the build machine's
  architecture. Release signing/notarization is not configured.

The build includes Qt, timezone/TLS resources, explicit dynamic database imports,
and only the migration source files. It excludes development test/type-checking
modules. User databases, OAuth files, API keys and local settings are never build
inputs. Runtime migrations resolve from the bundle and write to the user-data
location, not to the application directory.

`check_package.py` launches the actual executable twice, with a temporary working
and data directory outside the source tree's runtime paths and without inherited
MailBrief/Python configuration. Each process tests two runtime launches, database
migrations, settings restoration, synthetic cached metadata, Qt rendering,
timezone/TLS data and shutdown.
It imports the OS vault adapter but never instantiates it or reads credentials.
No Gmail, AI or external network requests occur.

The `--smoke-test-dir PATH` application option enables that isolated mode. Failure
reports contain only the exception type and, for import failures, the missing
module name. To troubleshoot build imports, `build_desktop.py --console` creates
a console build; rerun without that flag for the normal desktop package.

CI now builds and checks both platforms after the quality gates, then uploads
seven-day build artifacts. The macOS app is zipped with `ditto` to retain its
permissions and symlinks. Build flags and bundle-resource resolution follow the
[PyInstaller usage](https://pyinstaller.org/en/stable/usage.html) and
[runtime resource documentation](https://pyinstaller.org/en/stable/runtime-information.html).

## Remaining acceptance and follow-up

- Execute and inspect the macOS build/check job on a macOS runner.
- Verify packaged live connect → sync → review → analyze → display → open source,
  then restart, on Windows and macOS profiles without the development environment.
- Verify source links with multiple Google accounts, keyboard navigation, scaling,
  reconnect/offline and partial-result scenarios against real accounts.
- M4's outstanding live checks remain tracked in `m4-validation.md`.

## Local validation environment

Windows validation on 2026-09-27: **1,049 passed, 1 skipped; 94.91% coverage**.
Ruff formatting/lint and strict mypy on native, win32 and darwin targets pass.
The windowed Windows onedir package passes `check_package.py` in two separate
processes. Settings, offline browsing and shortlist layouts were also inspected
with synthetic data, including scaled rendering. The suite
still reports the existing MSAL deprecation warnings and one SQLAlchemy
connection-cleanup warning previously recorded for PR #12.

The existing `.venv` has inaccessible files. Validation uses the same locked
requirements in `out/m5-venv`, installed with `--link-mode copy` for OneDrive.
To use that environment from PowerShell:

```powershell
$env:UV_PROJECT_ENVIRONMENT = 'out/m5-venv'
uv run --no-sync mailbrief
```

Tests use fake providers, synthetic credentials/messages and temporary databases.
They cover settings precedence and failed writes, credential-field clearing,
background vault calls, consent-revocation scope, shutdown during a write,
shortlist inclusion/exclusion and bounds, keyboard selection, consent/decline,
overlap prevention, cancellation, saved-brief preservation, resolved deadlines,
safe source links, offline account isolation/pagination/DST boundaries, bundled
migrations and the actual Qt/async event loop.
