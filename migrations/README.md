# Database migrations

Alembic revisions are the production schema authority. Set
`MAILBRIEF_DATABASE_URL` to an async SQLAlchemy URL before running Alembic, for
example:

```powershell
$env:MAILBRIEF_DATABASE_URL = "sqlite+aiosqlite:///C:/path/to/mailbrief.sqlite3"
uv run alembic upgrade head
```

`Database.create_schema_for_tests()` exists only for isolated unit tests and
must not replace migrations in application startup or release builds.

## History

Git history shows revision 0001 edited twice after release: on 2026-09-03
(`65e4b63`) an `accounts.account_addresses` column was added in place, and on
2026-09-25 (`7b39448`) that edit was reverted, restoring the originally released
content; revision 0003 adds the column instead. The restore was a one-time repair.
Existing revisions must never be edited; schema changes need a new additive revision.
