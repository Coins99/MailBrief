# Later ecosystem: rough designs and guidelines

Updated: 2026-09-25. Provisional directions, not committed specifications.
Finish the [personal email workspace](email-implementation-plan.md) first.
Build the entire desktop ecosystem before implementing the server-hosted website.

## Product structure

Possible navigation: Today, Mail, Tasks, Calendar, Notes, Insights, Settings.
Only expose a destination when it has useful functionality. Share a detail pane
for sources, actions, drafts and notes instead of copying state between modules.

```text
Email / manual capture / note
              |
         accepted task ---- action steps
           |       |
         notes    work blocks ---- calendar
           |
       optional draft

Task and work-block changes -> local activity records -> insights
```

A task may have several sources and work blocks. An event may have no task.
Notes/drafts can stand alone. Source deadline, chosen target date and scheduled
time remain distinct; moving work must not silently change a deadline.

## 1. General tasks and projects

Reuse accepted email actions as the initial task model. Gradually add manual
quick capture, project grouping, filters, lightweight priority, waiting state,
recurrence and optional dependencies. Avoid a second competing task table.

Candidate views: Today with a chosen workload, capture Inbox, project detail,
open/waiting/completed. Include keyboard capture and undo. Due dates stay optional
so users need not invent deadlines to keep tasks visible.

Potential gate: organize a week of manual and email-derived work without duplicate
entry. Decide recurrence, parent/subtask completion, archival and effort-estimate
behavior based on actual use. Only explicitly required dependencies should block
scheduling.

## 2. Clean, adjustable internal calendar

Start with week view, daily agenda and an unscheduled-task sidebar. Add manual
events and busy blocks before automatic scheduling. Internal availability is
incomplete until the user enters commitments held in other calendars.

Interaction guidelines:

- Drag, resize and move work blocks; provide keyboard alternatives and undo.
- Distinguish fixed events, flexible work and deadline markers with labels as
  well as restrained colors. Reveal detail on selection rather than crowding cells.
- Support multiple sessions per task, unscheduling without task deletion, working
  hours, breaks, buffers and locked blocks.
- Preserve timezone/date-only semantics; test travel and DST.
- Preview all proposed changes before applying bulk rescheduling.

Begin scheduling with deterministic constraints: available hours minus fixed
events, duration, dependencies, deadline and preferences. AI can propose/explain
options; a constraint checker must verify them. Never move a locked commitment or
change a deadline just to make a schedule fit. Show conflicts and alternatives.

Potential gate: plan a week, add an unexpected meeting, and review a feasible
rescheduling proposal while retaining control. External calendar adapters can
follow, read-only first; define edit ownership/conflicts before external writes.

## 3. Notes and reusable drafting

Expand email notes into a lightweight searchable editor with task/project links
and Markdown/text export. Start with modest formatting. Rich text, attachments
and collaboration can wait.

Reuse drafting for project updates, meeting preparation and messages. The user
selects context; regeneration creates recoverable revisions. Approval to analyze
one email is not approval to transmit an entire linked project.

Potential gate: notes/drafts remain findable and exportable after clearing their
source-mail cache. Decide organization and search depth through personal use.

## 4. Analytics for better planning

When task/calendar features arrive, record a small local history: task creation,
acceptance, completion/reopening, rescheduling and optional explicit work-session
start/stop. Include IDs, origin, timestamp and schema version. This does not
require rebuilding the application around event sourcing.

| Question | Candidate measure | Limitation |
| --- | --- | --- |
| Am I overplanning? | Planned workload versus completed work | Counts ignore differences in task size |
| What keeps slipping? | Carryover and rescheduling frequency | Rescheduling is not inherently failure |
| Are estimates useful? | Estimated versus explicitly recorded actual duration | Calendar blocks are not actual measured work |
| Where does work originate? | Accepted tasks by source/project | Email volume is not productivity |
| Is assistance useful? | Suggestions accepted/edited/dismissed; draft reuse | Copying is not evidence of sending |

Begin with a weekly review and a few explainable charts. Avoid a single
productivity score, hidden monitoring or mandatory time tracking. Allow export
and reset. Decide whether actual-time tracking is worth its input burden.

## 5. Desktop completion gate

Before website implementation, the desktop should independently handle email,
tasks, internal calendar, notes, useful basic insights, preferences, saved-data
offline access and backup/restore. Validate the combined workflow for several
weeks and stabilize data semantics/migrations.

Keep services modular and data portable now. Do not introduce hosting, browser UI,
an HTTP layer or distributed jobs into the email milestones solely for later
compatibility. Reuse domain rules, contracts and tests; do not assume Qt widgets
will transfer to a web UI.

## 6. Website and server, much later

The website should eventually work while the desktop is off. A server therefore
needs durable state and independent job execution; a browser connected only to a
running desktop would not meet the intended outcome.

| Option to evaluate later | Benefit | Main decision/cost |
| --- | --- | --- |
| Server-authoritative data plus desktop offline cache | Central state and independent web operation | Define offline edits and conflict recovery |
| Local desktop data plus bidirectional server sync | Strong offline autonomy | Complex merging, deletions and duplicate prevention |
| Snapshot import/export as a transition | Simple private web trial | Not ongoing sync; divergent edits need care |

No storage/sync option is selected now. Choose after observing desktop usage.
For eventual seamless use, evaluate the first two against maintenance appetite.

Future server design checklist:

- API authentication, authorization, device/session revocation, TLS and server
  secrets separate from desktop credentials.
- Durable database, migrations, monitoring and backups with demonstrated restores.
- Appropriate web OAuth flow and renewed review of Gmail/cloud-processing data
  requirements; do not copy a desktop credential-store file to a server.
- Ownership of sync and AI jobs so desktop and server cannot duplicate briefs,
  drafts or charges.
- Stable IDs, revisions, retry-safe writes, deletion/tombstone rules, offline
  queues and visible conflict resolution for user text.
- Timezone ownership, missed jobs, notifications, browser offline behavior,
  export and shared-computer handling.
- Decide which mail-derived data may leave the desktop. Encryption in transit
  and at rest differs from end-to-end encryption; server-side AI/jobs need an
  explicit compatible data-access design.

Possible sequence: private server prototype → backup/manual import → web
read/edit parity → chosen synchronization design → independent jobs → extended
personal use. Public distribution and collaboration require a separate scope.

## Decisions deliberately left open

1. Which project/task organization proves useful in daily use?
2. Should scheduling favor deadlines, long focus blocks or flexibility?
3. Is actual-time tracking wanted, or are weekly trends sufficient?
4. Are local drafts enough, or is Gmail draft saving worth its complexity?
5. Must the eventual browser support offline editing, or only the desktop?
6. What server data custody and maintenance are acceptable?

Resolve these when their feature begins. They do not block email implementation.
