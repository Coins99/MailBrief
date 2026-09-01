# ADR 0004: Use OpenAI as the first AI provider

- Status: Accepted
- Date: 2026-08-31

## Context

The MVP needs one concrete cloud implementation while preserving the option to
support other cloud or local models later. Digest generation requires predictable
structured data rather than free-form text.

## Decision

Implement OpenAI behind an application-owned `AIProvider` protocol. Use the
async Responses API, JSON-schema Structured Outputs, a versioned Pydantic result
model, and `store=False`. Store the API key in the operating-system credential
store. Do not expose OpenAI SDK types outside the adapter.

## Consequences

- The MVP can validate analysis before storing or displaying it.
- Tests can use a fake provider without making paid network calls.
- OpenAI-specific errors and request options remain adapter details.
- Users must provide a valid API key and explicitly consent to cloud processing.

