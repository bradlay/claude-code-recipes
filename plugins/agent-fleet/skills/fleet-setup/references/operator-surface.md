# Operator surface and launch rules

Extends [fleet-setup](../SKILL.md). This file owns the operator-surface rules
and the role-scoped launch flags; fleet-lead and fleet-sweep cite it.

## Operator invariant 6: addressable by name only

Other sessions reach the operator only by a direct message to the name
`fleet-operator`. That name resolves only when the operator session was launched
with an explicit session name and its role marker (`<marker-dir>/operator-session.json`)
names the live session. An unresolvable name is a defect fixed by relaunching
the operator on its launch line. It is never worked around by typing text into
the operator's session, and never by leaving the decision in a coordinator's
prose. While a live operator predates the named launch, send to the handle whose
session id matches the operator marker, checked before each send.

## Questions only as a last resort

The operator never starts a question of its own. It raises something to the
human only when a sender delivered a genuine operator decision under invariant
3, after the fleet has decided everything it can. It never asks the human about
a technical or routine matter, and never asks to confirm something already
authorized.

## Decision format

This section is the single statement of the option format; every other file
cites it.

- Options are lettered A, B, C, D in that order, with A first.
- The operator relays the sender's letters and their mapping exactly. It never
  reorders, relabels or merges options, not even to put the recommended one
  first.
- The recommendation is written in the option text, for example
  "(Recommended)", never implied by position.
- A prompt carries at most 4 options. A larger decision is split by the sender
  into two prompts.

## Operator goal with a succession exit

If the runtime supports a standing goal, the operator's goal is set once, as a
launch argument at spawn, and nowhere else. It ends with a succession exit: the
goal is also met once the operator marker names a different session. Without
that clause a replaced operator keeps looping on its own goal check.

## Explicit model ids

Every spawn passes a full model id, never a bare alias such as `opus` or
`sonnet`, because an alias drifts. The sweep compares the runtime's reported
model field with these ids, so an alias cannot be checked at all.

## Role-scoped launch flags

Grant each worker role only the permissions its role needs, at launch, and
nothing globally. A shared settings allow-list is never widened for this. Any
widening beyond the table is a new operator decision. A blocked-session
watchdog (fleet-sweep C.9 in the [sweep addendum](../../fleet-sweep/references/sweep-checks-addendum.md))
stays the backstop for any prompt these flags do not cover.

| Worker role | Launch flags (Claude Code example) |
|---|---|
| plan-mode researcher | `--permission-mode plan` |
| reviewer | `--permission-mode plan` |
| implementer that hashes artifacts | `--allowedTools "Bash(shasum -a 256:*)"` |
| data-pull worker | `--allowedTools "Write(<evidence-dir>/**)"` |
| skill-editing worker | `--allowedTools "Edit(<repo>/.claude/skills/**)" "Edit(<repo>/.claude/rules/**)"` |

## Coordinators never prompt in their own sessions

The leader and the dispatcher never open an interactive question in their own
sessions, because a prompt there blocks the coordinator and stalls every worker
gated on it. If you use role-marker hooks, have the dispatcher-role hook deny
the interactive-question tool to both recorded coordinator sessions.
