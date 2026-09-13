# ADR 0007: Gmail is the first provider

- Status: Accepted
- Date: 2026-09-13

## Decision

Ship Gmail first behind the existing `EmailProvider` port. Keep Microsoft code and
tests dormant until Entra registration is available. Use direct async REST calls
after Google OAuth and store tokens outside SQLite in OS-backed encrypted storage.

## Consequences

Gmail is the runtime default. Core sync, ranking, and storage stay provider-neutral.
Public Gmail distribution requires a separate policy and verification review.
