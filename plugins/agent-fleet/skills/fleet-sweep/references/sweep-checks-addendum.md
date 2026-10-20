# Sweep checks C.8 to C.11

Extends [fleet-sweep](../SKILL.md) Step C. Each check cites the rule it checks
in the fleet-lead [pipeline and autonomy](../../fleet-lead/references/pipeline-and-autonomy.md)
reference rather than restating it. Emit one status line per check, with the
same statuses as C.1 to C.7.

- **C.8 Session accounting.** Enumerate every session in every tab or group,
  not only the control surface and known worker tabs. A session idle for more
  than two sweep ticks with no pipeline row is a FIX, as is a session with no
  label. FIX: add a row with owner, trigger and wake, or record a disposition
  and close the session once C.7 confirms nothing is stranded.
- **C.9 Blocked-session disposition.** Every blocked session has a disposition
  recorded this tick under the Blocked-session gate, either answered by its
  one-time grant (or a recorded operator allowance that names it) or sent to
  the operator and on the open-decisions list. A blocked session without one is
  a FIX, and so is any persistent or scope-widening grant selected with no
  recorded allowance behind it. If you run a blocked-session watchdog, confirm
  it is live and that each disposition was recorded against its occurrence.
  Read the dispatcher's `GATING:` lines since the last sweep, append them to
  `<gating-log>`, and spot-check them against Delegated gating: each answer names
  a rule 1 to 3 and the one-time grant, and no escalated class was answered.
- **C.10 Idle reasons.** Every session idle at sweep time has its line in the
  idle-reasons block with trigger and owner (No idle sessions). When fewer than
  `<min-working>` workers are working and no primary-stream row is runnable,
  either work generation fired this tick for an eligible unassigned item, or the
  block carries a "no eligible primary-plan item" line with its trigger and
  owner; both are PASS. FIX: re-task, write the missing line, or generate the
  next eligible item now.
- **C.11 Operator report.** The 30-minute operator report is scheduled and the
  last one went out within 30 minutes (Operator report). FIX: send it now and
  re-create the schedule.
