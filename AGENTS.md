# MailBrief workspace guide

Read only the files needed for the current task.

- Product boundary: `docs/mvp-scope.md`
- Current sequence: `docs/mvp-plan.md`
- Exact Gmail migration surface: `docs/change-map.md`
- Provider contract: `src/mailbrief/ports/email_provider.py`
- Gmail work: `src/mailbrief/providers/gmail/`
- Dormant Outlook work: `src/mailbrief/providers/microsoft/`

Gmail is the default MVP provider. Preserve Microsoft source, tests, diagnostic,
configuration, dependencies, cache path, and stored provider values. Do not read
the Microsoft package for Gmail-only work unless a shared contract is affected.

Keep provider payloads inside their adapter. Core services consume normalized
domain models. Never store message bodies or OAuth tokens in SQLite or logs.
Run focused tests first, then the full quality commands from `README.md`.
