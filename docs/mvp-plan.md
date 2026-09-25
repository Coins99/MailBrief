# Gmail-first delivery sequence

Updated: 2026-09-25. The detailed source of implementation work is
[email-implementation-plan.md](email-implementation-plan.md), including file
changes, dependencies, tests and acceptance gates.

Build for personal desktop use first. The website is a later server-hosted
product, after completion of the desktop ecosystem.

| Milestone | Outcome | Release |
| --- | --- | --- |
| M0 | Verify existing code/test baseline | Email MVP |
| M1 | Gmail OAuth, secure restoration and diagnostic | Email MVP |
| M2 | Inbox metadata sync, pagination and shortlist | Email MVP |
| M3 | Selected-body decoding and minimization | Email MVP |
| M4 | Consented structured analysis and saved brief | Email MVP |
| M5 | Connected desktop workflow and Windows package | Email MVP gate |
| M6 | Accepted actions, target dates and editable plans | Complete email |
| M7 | Persistent local email/note/message drafts | Complete email |
| M8 | Thread follow-up, history and daily operation | Complete email |
| M9 | Backup, recovery and personal-use validation | Complete email gate |
| M10 | Explicitly save reviewed drafts into Gmail | Optional extension |

Proceed in order with reviewable changes and keep the Microsoft suite passing.
M1 authentication is implemented; the owner verified sign-in, restoration and
disconnect/reconnect. M2 metadata sync and ranking are implemented; see
[metadata usage and acceptance](gmail-metadata.md). Google-side revocation/recovery
remains an M1 live check. The [M1 validation](m1-validation.md) records its original baseline.
No feature is marked implemented merely because it appears in this plan.

Main was reconciled with the Gmail line on 2026-09-25 and is again the integration
branch; new work branches from `main`.

Next: complete remaining live acceptance checks, then M3 shortlisted-body retrieval.
General tasks, calendar, analytics and website directions are intentionally rough
in [ecosystem-roadmap.md](ecosystem-roadmap.md).
