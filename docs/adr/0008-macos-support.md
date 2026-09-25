# ADR 0008: Support macOS alongside Windows

- Status: Accepted
- Date: 2026-09-25
- Amends: ADR 0006

## Context

Development agents run on macOS, but Gmail credentials could only be stored in Windows
Credential Manager. Gmail therefore could not sign in on the Mac, and every live check
needed a separate Windows PC. The owner also wants to use MailBrief on both machines.

## Decision

Windows and macOS are both supported platforms. Gmail refresh credentials live in Windows
Credential Manager on Windows and in the macOS Keychain on macOS. The backend is chosen
explicitly by platform; keyring auto-selection, plaintext files and other platforms remain
unsupported. CI runs the quality gates on both. Until M5 both run from source; M5 produces
a Windows `onedir` package and a macOS app bundle with PyInstaller, each built on its own OS.

## Consequences

- Live Gmail checks can run on either machine. Each machine keeps its own sign-in and its
  own local database.
- CI takes longer, and every change must pass on both platforms.
- The first macOS app is unsigned, for personal use. It may need a one-time approval to open,
  and the Keychain may ask for access again after a rebuild. Apple signing and notarization
  are a separate, later decision.
