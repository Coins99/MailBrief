# Personal Gmail setup and M1 diagnostic

M1 implements read-only sign-in and secure session restoration. Message listing,
daily summaries and UI wiring follow in later milestones. The `fetch` diagnostic
currently verifies your Gmail profile; it does not retrieve mail or write SQLite.

## 1. Create a Google Desktop OAuth client

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create or
   select a project for personal MailBrief development.
2. Enable the Gmail API for that project.
3. In Google Auth Platform, configure the application branding and consent
   audience. For a personal Gmail account use External; while publishing status
   is Testing, add your own Gmail address as a test user.
4. Configure the read-only Gmail scope:
   `https://www.googleapis.com/auth/gmail.readonly`.
5. Create an OAuth client with application type **Desktop app**, then download
   its JSON file. A Web application client file is not interchangeable.
6. Store the file outside the repository and avoid shared/synced folders. Do not
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
Sign in to the intended Gmail account in the browser and approve read-only access.
The callback waits up to three minutes. Ctrl+C cancels and closes the listener.

Expected result:

```text
Gmail read-only connection verified. No messages downloaded (M1).
```

The diagnostic intentionally omits your email address and all tokens. The browser
account chooser lets you verify which account is being connected.

## 3. Check restoration

Close the command, open another PowerShell session, set the same client-file path,
then run:

```powershell
uv run mailbrief-gmail-diagnostic fetch --silent-only
```

It must succeed without opening a browser. Refresh credentials are stored in
Windows Credential Manager under `MailBrief.Gmail`, not in SQLite or token files.
Access tokens remain in process memory. This Windows-first implementation refuses
plaintext/automatic keyring fallbacks and currently does not support other OSes.

There is one stored Gmail account. Changing the OAuth client or choosing a
different mailbox requires an explicit local disconnect first. A profile mismatch
does not overwrite an existing account's credentials.

Google documents seven-day refresh-token expiry for External apps in Testing
when using Gmail scopes. Reconnect when needed; this is not a failed persistence
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
| 3 | Invalid configuration or secure store unavailable | Check client JSON/path and Windows Credential Manager |
| 4 | Provider/network/permission failure | Check connection, enabled Gmail API and read-only consent; retry |
| 130 | Cancelled with Ctrl+C | Run again when ready |

If Google shows `access_denied`, check the test-user list and selected account.
If using an organizational account, administrator restrictions may apply.
The HTTP client uses fixed HTTPS endpoints, finite request timeouts, no redirects
and no environment-provided proxy configuration. A network requiring a proxy is
not supported by this initial composition. Never enable HTTP wire logging while
using real credentials; the diagnostic suppresses HTTP debug logs.

The UI still shows the foundation shell. Successful authentication is not evidence
that message sync or summaries have been implemented.

## Live acceptance checklist

- [ ] Interactive sign-in succeeds for the intended personal account.
- [ ] A new process restores with `--silent-only` without a browser.
- [ ] Denying consent and cancelling return safely without creating a session.
- [ ] Local disconnect is repeatable; subsequent silent fetch requires sign-in.
- [ ] Google-side revocation produces a reconnect state; interactive recovery works.

These checks require a real Desktop OAuth client file and user sign-in. They are
pending until that setup is available. Automated tests use synthetic credentials,
mock Google responses and local callback requests only.
