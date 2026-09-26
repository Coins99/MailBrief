# M4 validation

Date: 2026-09-26. Working branch: `feat/m4-analysis-digest`, based on draft PR #12.

## Groq migration

Implemented locally on the existing branch at the owner's request, preserving the
prior integration fixes and request-limit work. No commit or push was performed.
The owner subsequently ran live checks, recorded below.

- Groq Chat Completions via pinned httpx transport, strict JSON Schema and local
  Pydantic/domain validation; OpenAI SDK and runtime adapter removed.
- Groq-specific OS-vault key storage, configuration, consent and CLI messages.
- Accurate ZDR disclosure, without claiming to verify the console setting.
- Default batch size 1, prepared body limit 4,000 characters, output limit 2,000 tokens
  and 10 HTTP attempts per run, including retries.
- Exhausted-quota pacing, bounded retries, terminal organization spending-limit errors
  and partial-result preservation.
- Historical OpenAI keys, consent, analyses and saved data are preserved. Migration
  tests verify separate credentials, fresh consent, separate cache identities and
  continued readability of a previously saved OpenAI brief.
- Key setup and the accepted organization-wide spending-limit instructions are in
  [ai-analysis.md](ai-analysis.md). Architecture decision: [ADR 0010](adr/0010-groq-ai-provider.md).

## Automated verification

Windows, Python 3.13.15, dependencies from `uv.lock`:

- Initial focused Groq, analysis, brief, configuration and CLI tests: 159 passed.
- Final full suite: **880 passed, 1 skipped** (Unix-domain-socket test on Windows).
- Overall branch coverage: **95.44%**; synchronization 92%, ranking 97%, bodies and
  digest 100%; Groq provider 96%.
- Ruff formatting and lint: passed.
- Strict mypy: passed for native, Windows and macOS targets (138 source files each).

Tests use mocked HTTP, synthetic messages and an in-memory credential vault. The full
run reported 21 MSAL deprecation warnings and one SQLAlchemy connection-cleanup warning,
with no failures. The prior environment had locked package files, so verification used
`UV_PROJECT_ENVIRONMENT=out/groq-venv` and `uv sync --locked --all-groups --link-mode copy`.
Test temporary files, cache and coverage output are under ignored `out/`.

The pre-migration baseline had 868 passing tests and 95.40% coverage. Its fixes for
attempted-request reporting and per-run limits remain covered by the Groq tests.

## Owner live acceptance: partially verified

Owner-provided terminal output on 2026-09-26 confirms a successful live Gmail sync,
Groq analysis and cached rerun in `America/New_York`:

- Initial metadata check: complete, one page, one retrieved and selected message,
  zero failed items.
- Live brief: complete, two shortlisted and analyzed messages, zero failures or skips.
  Provider/model: `groq / openai/gpt-oss-120b`; reported input/output tokens: 2317 / 1051.
- Repeat brief: complete, two items, zero analyzed, two reused, zero failures or skips;
  reported `AI: nothing sent this run`.

The owner also confirmed reviewing summary/deadline accuracy and enabling Groq ZDR.
This verifies the main live workflow for the sample. Quota pacing and the remaining
edge-case acceptance checks are not yet recorded. No email content or credentials
are recorded here.

| Check | Live result |
| --- | --- |
| Create/save Groq key and configure `MAILBRIEF_GROQ_MODEL` | Working configuration demonstrated by successful live brief |
| Enable ZDR in Groq Console Data Controls | Confirmed by owner |
| Synthetic model/schema/token-usage smoke test | Separate synthetic test not recorded; live brief accepted output and reported usage |
| Decline first-use consent; no AI request | Pending |
| Consent, generate and view brief; verify Gmail links | Generation passed; owner confirmed summary/deadline accuracy; consent interaction and links not independently confirmed |
| Repeat run reuses cached analyses | Passed: analyzed 0, reused 2, no AI transmission reported |
| Exact, date-only and unresolved deadlines | Owner confirmed sample deadline accuracy; coverage of all three variants not specified |
| Email instructions remain untrusted content | Pending |
| Revocation blocks new transmissions | Pending |
| Small request budget preserves a partial brief | Pending |
| Wrong key gives a recovery hint without leaking it | Pending |
| Body marker absent from database files | Pending |

Complete the [live acceptance checklist](ai-analysis.md#live-acceptance-checklist)
before marking M4 accepted. Desktop UI integration and packaging remain M5.
