# OpenAI to Groq migration plan

Status: implemented locally; automated verification and live acceptance tracked in
[m4-validation.md](m4-validation.md). Reviewed 2026-09-26.

## Outcome and baseline

Make Groq the AI provider for MailBrief's consented daily brief, with secure key
storage, validated extraction, bounded requests and free-tier operation. Keep the
existing Gmail, ranking, evidence checking, deadline handling and digest pipeline.
Desktop UI work remains in M5.

Implementation note: the owner subsequently requested implementation on the current
working tree; existing local changes were preserved and no commits or branch switches
were made. Live acceptance remains pending.

The baseline is draft PR #12 (`feat/m4-analysis-digest`) plus the local integration
fixes and request-limit work. Preserve and checkpoint those changes before migration.
Follow the repository workflow: integrate the baseline, then branch from updated
`main` for one migration PR. Do not reset the existing working tree to start this work.

## Provider design

- Add `providers/groq/{provider,factory,credentials}.py`, implementing `AIProvider`.
- Use existing `httpx` directly against the pinned HTTPS endpoint
  `https://api.groq.com/openai/v1/chat/completions`. Disable environment proxies and
  redirects; retain explicit timeouts, resource cleanup and bounded retries.
- Start with `openai/gpt-oss-120b`, configurable through `MAILBRIEF_GROQ_MODEL`.
  This model runs on Groq; its name does not require an OpenAI API account.
- Send the existing extraction instructions and minimized email payload as messages.
  Request strict JSON Schema through `response_format`; retain all required fields
  and `additionalProperties: false`. Validate returned JSON with Pydantic, then run
  the existing domain, evidence and deadline checks.
- Map `choices[0].message.content` and prompt/completion token usage into existing
  domain responses. Reject refusal, truncation, absent content, malformed envelopes,
  unexpected finish reasons and invalid schema without persisting partial JSON.
- Keep tools, streaming, file uploads and provider batch jobs disabled. Local batches
  mean multiple emails in one inference request, not Groq's persistent Batch API.
- Give the Groq prompt its own version. Keep provider JSON and parsing in the adapter.

