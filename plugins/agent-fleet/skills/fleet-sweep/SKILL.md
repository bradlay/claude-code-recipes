---
name: fleet-sweep
description: Full stock-take, disposition, liveness check, and recovery pass for a multi-session agent fleet, run by the leader. Invoke on the periodic schedule, whenever the fleet looks idle, when workers finish, block, or appear stalled, when resuming after a handoff, or when asked for fleet status. Classifies every session, disposes of it, re-surfaces parked triggers and open decisions, and runs the C.1 to C.7 structure compliance check.
---

# Fleet Sweep

Run top to bottom every time. Change the procedure here, never by remembering a
variation. Structure values come from fleet-setup; runtime commands come from
its adapter references.

Two modes:

- **Status-only** (someone asked "what is the fleet doing?"): read, classify,
  report. Change no files, assignments, or schedules.
- **Recovery** (the sweep is scheduled, or someone asked to fix or continue the
  fleet): additionally dispose of sessions within existing authorization.

## Delivery mechanics

- A send that returns success is not delivery. Read the target session; if the
  text is still sitting in its input box, deliver it again. A send that times out
  is UNKNOWN, not delivered.
- Polling, attempted delivery, and consumed input are three separate facts.
- Unsent text in an idle session that you did not send is a UI suggestion.
  Never submit it, and never treat it as an order.
- A message sent to a session sitting at a menu queues behind the menu.
- A plan that will not converge after several review iterations is decomposed
  or narrowed, with the grounds recorded; never approved blind.

## Step 0: coordinators locked in

1. The periodic sweep is scheduled.
2. The leader has an active goal and the dispatcher owns its liveness.
3. One dispatcher is live with a recent state change.
4. The control surface matches fleet-setup. Step C checks it in full; fix
   layout in this sweep, never defer it.
5. Leader and dispatcher are above the coordinator succession threshold, read
   from the authoritative indicator; otherwise hand over now. The operator is
   exempt.
6. The push signals from fleet-setup are armed (blocked rising edge, worker
   finished, dispatcher watch). Arm any that are missing.

## Step 1: enumerate everything

List every session the fleet owns before disposing of anything. Do not control
sessions outside the fleet's inventory. Then read the ledger's parked list and
label each trigger cleared, still blocked, or unknown (unknown is not cleared).
Then read the open-decisions list and label each decision still open or
answered; re-surface every open one through the operator relay. The report
carries both lists; a report without them did not run this step.

## Step 2: classify every session

Read each session's recent output unless status plus a fresh report make it
obvious. Check each role's model against what fleet-setup recorded, and that the
dispatcher wrote no code. Exactly one state each, checked in this order:

| State | Evidence | Response (recovery mode) |
|---|---|---|
| LIKELY-DEAD | Revision frozen since the last sweep, open work, active goal | Confirm from current liveness evidence, preserve artifacts, then hand over to a fresh successor |
| LOW-CONTEXT | Authoritative context at or below the role's threshold | Handoff, quiescence, fresh successor, supervision rebind |
| BLOCKED-APPROVAL | A required permission is missing | Finish independent preparation; surface the real gate to the human |
| BLOCKED-DECISION | A scope or operator choice is pending | The leader decides technical choices now; operator choices go through the relay |
| BLOCKED | A known dependency, lock, push window, or another session's commit | Give bounded work in its own paths, or park with an exact trigger |
| STALLED | Incomplete work, no progress, no legitimate wait | Re-plan, split, or re-task |
| RESULT-READY | Finished, but its output has not been consumed | The leader reads and gates it now |
| COMPLETE | Done-test verified, including the required landing state (committed, pushed, deployed) | Record terminal status; close |
| BUILDING | Fresh progress or a legitimate running command | Record the next observation point |
| UNKNOWN | Missing or contradictory evidence | Get evidence before intervening |

"Idle" is not completion. Never replace a session based on its own reported
context figure.

## Step 3: dispose (recovery mode)

Apply the response column. Additional rules:

- BLOCKED with no buildable work of its own: log it as a still-open item with a
  tracked resume trigger and a concrete resume path, then close. A silent close
  with no trigger is forbidden.
- A blocker that is really an operator decision goes on the open-decisions list
  and through the relay, not into a park.
- Close only fleet-owned sessions, and only after reading the final report and
  confirming the landing state.

## Step 4: exit invariant

After the sweep, every session is in exactly one legitimate state: BUILDING
under an active goal; held on a tracked external blocker re-surfaced this sweep;
awaiting a live operator decision on the list; awaiting a verification step that
is itself in flight; or closed after verified completion (or logged with a
tracked trigger). A session whose only open work is waiting for a push window
counts as idle: give it the next buildable item.

## Step C: structure compliance check

Emit one line per check, C.1 to C.7, each with one status:

- PASS: evidence-backed success.
- FIX: a repair was made and verified.
- BLOCKED: a required condition is unmet.
- UNKNOWN: evidence is missing.
- N/A: not applicable, with a concrete reason.

Never turn a capability gap into PASS.

- **C.1 Layout.** The control surface holds exactly the operator (top, full
  width), the leader (bottom left) and the dispatcher (bottom right). No worker
  is on it. FIX: swap a reversed pair, move a stray worker out, rebuild only if
  slots are missing.
- **C.2 Naming.** Check the label field the UI renders, for every session and
  every tab. No default titles, no numeric tab labels. FIX: rename, then read the
  label back.
- **C.3 Models.** Each role is on its recorded model. An unavailable effective
  model is UNKNOWN, not PASS. FIX: switch in place where supported, otherwise
  replace the session.
- **C.4 Operator invariants.** All five hold this cycle: no automated input to
  the operator except the decision relay; no code or repository commands from
  it; every operator decision surfaced there and only there; not replaced; the
  open-decisions list re-surfaced. Also check that the dispatcher authored no
  code and, if role markers are in use, that each marker names the live session.
- **C.5 Consistency.** fleet-setup, fleet-lead and fleet-sweep (plus the adapter
  references) agree. When one changes, review all three together. The ledger and
  handoff are current. Monitoring coverage is stated accurately: unattended
  operation counts only after an observed successful run and a separate liveness
  signal.
- **C.6 Idle with unconsumed output.** Every idle or finished session has had
  its output read, gated, or recorded with a named pending decision and owner.
  FIX: relay it to the leader now.
- **C.7 Idle for a whole interval.** For every worker idle since before the last
  sweep, confirm concretely whether its work landed (committed and pushed, or
  otherwise verified) or whether work remains (an unpushed commit, an unfolded
  edit, staged changes, a held backlog). A claimed trigger is not confirmation.
  Close it if done; re-task or hand off the remainder if not. Never close for
  tidiness, and never strand incomplete work in a closed session.

## Step 5: report and persist

One line per session grouped by state, the C.1 to C.7 block, one line per parked
stream with its trigger state, one line per open operator decision, and what was
closed, re-tasked, decided, or spawned. In recovery mode, append the sweep to the
fleet ledger in the same tick. In status-only mode, report without writing. A
missing ledger is a limitation to report.

## Recovery

Fleet idle with open work, or rollups stopped: restore the dispatcher's goal and
liveness check, re-arm the push signals, then run Steps 1 to 5 including Step C.
The usual cause is a lapsed goal plus a decision that never reached the leader.
