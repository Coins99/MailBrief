# M9 live validation — owner checklist

Status: pending. Implementation and synthetic checks are separate from this acceptance.
The owner runs these checks; ordinary tests and package smoke mode use no live account,
credential vault or paid AI. Do not mark M9 accepted until these results are recorded.

## Before starting

Use the native package for your platform. Keep a separate private backup and a writing
export before testing recovery or permanent cleanup. Enable Groq Zero Data Retention
before approving transmission of real email. Keep filled notes outside the repository;
record counts and outcomes here, not credentials, bodies or private writing.

## Complete email scenario

- [ ] Connect the intended Gmail account with read-only permission; restart and verify
  silent sign-in restores the same account.
- [ ] Brief a day requiring multiple Inbox pages. Manually include an omitted message;
  check coverage, time zone, source links and displayed dates.
- [ ] Accept two actions from one message. Edit dates, notes and steps, dismiss another
  suggestion, then regenerate and restart. Verify decisions and edits persist, with
  no duplicate action.
- [ ] Save a reply, note and message. Edit and checkpoint versions, restart, copy and
  export them. Check exact text, recipients as typed, Unicode and line breaks.
- [ ] Review a later reply proposing a changed deadline. Apply only the intended change;
  verify other edits survive and an unfinished action carries into tomorrow.
- [ ] Exercise offline operation, cancellation, expired sign-in and partial AI failure.
  The last good brief and all writing must remain available.
- [ ] In **Data and recovery**, back up and verify. Restore into a clean, separate test
  profile first; reconnect credentials separately. Check real actions, completed and
  deleted writing, steps, notes, drafts, versions, source snapshots and preferences.
  Automatic AI permission must be off. If testing replacement, locate and verify the
  retained pre-restore database before disposing of any copy.
- [ ] Preview cache cleanup and cancel; confirm nothing changed. In the test profile,
  apply cleanup and account removal; writing and source snapshots must survive.
  Try permanent deletion only on disposable deleted writing, after backing up.
- [ ] Check Tab/Shift+Tab, mnemonic buttons, Enter and Escape; try native file pickers,
  recovery confirmation, small windows, and 100%/200% display scaling.
- [ ] Inspect test-profile database/logs for credentials and a distinctive synthetic
  incoming-body marker. The full incoming body must not be retained; Gmail's preview
  snippet is an intentional exception. Review the package audit result.

## Personal-use journal (7–14 days)

Keep a small labelled sample, including messages omitted by the shortlist. Track
important missed mail, wrong dates, duplicate actions, usefulness of drafts, latency
and AI request/token usage. Avoid private content in the journal.

| Day | Important mail missed | Wrong dates | Duplicate actions | Useful drafts / tried | Brief latency | AI requests / tokens | Issue reference |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | | | | | | | |
| 2 | | | | | | | |
| 3 | | | | | | | |
| 4 | | | | | | | |
| 5 | | | | | | | |
| 6 | | | | | | | |
| 7 | | | | | | | |

Extend to 14 days if the sample is too small or issues recur. Record misleading
deadline/completion proposals and whether copy/export makes optional M10 unnecessary.

## Acceptance record

- Package version / commit and platform:
- Dates used and sample size:
- Clean-profile recovery result:
- Remaining issues:
- Accepted by / date:

Stop acceptance for data loss, credential/full-body leakage, silent edit overwrites,
unintended provider writes or duplicate acceptance. Fix and rerun the affected checks
before declaring M9 complete.