Groq documents strict structured output support for this model in its
[Structured Outputs guide](https://console.groq.com/docs/structured-outputs).
The [API reference](https://console.groq.com/docs/api-reference) specifies
`max_completion_tokens` for the output bound. This is an explicit adapter migration:
changing the OpenAI base URL alone is insufficient. In particular, Groq's
[Responses API](https://console.groq.com/docs/responses-api) does not support the
current adapter's `store` request parameter.

## Credentials, consent and saved data

1. Store the Groq key only in Windows Credential Manager or macOS Keychain under
   `MailBrief.Groq`, separate from `MailBrief.OpenAI`. Use hidden key entry and static,
   sanitized errors. Never read an OpenAI key as a Groq credential.
2. Point the existing `ai-key` commands at Groq and update their help/status text.
   Document creating a key in Groq Console, entering it in the hidden prompt and
   checking key status. Do not put secrets in environment setup examples or files.
3. Require fresh consent for provider `groq`. Update transmission previews,
   consent status/revoke and success/error labels. Keep `--yes` unable to bypass
   first-use consent.
4. Replace the current `store=false` disclosure with accurate Groq privacy wording.
   Before live mail acceptance, the owner enables Zero Data Retention (ZDR) in
   Console Data Controls. The application must not claim it verified that remote
   setting unless a supported verification mechanism is implemented.
5. Preserve OpenAI analyses, saved briefs and consent records. New analyses use
   `groq` in the existing provider/model/prompt/schema/input cache identity; old
   results are not relabeled or reused as Groq results. No database migration is
   expected because these identities are already stored generically.
6. Retain data minimization and untrusted-email safeguards. Send no credentials,
   account/message identifiers, source links or attachments; log no bodies or raw
   provider errors. Leave old vault entries intact and document optional cleanup.

Groq allows all customers to enable ZDR. Without that setting, reliability/abuse
logs can retain inputs and outputs for up to 30 days, with legal exceptions. Usage
metadata is still retained under ZDR. See [Groq data controls](https://console.groq.com/docs/your-data).

## Usage limits and failure behavior

Start with these conservative defaults, then validate them using synthetic emails:

| Setting | Proposed value | Scope |
| --- | --- | --- |
| `MAILBRIEF_GROQ_MODEL` | `openai/gpt-oss-120b` | Groq model |
| `MAILBRIEF_AI_MAX_REQUESTS_PER_RUN` | `10` | All HTTP attempts, including retries |
| `MAILBRIEF_AI_BATCH_SIZE` | `1` | Emails per inference request |
| `MAILBRIEF_AI_BODY_CHARACTER_LIMIT` | `4000` | Prepared body per email |
| `MAILBRIEF_AI_MAX_OUTPUT_TOKENS` | `2000` | Maps to `max_completion_tokens` |

Keep one provider instance/budget for a whole brief run; an HTTP reconnect must not
reset it. Cached analyses consume no requests. Budget exhaustion stops new calls,
preserves validated results and produces a clear partial-brief message. These are
local per-run controls, not a monthly allowance or a quota shared across processes.

The documented free limits for the proposed model are 30 requests/minute, 1,000/day,
8,000 tokens/minute and 200,000/day. Limits are organization-wide and model-specific;
the account dashboard is authoritative. See [Groq rate limits](https://console.groq.com/docs/rate-limits).
Character limits do not guarantee a token fit: include prompt/schema overhead and
measure synthetic requests before accepting the defaults.

Use response quota/reset headers to pace sequential calls. Handle 429 and transient
server/network failures with bounded retries and valid `Retry-After`; do not retry
sooner than requested. If the wait exceeds the existing 30-second retry ceiling,
stop with a resumable partial brief. Authentication, invalid model/schema, oversized
requests and exhausted local budgets are terminal. Classify HTTP 400
`blocked_api_access` as a terminal billing limit, not malformed model output.

For the requested monthly protection, stay on the free plan initially. If paid usage
is enabled later, configure a monthly USD limit in Groq Console Settings > Billing >
Limits, with alerts. Groq's limit is organization-wide, not per key/connection, and
spend tracking is delayed 10-15 minutes, so some overshoot is possible. Do not label
it an exact per-key hard cap. See [Groq spend limits](https://console.groq.com/docs/spend-limits).
The owner accepted the organization-wide scope. No per-key monthly ledger is required.

## Implementation sequence and files

1. **Adapter and contract tests:** add the Groq adapter, factory and vault store;
   port useful OpenAI regression cases into `tests/unit/providers/groq/`. Preserve
   request counting, safe parsing and sanitized error behavior from the local fixes.
2. **Application switch:** update `config.py`, `diagnostics/gmail.py` and
   `services/brief.py`; add a Groq classifier in `infra/http_retry.py` without changing
   Gmail/Microsoft retry semantics. Reuse provider-neutral errors and analysis logic.
3. **Consent and integration tests:** verify fresh Groq consent, refusal, revoke,
   cached reruns, budget exhaustion, partially successful runs and accurate reporting
   of attempted transmission even when a request fails.
4. **Retire OpenAI runtime:** remove the unused OpenAI adapter/configuration and SDK
   dependency; update `pyproject.toml` and `uv.lock` together. Migrate relevant tests
   before removing old adapter tests. Never fall back to OpenAI on Groq errors.
5. **Documentation:** update `AGENTS.md`, `README.md`, `docs/ai-analysis.md`, the M4
   validation checklist and active milestone plans. Add ADR 0010 superseding the
   OpenAI provider decision; preserve historical ADRs. Explain that old
   `MAILBRIEF_OPENAI_MODEL` configuration must be replaced.

## Acceptance and rollout

- Mock all network traffic in automated tests. Assert the Groq destination, headers,
  strict schema and minimal payload; cover invalid JSON, refusal, truncation,
  unsupported models, 401/403, 413, 429, 5xx, timeouts and billing blocks.
- Test retries consuming the same budget, no sleep/retry after exhaustion, safe
  cancellation/cleanup, and no key/body/provider-error leakage.
- Verify historical OpenAI briefs still open, Groq cache identities are separate,
  and a repeated identical Groq run performs zero inference calls.
- Run Ruff formatting/lint, strict mypy for native/Windows/macOS and full pytest
  with repository coverage thresholds. Keep dormant Microsoft checks passing.
- First perform an explicitly initiated synthetic live smoke test using a Groq key:
  confirm actual schema acceptance, model availability, token usage and pacing.
  Tune output/body limits if extraction truncates; never silently accept partial JSON.
- Then perform M4 acceptance on a small shortlist after key setup, ZDR configuration
  and explicit in-app consent. Confirm useful summaries and evidence-backed deadlines,
  then a cached rerun and a deliberately small request budget.
- Record sanitized outcomes in the M4 validation checklist. Merge only after automated
  checks and live acceptance pass. If live acceptance cannot be performed, leave it
  explicitly pending rather than declaring the integration validated.

Rollback is a code revert to the preserved baseline, not automatic provider failover.
Existing vault entries and database rows remain intact. Returning to OpenAI requires
explicit configuration and valid provider consent before any transmission.
