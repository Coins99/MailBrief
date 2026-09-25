# Personal Gmail setup and M1 diagnostic

M1 implements read-only sign-in and secure session restoration. The `fetch`
diagnostic verifies your Gmail profile; it does not retrieve mail or write SQLite.
M2 adds a separate `sync` command for [metadata and ranking](gmail-metadata.md).
Daily summaries and UI wiring follow in later milestones.

## 1. Create a Google Desktop OAuth client

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create or
   select a project for personal MailBrief development.
2. Enable the Gmail API for that project.
3. In Google Auth Platform, configure the application branding and consent
   audience. For a personal Gmail account use External; while publishing status
   is Testing, add your own Gmail address as a test user.
4. Configure the read-only Gmail scope:
   `https://www.googleapis.com/auth/gmail.readonly`.
5. On the Audience page, select **Publish app** so the publishing status becomes
   **In production**. A personal app can stay unverified: Google then shows an
   unverified-app warning on the consent screen at each interactive sign-in, which you
   accept. While the status is Testing, Google expires the saved sign-in after seven days.
6. Create an OAuth client with application type **Desktop app**, then download
   its JSON file. A Web application client file is not interchangeable.
7. Store the file outside the repository and avoid shared/synced folders. Do not
   paste the JSON into issues, logs or chats. No redirect endpoint or hosted
   server needs to be deployed; the app uses an ephemeral loopback port.

The implementation uses fixed Google endpoints, system-browser authorization,
PKCE and callback state checking. It does not trust endpoint URLs supplied in the
downloaded JSON. See [Google's desktop OAuth documentation](https://developers.google.com/identity/protocols/oauth2/native-app).

## 2. Configure and connect

In PowerShell from the MailBrief project directory:

```powershell
uv sync --locked --all-groups
$env:MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH = 'C:\private\mailbrief-google-desktop.json'
uv run mailbrief-gmail-diagnostic fetch
```

Replace the example path with your downloaded file's actual location. The
environment setting applies to this PowerShell process and its child commands.
PowerShell accepts the assignment even if the file does not exist. Check it with
`Test-Path -LiteralPath $env:MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH -PathType Leaf`;
the result must be `True` before running the diagnostic.

On macOS, in Terminal from the project directory:

```bash
uv sync --locked --all-groups
export MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH="$HOME/private/mailbrief-google-desktop.json"
test -f "$MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH" && echo found
uv run mailbrief-gmail-diagnostic fetch
```

The `export` applies only to that Terminal window, and `found` must print before you
continue.

Sign in to the intended Gmail account in the browser and approve read-only access.
The callback waits up to three minutes. Ctrl+C cancels and closes the listener.

Expected result:

```text
Gmail read-only connection verified. No messages downloaded (M1).
```

The diagnostic intentionally omits your email address and all tokens. The browser
account chooser lets you verify which account is being connected.

## 3. Check restoration

Close the command, open another PowerShell or Terminal window, set the same client-file path,
then run:

```powershell
uv run mailbrief-gmail-diagnostic fetch --silent-only
```

It must succeed without opening a browser. Refresh credentials are stored in
Windows Credential Manager or the macOS Keychain under `MailBrief.Gmail`, never in
SQLite or token files. Access tokens remain in process memory. MailBrief chooses the
vault explicitly and refuses plaintext or automatic keyring fallbacks; other operating
systems are not supported.

On macOS, the Keychain may ask whether Python can use `MailBrief.Gmail` (for example
after Python is updated); choose Always Allow. To confirm the entry exists without
revealing it, run `security find-generic-password -s MailBrief.Gmail >/dev/null && echo stored`.
Each computer keeps its own sign-in, so sign in once on every machine you use.

There is one stored Gmail account. Changing the OAuth client or choosing a
different mailbox requires an explicit local disconnect first. A profile mismatch
does not overwrite an existing account's credentials.

Google documents seven-day refresh-token expiry for External apps in Testing
when using Gmail scopes; publishing the app (setup step 5) avoids it. If you publish
after connecting, run `disconnect` and then an interactive `fetch` once so the new
sign-in is not time-limited. If you stay in Testing, reconnect when needed; this is
not a failed persistence
test. Tokens can also be revoked or expire for other reasons. See
[Google's token-expiration guidance](https://developers.google.com/identity/protocols/oauth2#expiration).

## 4. Disconnect and revoke

```powershell
uv run mailbrief-gmail-diagnostic disconnect
```

This removes locally stored Gmail credentials, even if the client JSON is missing
or malformed. Running it twice is safe. It does not delete messages, local app
data, or Google's authorization grant. It cannot stop a separate already-running
process from using an access token already held in memory; close other instances.

To revoke Google's grant too, remove MailBrief from your
[Google account connections](https://myaccount.google.com/connections). Then a
silent fetch should ask you to reconnect. Local data removal is a separate future
application action, not a side effect of disconnect.

## Troubleshooting

| Exit | Meaning | Next action |
| --- | --- | --- |
| 0 | Profile verified or local disconnect completed | Continue |
| 2 | Missing/expired authorization, denial, browser failure or callback timeout | Run interactive fetch; confirm the account and consent |
| 3 | Invalid configuration or secure store unavailable | Check client JSON/path and Windows Credential Manager or the macOS Keychain |
| 4 | Provider/network/permission failure | Check connection, enabled Gmail API and read-only consent; retry |
| 130 | Cancelled with Ctrl+C | Run again when ready |

If Google shows `access_denied`, check the test-user list and selected account.
If the diagnostic reports that Gmail API is disabled, select the Google Cloud
project that created your OAuth client, open **APIs & Services → Library → Gmail
API**, and enable it. An OAuth client alone does not enable the API. Retry the
interactive command after enabling it; a replacement client file is unnecessary.
Permission diagnostics translate recognized Google error reasons into static
guidance without displaying raw responses or account details.
Other failed authentication requests report their stage (token exchange, token
refresh, or Gmail profile check) and HTTP status. Include that diagnostic text
when reporting a failure; do not share client JSON, tokens, or raw HTTP responses.
If using an organizational account, administrator restrictions may apply.
The HTTP client uses fixed HTTPS endpoints, finite request timeouts, no redirects
and no environment-provided proxy configuration. A network requiring a proxy is
not supported by this initial composition. Never enable HTTP wire logging while
using real credentials; the diagnostic suppresses HTTP debug logs.

The UI still shows the foundation shell. Successful authentication is not evidence
that message sync or summaries have been implemented.

## Live acceptance checklist

- [x] Interactive sign-in succeeds for the intended personal account (owner reported).
- [x] A new process restores with `--silent-only` without a browser (owner reported).
- [ ] Denying consent and cancelling return safely without creating a session.
- [x] Local disconnect and subsequent silent-fetch/reconnect behavior passed (owner reported).
- [ ] Google-side revocation produces a reconnect state; interactive recovery works.

Unchecked live checks remain pending. Automated tests use synthetic credentials,
mock Google responses and local callback requests only.
