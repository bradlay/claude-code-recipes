---
name: fleet-setup
description: Define, stand up, and verify the structure of a multi-session agent fleet (operator, leader, dispatcher, workers), including layout, per-role model, naming, and the operator invariants. Invoke when starting a fleet from scratch, when a leader succession needs the control layout rebuilt, when a role pane is missing or on the wrong model, or when fleet-lead or fleet-sweep cites fleet-setup as the source of truth for structure.
---

# Fleet Setup

This skill is the single source of truth for the fleet structure contract.
fleet-lead and fleet-sweep cite it rather than repeating its values, so the
three cannot drift apart. When any of the three changes, review all three
together.

The contract is runtime-neutral. The mechanics of a particular runtime live in
adapter references:

- [references/herdr.md](references/herdr.md): sessions as panes and tabs in the
  Herdr terminal multiplexer (Claude Code or Codex sessions).
- [references/claude-code.md](references/claude-code.md): Claude Code features
  the fleet relies on (Monitor, scheduled prompts, goals, hooks), including an
  optional hook pattern for enforcing role boundaries.
- [references/codex.md](references/codex.md): a Codex native task tree, with a
  table mapping fleet operations to collaboration tools.

If the chosen runtime cannot keep the four roles separate, report that as
unsupported. Never merge roles silently to make the fleet fit.

## The structure contract

Four tiers. Workers never share the coordinators' control surface.

| Tier | Name | Model | Placement | Role in one line |
|---|---|---|---|---|
| operator | `fleet-operator` | `<fast-model>` | control surface, top, full width | Sole interactive channel to the human; relays directives to the leader and answers back |
| leader | `fleet-lead-<n>` | `<coordinator-model>` | control surface, bottom left | Coordinator: rulings, gates, go decisions, succession |
| dispatcher | `fleet-dispatcher-<n>` | `<fast-model>` | control surface, bottom right | Relay and execution lane: spawns and briefs workers, runs commit, push and deploy commands on gated work |
| worker | descriptive unique name | leader's choice by task | own tab or own session, never the control surface | Authors code, plans, docs, tests |

Model guidance: the leader needs the strongest reasoning you have, because it
reads plans and judges results. The operator and dispatcher mostly relay and
run commands, so a fast, cheaper model fits. Pick worker models per task.
Every spawn passes an explicit model; runtime defaults drift, and a vague alias
can resolve to something other than what you intended. Record the model you
chose for each session so the sweep can check it.

## Operator invariants

These five hold at all times. A breach is a defect fixed on sight.

1. Nothing is sent into the operator's session automatically, except the single
   operator-decision relay in invariant 3. Apart from that relay, only the human
   types into it.
2. The operator authors no code and runs no repository commands. It relays and
   displays judgment, and watches that the leader and dispatcher are alive. It
   flags them if they stall, loop, or go silently idle.
3. Every genuine operator decision is delivered to the operator's session and
   nowhere else, unless the leader decides the matter itself (the default; the
   leader decides rather than escalates whenever the call is technical). The
   delivery is ONE message with concrete lettered options, sent once, never
   re-sent or followed by synthetic key presses. The leader and dispatcher never
   ask an interactive question in their own sessions, because a question there
   blocks the coordinator until a human happens to look.
4. The operator is never replaced at a context limit. Compaction is fine for it;
   it is the one constant interactive surface. Never create a second
   interactive-decision surface elsewhere in the fleet.
5. Every open operator decision is also recorded on a single running
   open-decisions list in the fleet ledger and re-surfaced every sweep until
   answered. A decision that lives only in a coordinator's prose or in memory,
   off that list, is a breach.

## Stand-up sequence (generic)

The adapter reference gives exact commands. The order is:

1. Confirm the starting sessions and record their IDs from the runtime's own
   output; never predict IDs.
2. Create the control surface with three slots: operator across the top, leader
   bottom left, dispatcher bottom right.
3. Start each role on the model the contract names for it.
4. Give every session and every tab a role name. No session keeps a default
   title such as the runtime's name or a bare number.
5. Optional, recommended: record which session is the dispatcher (and the
   leader) in a role-marker file that a pre-tool hook reads, so role boundaries
   are enforced mechanically and not by memory. See
   [references/claude-code.md](references/claude-code.md#optional-role-boundary-hooks).

## Verify

Run after stand-up, after every spawn, and after every succession:

1. Layout: operator full width on top; leader and dispatcher below it at about
   50/50, leader on the left; no worker on the control surface.
2. Names: every live session carries a role name. Check the field the UI
   actually renders (a pane or tab label), not only the agent name set at spawn.
   They can be different fields: an agent-name check passes while the title
   still shows a default, which is how this defect hides across successions.
   Read the label back after each rename.
3. Tab names: every tab carries a descriptive label, and no tab keeps a numeric
   placeholder.
4. Models: each role on the model recorded for it.
5. Invariants: the five operator invariants hold.
6. If role markers are in use, each marker names the live session for its role.

Fix any miss in the same pass. A role on the wrong model that cannot switch in
place is replaced on the right model.

## Inline enforcement

Layout is enforced at creation, not deferred to the hourly sweep. A succession,
a worker spawn, and a dispatcher succession each create a session, and a split
can land on the wrong side. Each of those events runs the Verify list inline and
self-corrects before handing off or reporting done. The incoming leader after a
succession confirms it holds the bottom-left slot, swaps if reversed, and
renames its own session, because a spawn usually sets only the agent name.

## Idle-with-output detection

No finished session sits unnoticed. Use several independent paths, because each
one is blind to something:

1. Dispatcher poll: the dispatcher reads each worker's recent output right after
   tasking it and on a steady cadence between sweeps. An idle worker with output
   is relayed to the leader; the dispatcher does not act on the work itself.
2. Blocked rising edge: the leader watches for any session entering a blocked
   state (a decision menu or approval hold), firing once per transition.
3. Worker finished: a finished worker waiting for its gate goes idle, not
   blocked, so path 2 cannot see it. The leader also watches for a worker moving
   from working to idle.
4. Dispatcher watch: the dispatcher is a control role, so path 3 excludes it, and
   when it escalates a problem it usually goes idle with a question rather than
   blocked. The leader watches the dispatcher's output for a pending escalation.
   An unseen dispatcher stall idles every worker waiting on a commit behind it.
5. Sweep backstop: fleet-sweep C.6 and C.7 catch anything all push paths missed.

Some runtimes cannot push notifications for every session type. Where a
notification feature does not reach a session, fall back to polling; never
assume coverage you have not observed.

Triage on every path: the leader decides technical calls itself, which is the
default, and routes a genuine operator decision through invariant 3 and onto the
list of invariant 5. Leader succession is technical: it fires at the context
threshold with a ready handoff and is never presented to the operator as a
choice.
