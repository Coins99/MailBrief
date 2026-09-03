# Microsoft account setup and diagnostic

MailBrief uses Microsoft Entra delegated permissions and the system browser. The
desktop application is a public client: its application (client) ID is public
configuration, and a client secret must never be created, configured, or shipped.

## Register the application

1. In the Microsoft Entra admin center, create an app registration named
   `MailBrief`.
2. For supported account types, choose **Accounts in any organizational directory
   and personal Microsoft accounts**.
3. Under **Authentication**, add the **Mobile and desktop applications** redirect
   URI `http://localhost` and enable public-client flows.
4. Under **API permissions**, add the Microsoft Graph delegated permissions
   `User.Read` and `Mail.Read`. Do not add application permissions.
5. Copy the application (client) ID. Do not create a client secret.

In PowerShell, configure the current process and run the diagnostic:

```powershell
$env:MAILBRIEF_MICROSOFT_CLIENT_ID = "<application-client-id>"
uv run mailbrief-ms-diagnostic fetch
```

The first run opens the system browser. MailBrief requests account selection and
consent, fetches `/me`, and retrieves at most the first Inbox metadata page for the
current UTC day. It never retrieves message bodies or attachments. Successful
output contains only the account email address and first-page message count:

```text
Connected: user@example.com
Messages in first page: 12
```

## Verify restart and removal

Close the first process, retain the same environment variable, and run:

```powershell
uv run mailbrief-ms-diagnostic fetch --silent-only
```

Success without a browser proves that the encrypted MSAL cache restored the
session in a fresh process. To remove all cached Microsoft account and token
records, run:

```powershell
uv run mailbrief-ms-diagnostic disconnect
uv run mailbrief-ms-diagnostic fetch --silent-only
```

The second command should exit with code `3`. Disconnect works without the client
ID and without network access. It retains only nonsecret MSAL application metadata
and the persistence/lock envelope; platform-level physical credential deletion is
part of the packaging security audit.

## Troubleshooting

- Exit `2`: verify the client ID and application-data access. Encrypted persistence
  must initialize; MailBrief never falls back to plaintext in production.
- Exit `3`: the session is absent, expired, ambiguous, or interactive sign-in was
  cancelled. Run `fetch` without `--silent-only`.
- Exit `4`: the account or tenant has not granted `User.Read` and `Mail.Read`, or a
  tenant policy blocks them. An administrator may need to grant consent.
- Exit `5`: Microsoft is offline, unavailable, or throttling requests. Retry after
  connectivity or service health recovers.
- Exit `6`: Graph returned an unsupported or malformed response.
- Exit `130`: the command was interrupted.

Do not paste tokens, the token-cache file, raw Graph responses, or verbose identity
logs into an issue. Public diagnostic errors are deliberately sanitized. The
encrypted token cache is stored below Qt's platform-specific local application-data
directory; tokens are never written to the MailBrief SQLite database.
