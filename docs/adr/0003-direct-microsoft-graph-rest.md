# ADR 0003: Call Microsoft Graph REST directly

- Status: Accepted
- Date: 2026-08-31

## Context

The MVP uses a small Graph surface: account identity, Inbox message metadata,
pagination, and plain-text body retrieval. The planned stack already includes
async `httpx`, while adding the generated Graph SDK would increase dependency
weight and expose SDK-specific models to the application.

## Decision

Implement the Microsoft provider with MSAL for authentication and a shared
`httpx.AsyncClient` for Graph REST calls. Map every Graph response immediately
into provider-neutral Pydantic models. Treat `nextLink` as an opaque continuation
URL and validate its host before use.

## Consequences

- Request fields, timeouts, retries, and data minimization remain explicit.
- The provider owns Graph JSON validation and error translation.
- Graph schema or endpoint changes require maintenance in the adapter.
- A future SDK migration is isolated behind the `EmailProvider` protocol.

