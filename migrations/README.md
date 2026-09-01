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
