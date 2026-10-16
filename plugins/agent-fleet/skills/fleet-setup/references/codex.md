# Adapter: Codex native task tree

Codex can run a fleet without a terminal multiplexer, through its native
collaboration tools. There is no control tab: the conversation you are in is
the root, and workers are child agents.

Tool names differ across Codex versions. Bind each operation below to the tools
your runtime actually exposes; do not invent missing capabilities. If delegation
is unavailable, report the fleet as unsupported. Never launch nested Codex
processes to get around a runtime limit or a denied action.

## Operation mapping

| Fleet operation | Typical native tool | Constraint |
|---|---|---|
| Commission a worker | spawn agent | Bounded, independent track |
| Inventory | list agents | Current root tree only; it does not see unrelated conversations |
| Steer a running worker | send message | Does not start an idle worker |
| Re-task an idle worker | follow-up task | Starts a new turn |
| Wait for events | wait for agent | Re-list afterwards and read delivered messages |
| Stop a worker's current turn | interrupt agent | Does not close the agent or free its slot |
| Ask the human | the runtime's user-input tool | Permission controls stay separate |

## Role separation

- If the runtime cannot keep the operator, leader, dispatcher and worker
  responsibilities separate, report that as unsupported. Do not let the
  conversation that talks to the human also execute worker tasks.
- A worker cannot grant an approval. Native approval controls stay with the
  human.
- Respect the live concurrency limit, counting the root and every descendant.
  Start with one useful worker and queue the rest; never collapse roles to fit.
- Use the current model by inheritance unless an override is authorized and
  supported. Record the model or the inheritance; never infer it from prose, and
  never translate another vendor's model alias into a guessed ID.

## Ledger and continuity

- Choose a persistent, writable ledger path, record it in every brief, and read
  the ledger back after each write. Until one exists, report durability as
  unavailable.
- Ledger contents: objective and done-test, authorized scope, root identity,
  worker IDs and names, assigned paths, model, status, last progress and time,
  evidence, pending decisions, parked items with exact triggers, commit and push
  state, scheduler coverage or its absence, next action, and a handoff section.
- Context succession needs an authoritative remaining-context figure bound to
  the session. Missing, stale, or conflicting figures are UNKNOWN and block any
  claim of context protection. Never derive it from cumulative token counts or
  account quota.
- This adapter installs no scheduler. Do not claim periodic coverage after the
  turn ends unless a supported scheduler has been set up and observed running.

## Commit and push

A separately commissioned execution worker commits and pushes, after the leader
has gated the artifacts, using an explicit path manifest. The dispatcher
coordinates the window but runs none of it. After the push, verify the upstream
difference is zero.
