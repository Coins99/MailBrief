# ADR 0005: Use SQLite for local storage

- Status: Accepted
- Date: 2026-08-31

## Context

MailBrief is a single-user desktop application with local-first storage,
moderate data volume, and no need for an external database service. The UI must
remain responsive while persistence work occurs.

## Decision

Use SQLite through SQLAlchemy's asyncio API and `aiosqlite`. Manage schema
changes with Alembic, enable SQLite foreign keys for every connection, and keep
the database in the operating system's application-data directory. Persist
message metadata and analysis results, but never persist full message bodies or
credentials.

## Consequences

- Installation has no database-server prerequisite.
- SQLAlchemy provides an explicit transaction and repository boundary.
- SQLite write concurrency is limited but sufficient for one desktop process.
- Schema changes require reviewed Alembic revisions.
- Migration to another relational database remains possible but is not an MVP
  goal.

