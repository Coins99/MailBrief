# Gmail migration file map

This is the verified change surface as of 2026-09-13.

This map covers the provider switch. The expanded 2026-09-25
[email implementation plan](email-implementation-plan.md) also proposes later
schema/service changes for actions, drafts and follow-up. The no-migration
statement below applies only to provider identity changes.

M2 implementation adds `client.py`, `mapper.py`, and `provider.py`, and extends the
diagnostic with metadata sync. Shared models/services now carry failed-item counts
and cached Inbox membership; additive revision `20260925_0002` preserves existing
data. These later changes supersede the original preserve-unchanged list for the
shared sync/ranking/storage surface. The 2026-09-25 reconciliation with `main` moved
main's newer Microsoft adapter into `providers/microsoft/provider.py`.

## Changed now

| File | Reason |
| --- | --- |
| `domain/messages.py` | Add the stable `gmail` provider value. |
| `config.py` | Default to Gmail and accept its desktop OAuth client path. |
| `services/application.py` | Stop relabeling unknown provider data as Microsoft. |
| `storage/repositories.py` | Require an explicit provider when rebuilding messages. |
| `providers/microsoft/provider.py` | Hold the retained Microsoft adapter implementation. |
| `providers/microsoft/__init__.py` | Preserve its existing public imports. |
| `providers/gmail/__init__.py` | Establish the active provider package. |
| `README.md`, `AGENTS.md`, `docs/*` | Provide a small, unambiguous workspace map. |
| matching unit tests | Cover both stable provider identities and new settings. |

No migration is required: `accounts.provider` is already a string and message
uniqueness is scoped to an account.

## Add during Gmail implementation

| Module | Responsibility |
| --- | --- |
| `providers/gmail/auth.py` | OAuth flow and token refresh |
| `providers/gmail/cache.py` | OS-backed credential persistence |
| `providers/gmail/client.py` | Authenticated Gmail REST requests and retries |
| `providers/gmail/mapper.py` | Gmail JSON/MIME to domain models |
| `providers/gmail/provider.py` | `EmailProvider` orchestration |
| `providers/gmail/factory.py` | Production resource composition |
| `diagnostics/gmail.py` | Live fetch, restore, and disconnect checks |
| `docs/gmail-setup.md` | Short Google Cloud setup instructions |

Also update `pyproject.toml`, `uv.lock`, `paths.py`, application composition/UI,
and mirrored tests when those modules are implemented.

## Preserve unchanged

- `providers/microsoft/{auth,cache,factory,graph_client,mapper,provider}.py`
- `diagnostics/microsoft.py` and Microsoft tests
- MSAL dependencies, Microsoft settings/cache path, ADR 0003, and database rows
- provider-neutral ports, sync, calendar, ranking, database tables, and migration

Microsoft remains dormant because the default is Gmail and the UI does not expose
it. Its diagnostic stays available for later Entra validation.
