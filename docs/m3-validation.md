# M3 validation record

Implementation: PR #11, merged at `83e8536`. Usage and checklist: [Gmail bodies](gmail-bodies.md).

## Scope

- Live checks of the code at `83e8536` on the owner's Mac, 25 September 2026, with real mail.
- Synthetic re-checks on 26 September 2026 on `feat/m4-analysis-digest`.

Results are owner-reported. To keep this public record free of private data, it gives
generic descriptions and character counts only: no senders, subjects, addresses or email
text.

## Results

| # | Check | Result |
| --- | --- | --- |
| 1 | Reply in a thread | Pass. Two replies, 196 → 2 and 566 → 23 characters, both marked "quoted history removed" and keeping only the new text. The second passed after a fix for quoted history that contained a forward marker. Synthetic re-check on 26 September: 257 → 55, pass. |
| 2 | Forward | Pass. 8,018 → 3,090 characters after link cleanup; the forwarded content was kept. Synthetic re-check: pass. |
| 3 | Newsletter or marketing email | Pass. Four real newsletters, for example 5,585 → 1,492 and 4,124 → 1,536 characters. Readable text; tracking links, invisible padding and HTML leftovers removed. |
| 4 | Non-English email | Pass. Chinese text displayed correctly, in real mail and in a synthetic re-check. |
| 5 | Attachments | Pass. Body text only (82 → 80 characters). Attachment-only emails came back empty, with attachments counted and never read. |
| 6 | Long email | Pass. Two real emails, 5,272 → 3,844 and 6,247 → 3,845 characters, cut at a line or word break at the default limit of 4,000 (26 September). The 25 September test email (7,348 characters) was under the limit then in force (8,000), so it did not exercise truncation. The limit is configurable. |
| 7 | Image-only email | Pass. Came back empty, with no error. |

Across all runs, sync completed, nothing crashed, and no email text was saved or logged.

## Problems found during testing and fixed

- Plain-text versions kept invisible padding characters and HTML leftovers.
- Link cleanup could leave a stray "[[link]" marker.
- Quoted history that contained a forward marker was kept as new text.
- Deeply nested quotes could exhaust memory.
- Attached emails were read as body text.
- Failed downloads were reported as empty bodies instead of failures.
- Whitespace could use up the text limit.
- Four crafted inputs caused slowdowns or crashes.

## Known limitations

These are still open; a follow-up PR is planned after M4.

- R1: a forward with a non-English separator line and a changed subject loses its
  forwarded content.
- R2: a plain-text version that is mostly tracking links is chosen over a rich HTML version.
- R3: preformatted (`<pre>`) text is flattened onto one line.

## Automated results at `83e8536`

- 555 tests passed on Linux. On macOS, 553 passed and 2 were intentionally skipped.
- Coverage: about 94.6%.
- Ruff and strict mypy were clean for all three platforms.
- Every pushed commit passed CI on Windows and macOS.
