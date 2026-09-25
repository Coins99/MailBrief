# M2 validation record

Implementation branch: `feat/gmail-m2-metadata`.

## Implemented

- Gmail Inbox ID pagination with exact local-day filtering and cross-page deduplication.
- Metadata-only requests, five concurrent fetches, bounded responses, retries and cancellation.
- Normalized headers, received times, labels, bounded snippets and account-aware source links.
- Additive Inbox-membership and failed-item-count migration, preserving existing provider data.
- Safe reconciliation after complete scans, partial-result reporting and last-success tracking.
- Local ranking, bounded per-run manual inclusion/exclusion, and the `sync` diagnostic.
- Protection against a saved-account change during an active token-refresh cycle.

No full-body retrieval, attachment download, AI processing or Gmail write operations
are included. [Usage and limitations](gmail-metadata.md).

## Automated results

- 374 tests passed after the setup-diagnostic improvements, including the retained Microsoft suite.
- Overall coverage: 95.35%; application, ranking and sync service coverage each exceed 90%.
- Strict mypy passed for 99 source/test files.
- Ruff lint and formatting passed, excluding generated `out/` artifacts.
- Migration tests cover upgrade/downgrade/schema agreement and preservation of
  existing Gmail and Microsoft message rows.
- End-to-end diagnostic tests use real Alembic/SQLite with synthetic Gmail HTTP
  responses; counts-only output omits subjects and addresses.
- Additional tests cover page boundaries, deduplication, archive reconciliation,
  partial failures, permissions, response limits, token refresh, cancellation,
  concurrency and manual review.

Validation used the locked Python 3.13 environment documented for M1. Generated
test/cache/coverage paths were isolated under `out/`. The pre-existing SQLAlchemy
connection-cleanup warning remained; Windows also denied a best-effort pytest
cache write and a Ruff cache write. These did not fail the checks or tests.

## Live validation

The owner reported successful live metadata synchronization for Inbox date
2026-09-25 in America/Toronto: status complete, one page, one message retrieved,
zero failed items and one selected message. The reported last complete sync was
2026-09-25 05:51:06.234949 UTC. Output confirmed metadata-only operation.

The owner then reported that a subsequent `sync --silent-only` completed without
opening a browser. The live synchronization and saved-session restoration checks
have passed. Earlier setup failures are resolved for this tested path; their
underlying Google-side cause was not confirmed.

These results are owner-reported, not independently observed. Browser source-link,
archive/refresh, persisted-row deduplication, manual include/exclude and cancellation
checks remain manual acceptance steps in the usage guide; they are covered by
automated tests where applicable but have not been reported as live checks.
