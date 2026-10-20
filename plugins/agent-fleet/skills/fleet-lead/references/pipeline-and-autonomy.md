# Pipeline, gating and autonomy rules

Extends [fleet-lead](../SKILL.md). The operator format and launch flags it cites
live in the fleet-setup [operator surface](../../fleet-setup/references/operator-surface.md)
reference.

## Pipeline table

Every open work item, live or parked, is one row in `<pipeline-file>`: a
markdown table under an `updated: <time>` line, with columns
`item | stage | state | since | next stage | owner | trigger or blocker | wake`.
It lives outside any one leader's memory so it survives succession, and the
sweep re-reads it every tick. The leader is its only writer. Operator decisions
stay on the open-decisions list, not here.

Before any plan item or operator decision is raised, the leader reconciles it
against the answered entries of the open-decisions list, so a decided matter is
never asked again.

- `stage`, in this fixed order: brief, plan, review, fold, execute, verify,
  commit/push, done. There are two review points: the plan review (after the
  plan passes its automated review and the leader's plan gate) and the diff
  review (after the result gate). Each is a pass by `<reviewer-model>` when your
  risk-tier rule calls for it; a low-risk item skips it. `verify` is the
  leader's result gate on the worker's test evidence, before commit and push. A
  post-landing check is a wake on the landed row. No stage is a human review
  step.
- `state`: `running`, `finished` (stage done, next not started), or `held`
  (next stage waits on a trigger).
- `wake`: `at <time>` for a time trigger, `on <check>` for an event trigger
  that names a concrete check, or `-` for a running row the push signals cover.
  A held row without both a trigger and a wake is silent shelving.

### Auto-advance

A stage-finished event starts the next stage in the same turn.

| Event | Next stage started the same turn |
|---|---|
| Plan gate PASS | High-risk: plan review. Low-risk: approve, execute starts |
| Plan review verdict | Clean: approve, execute. Blocking: fold, re-check the changed hunks |
| Worker reports ready | verify: the leader's result gate |
| Result gate PASS | High-risk: diff review. Low-risk: commit and push |
| Diff review verdict | Clean: commit and push. Blocking: worker fixes, re-check the changed hunks |
| Landed commit (zero commits not yet upstream) | done, or held with a wake for the post-landing check |

A turn that consumes a stage-finished event and neither starts the next stage
nor sets the row `held` with a trigger and wake breaks this rule.

## No idle sessions

- Idle reasons: `<pipeline-file>` carries an idle-reasons block above the
  table, one line per idle session naming the genuine reason, the trigger that
  ends the idle, and the owner. An idle session with no line is fixed the same
  tick: re-task it, write the line, or close it once its work is verified.
- Work generation: when fewer than `<min-working>` workers are working and no
  primary-stream row is runnable, the leader takes the next item of
  `<primary-plan>` that is unassigned (no row covers it) and eligible (its
  dependencies are done, it needs no unavailable hardware, and its repository is
  not gated), writes the row, and briefs a plan-mode worker the same tick. If no
  item qualifies, the leader writes an idle-reasons line saying so, with the
  trigger that would make one eligible, and briefs no blocked work.
- No fixed-hour waits: a serial gate the fleet imposed on itself (a timed check,
  a stale finding, a scheduled reconcile) is cleared through its sanctioned
  individual route at once. A stale finding is investigated, then fixed or
  dismissed individually with grounds, never bulk-resolved. When no sanctioned
  route exists, it is an operator decision, not a wait.

## Blocked-session gate

Every blocked session (permission or decision prompt) gets a disposition within
one tick. This replaces the older rule that only the human approves a
permission prompt.

- A routine, in-plan prompt within existing authorizations: the leader checks
  the exact prompt text against the approved plan, then answers with the
  one-time grant only, the option that allows exactly this call. Never a
  "don't ask again" option, which widens permissions for the whole session.
- An operator-class prompt (permission scope, production, credentials, physical
  acts, irreversible steps, a bypass), or one whose only way past is a
  persistent or scope-widening option: one decision to the operator in the
  decision format, recorded on the open-decisions list.
- An allowance the operator grants is scoped to the sessions it names and
  recorded with those names; a session not named needs its own decision.
- A plan menu is approved only with "Yes, manually approve edits". "Yes, and
  use auto mode" widens the worker's permissions for the whole session and is
  never selected by a coordinator.

### Delegated gating

The leader stays the plan gate for new plans. The dispatcher answers only these
routine prompts, by the one-time grant only:

1. A read-only command whose full text is visible, in a session whose plan is
   approved, touching only paths inside the fleet's working roots and no
   credential-bearing path, with no network write.
2. An edit or write on a file the approved plan names, outside the repository's
   agent-config directory.
3. A re-submitted plan menu after a fold the leader already sent, when the fold
   reads back in the plan file and nothing else changed, approved with the
   option named above.

Everything else goes to the leader in one line (session plus exact prompt). The
dispatcher reports every answer as one line in its own session starting
`GATING:` with time, session, prompt summary, choice and rule number. The
leader is the only writer of `<gating-log>` and appends those lines at each
sweep.

## Review-only steps

A step whose only output is a verdict (plan audit, diff validation, skill
audit) is routed by your risk-tier rule: a high-risk item goes to
`<reviewer-model>` on a compact package (the diff, a plan summary, the test
evidence), and a low-risk item relies on automated review and CI. It never goes
to the operator and is never reviewed by its own author.

## Briefing additions

- Restate every operator authorization or limit that binds the task, verbatim
  with its scope, since a worker cannot see the operator's order.
- Restate tool routing: read and search with the dedicated file tools, create
  files with the write tool, keep calls that may prompt out of parallel
  batches (one declined call rejects every sibling), and start each command
  with its own name.
- Name the worker's role from the launch-flag table so the spawn carries its
  flags.

## Memory is a lead, not a fact

Before acting on or relaying any claim about host, session, lock, finding or
deploy state taken from memory, a handoff or the ledger, re-query it live. A
host decommissioned that day can still be described as live in older notes.

## Operator report

Every 30 minutes the leader sends one message to the operator: one line per
session naming its item, stage and next step, plus each open operator decision.
No tables. An operator-only blocker goes at once, not at the next report. A
scheduler that dies with the session is re-created at every takeover.
