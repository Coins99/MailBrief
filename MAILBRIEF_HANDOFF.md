# MailBrief Agent Handoff

## 1. Current Status & Context
**Branch:** `main` (Fully up to date and clean)
**CI Status:** 🟢 Passing (Linting, typing, and 294 unit tests passing with >92% coverage).

You are picking up the MailBrief project exactly halfway through a major architectural update. We have completed **Sections 1–4** of the refactor, covering robust HTTP retry logic, decoupled sleep telemetry, complete MSAL Token Cache lifecycle management, and typed OpenAI error classifications.

## 2. The Current Blocker (The Gate)
We are currently **PAUSED** waiting for the human user to run the **Live Microsoft Diagnostic Checks**.

Because agents run in a headless sandbox without a graphical WindowServer or the user's `MAILBRIEF_MICROSOFT_CLIENT_ID`, **the agent CANNOT execute the OAuth browser flow or test the live macOS Keychain integration.**

**Do not attempt to run the diagnostic checks yourself.** Wait for the user to provide the output of these 4 commands:
```bash
mailbrief-ms-diagnostic fetch
mailbrief-ms-diagnostic fetch --silent-only
mailbrief-ms-diagnostic disconnect
mailbrief-ms-diagnostic fetch --silent-only
```
If the user provides successful output proving that the cache successfully persists and purges in the host environment, you may proceed to the next phase.

## 3. Work Already Completed (Do Not Repeat)
- **HTTP Retry & Duration Parsing:** Fixed regex parsing (`_DURATION_RE`) and `parse_openai_ratelimit_reset` to correctly extract retry limits and drop unbounded exponential notation.
- **Sleep Telemetry:** Removed `accumulated_sleep_seconds` from the domain `MessagePage`. It is now managed via a `RetryTracker` and `ContextVar` inside `sync.py`.
- **MSAL Keychain Purge:** Fixed a critical bug where `purge_credentials()` was unlinking the cache file but failing to wipe the underlying Keychain persistence. (Also fixed the Windows DPAPI CI test failures that were crashing the suite).
- **Error Classification:** Implemented robust translation of OpenAI API response codes (401, 403, 429) into typed domain errors (e.g., `ProviderPermissionError`, `ProviderRateLimitError`, `AIAuthenticationError`).

## 4. Next Phase: Section 5 / Task 16 Implementation
Once the live diagnostic checks are verified by the user, you are tasked with implementing **Task 16**: building out the AI Adapter and Structured Outputs logic. 

**Key Requirements for Task 16:**
- **Opaque Hex Keys:** When batching messages to the LLM, use `secrets.token_hex(4)` (e.g., `a3f9e2b1`) for message keys to avoid sibling-injection attacks. Do not use sequential keys like `msg_1`.
- **Python-Side Date Resolution:** Do **not** ask the LLM to output `deadline_at_utc`. Ask it for a `deadline_text` span and `deadline_precision`, then calculate `deadline_at_utc` in Python against the message's `received_at_utc`.
- **Python-Side Validation:** Enforce string `maxLength` (240) and float ranges (0.0 - 1.0) on the Python side, as OpenAI's strict mode JSON schema rejects `maxLength` and `maximum` keywords.
- **Batch Processing & Fallbacks:** Cap `ai_batch_size` at 5. Truncate bodies at 8,000 characters (marking `body_truncated: True`). If batch validation fails, cleanly trigger the split-to-singles fallback logic. Drop `ranking_reasons` from the LLM prompt.

## 5. Instructions for the Next Agent
1. Read this file.
2. Review the most recent `task.md` or `implementation_plan.md` in the artifacts directory for deep technical details.
3. Wait for the user to paste the terminal output for the diagnostic checks.
4. If the diagnostic checks pass, begin implementing Task 16 following the constraints listed above.
