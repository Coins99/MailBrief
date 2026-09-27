# M5: remaining platform and human acceptance

The implementation and automated Windows checks are complete. These checks require
macOS, a real account, a clean user profile or human judgement; they have not been
marked passed by synthetic tests. M5 is not accepted until they are recorded here.

## macOS

- [ ] Run the macOS CI quality/build/package-check job and inspect its result.
- [ ] Launch `MailBrief.app` on a Mac without the source checkout or Python environment.
- [ ] Configure a Desktop OAuth client and Groq key through Settings; verify Keychain
  access, persistence after restart, key removal and reconnect.
- [ ] Complete the daily workflow and source-link checks below on macOS.

## Packaged real-account workflow (Windows and macOS)

- [ ] In a clean OS profile, launch the complete package folder/app bundle and configure
  Settings. Verify startup does not require the development environment.
- [ ] Connect Gmail and sync. Verify automatic selections, add a message outside the
  suggestions, remove another, and confirm the selected set is the one analyzed.
- [ ] Enable Groq Zero Data Retention. Read the transmission disclosure, decline once,
  then approve a small selection. Check summaries/actions/deadlines against the source.
- [ ] Repeat with unchanged messages and confirm cached results need no retransmission.
- [ ] Open sources while multiple Google accounts are signed into the browser; verify
  each source opens the correct account and thread.
- [ ] Close and restart. Confirm saved brief, settings and Gmail session restore.

## Recovery and usability (human verification)

- [ ] Go offline after syncing. Reopen the app, read the saved brief and browse cached
  account/date pages. Confirm stale metadata is clearly labelled and retry recovers.
- [ ] Cancel during sync, review, consent and AI work; verify responsiveness and that
  the displayed saved brief remains available. Close during an operation and relaunch.
- [ ] Exercise expired/revoked Gmail authorization, missing/rejected Groq key and
  partial/rate-limited results; verify the messages and recovery actions are understandable.
- [ ] Use keyboard-only navigation, Space selection and Tab focus; inspect readability
  and scrolling at the display scaling used on each real device.
- [ ] Finish any remaining owner checks in [M4 validation](m4-validation.md).
- [ ] Review and merge the M5 pull request once the required checks pass.

Record results with date, OS, package/commit and pass/fail notes. Do not attach API
keys, OAuth files, raw bodies or screenshots containing private mail to the repository.
