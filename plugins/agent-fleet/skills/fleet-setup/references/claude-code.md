# Adapter: Claude Code

Claude Code features the fleet uses, what each one covers, and where each one
falls short. Tool availability varies by version and plan; check what your
session actually exposes before relying on any of these.

## Scheduling and signals

| Fleet need | Claude Code feature | Notes |
|---|---|---|
| Periodic sweep | A scheduled prompt (for example the cron scheduling tool, or `/loop` with an interval) that runs `/fleet-sweep` | Confirm the schedule exists every sweep (Step 0) |
| Blocked rising edge, worker finished, dispatcher watch | The Monitor tool running a polling command (for example `herdr agent list` every 30 to 60 seconds) and emitting only on a transition | One monitor per signal, so each fires once per edge |
| Leader context watch | A Monitor that reads the leader's authoritative context figure on a cadence and fires at the threshold | See "Context figure" below |
| Leader goal | `/goal` with the streams and a done-test | A goal shows intent; it is not evidence of progress |
| Cross-session messages | SendMessage, where available | Its idle notification does not reach every session type (for example remotely controlled sessions); poll those instead |

Never claim supervision after a turn ends unless a schedule or monitor is
installed and has been observed firing in its real environment.

## Context figure

Succession needs an authoritative remaining-context figure per session. A
session's own estimate of its context is not one: the model has no direct view
of its token budget, and its estimate drifts. Options:

- Configure a status line that prints remaining context for the session, then
  read it from the pane (under Herdr: `herdr agent read <name> --lines 3`, and
  match the figure).
- If no such figure is available for a role, report its context as UNKNOWN in
  the sweep. Do not substitute an estimate.

## Optional: role-boundary hooks

The roles table in fleet-lead is easier to keep when a PreToolUse hook enforces
it. The pattern:

1. At setup, each coordinator writes a role-marker file recording its own
   session ID, for example
   `{"session_id": "<id>", "name": "fleet-dispatcher-2", "since": "<iso time>"}`.
   A successor overwrites the single slot at takeover.
2. A PreToolUse hook reads the marker. If the calling session is the marked
   dispatcher and the tool is an authoring tool (Edit, Write, NotebookEdit, or
   an in-process subagent), it denies with a reason that points to the right
   lane. A leader marker can likewise deny direct repository commands so the
   leader stays in its gate-and-delegate loop, and both markers can deny an
   interactive question tool, which would freeze a coordinator.
3. There is no self-service override file. A real role change is a
   succession, which rewrites the marker.

Example registration in a plugin's `hooks/hooks.json` (script names are
placeholders you would write yourself):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Agent|Task|Edit|Write|NotebookEdit",
        "hooks": [
          { "type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/bin/dispatcher-role-guard\"", "timeout": 5 }
        ]
      },
      {
        "matcher": "AskUserQuestion",
        "hooks": [
          { "type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/bin/coordinator-question-guard\"", "timeout": 5 }
        ]
      }
    ]
  }
}
```

The guard reads the hook payload from stdin (it includes `session_id` and
`tool_name`), compares the session against the marker, and to deny prints:

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "Dispatcher sessions do not author files; brief a worker instead."}}
```

Decide deliberately whether the guard fails open or closed when the marker is
missing or unreadable, and document the choice. A guard that silently stops
enforcing is worse than none, because the fleet believes it is protected.
