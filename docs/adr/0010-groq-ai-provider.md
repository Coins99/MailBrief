# ADR 0010: Groq for consented AI analysis

Date: 2026-09-26. Status: accepted; live acceptance pending.

Supersedes ADR 0004's OpenAI provider choice and the OpenAI transport/storage details
in ADR 0009. The versioned extraction contract, evidence validation and minimized
inputs remain in effect.

## Decision

Use Groq Chat Completions through the existing httpx dependency, with strict JSON
Schema and local Pydantic/domain validation. Recommend `openai/gpt-oss-120b` on Groq.
Pin the endpoint; disable environment proxies, redirects, tools and streaming.
Remove the OpenAI SDK and adapter. Do not provide automatic provider fallback.

Keep the key in the OS vault under `MailBrief.Groq`. Require fresh provider-specific
consent and disclose Groq's console-controlled Zero Data Retention setting accurately.
The application cannot attest to that remote setting. Preserve historical provider
identities in stored analyses and briefs; no schema migration is needed.

Count every attempt against a local per-run request limit, including retries. Start
with one email per request, 4,000 body characters and 2,000 output tokens. Pace exhausted
quotas using response headers, and stop for long delays or terminal billing failures.
The owner accepts Groq's organization-wide monthly spending control; per-key monthly
accounting is outside scope. The free plan is the initial target.

## Consequences

Groq keys and model configuration must be set up separately. Strict schema support,
quality and actual account quotas need synthetic and consented live validation before
M4 acceptance. Monthly paid spending enforcement may lag; local request/output limits
are not a dollar cap. Existing OpenAI data and keys remain intact.

See [the implementation plan](../groq-migration-plan.md) and
[setup, privacy and usage limits](../ai-analysis.md) for details and official sources.

## Amendment (2026-09-26)

- After a Codex review: the key is read only when a request is needed. "requests" counts
  HTTP attempts. A stated time zone is used only when the email writes it, and a phrase
  naming an unreported zone keeps only the date. Stored evidence is at most 300 characters
  and never a whole body (schema version 3). Legacy deadline rows load without being
  hidden. Calls are paced when the token window is smaller than the last call.
