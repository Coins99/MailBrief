# ADR 0013: AI drafting from context the owner chooses

Date: 2026-09-28. Status: accepted.

Builds on ADR 0010 (Groq), ADR 0012 (drafts and notes) and the M4 consent rules.

## Context

M7 stage 2 lets Groq write or rewrite a draft. A draft can draw on an email, an action and
the owner's own text, so the owner must see exactly what leaves the device, approve it each
time, and keep their own words. The AI stays untrusted: an email may try to steer it, and it
may invent recipients, facts or quotes.

## Decision

- **Sent, only when the owner ticks it for that generation:**
  - the email being replied to: its subject, the sender's **name only** (never the
    address), the received time in the owner's zone, and its body. The body is downloaded
    when the generation starts, prepared like the brief's (the same length limit, quoted
    history trimmed) and never stored;
  - the linked action: title, whose it is, target date, deadline phrase, steps, and at most
    2,000 characters of notes;
  - the draft's current title and body, at most 8,000 characters.
  - Always sent: the draft's kind, the tone, the length, the owner's instructions (at most
    1,000 characters) and today's date.
- **Never sent:** the sender's address, To or Cc, other emails, attachments, the owner's
  other drafts or actions, Gmail or MailBrief IDs, or credentials. The parts above are sent
  as written, so any addresses, links or numbers inside them (in an email body, the current
  text or action notes) are sent too; the disclosure says so.
- **Approval every time.** Each generation shows a preview of every part with its character
  count and waits for approval. The first use also needs explicit consent to a disclosure.
  That consent is recorded per provider for the owner (not per account) and disclosure
  version before anything is sent, and can be revoked with the brief's consent. Declining
  sends nothing. Version 2 corrected what the disclosure says is never sent.
- **Output:** an optional subject, a body and a list of missing context. There are no
  recipients: the schema has no field for them.
  - Python cleans the text (control and format characters removed, blank lines tidied) and
    removes quoted history.
  - Python rejects a draft that copies 200 or more characters of the source email word for
    word, compared after Unicode normalization, case folding and whitespace collapsing.
  - Facts the AI lacks stay as `[[placeholders]]`, and the missing context is listed.
  - A subject is used only for emails and notes, and only when the draft has no title yet.
- **Storage:** the owner's text is saved as a version before the call. A generation becomes
  a new "generated" version with a record of its provider, model, prompt version, tone,
  length, parts, instructions and missing context. Generated text follows ADR 0012.
- **Failure:** a failed, declined or cancelled generation leaves the draft's text unchanged.
  An unusable answer is retried once.
- **`json_validate_failed`:** this Groq HTTP 400 means Groq couldn't finish a valid answer,
  usually because it hit the output limit. It counts as an incomplete answer (retried once,
  like a cut-off answer), not as a rejected request, for briefs and drafting alike.

## Consequences

- Migration 0008 adds `owner_consents` and `draft_generations`. Pruning a version removes
  its generation record; downgrading below 0008 drops both tables.
- Drafting consent is separate from the brief's per-account consent: agreeing to one does
  not agree to the other, and revoking AI consent revokes both.
- A brief whose messages fail only because answers were incomplete reports
  `AI_OUTPUT_INCOMPLETE` with advice about the output limit, instead of a rejected request.
