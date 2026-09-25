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

- 341 tests passed, including the retained Microsoft suite.
- Overall coverage: 95.26%; application, ranking and sync service coverage each exceed 90%.
- Strict mypy passed for 98 source/test files.
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

The initial silent attempt reported no restorable Gmail session, before any mail
retrieval. Interactive reconnection and a subsequent silent metadata sync are
pending the owner's execution. Do not treat the automated fixture counts as
live-mailbox validation. Browser source-link and archive/refresh checks are also
manual acceptance steps documented in the usage guide.
