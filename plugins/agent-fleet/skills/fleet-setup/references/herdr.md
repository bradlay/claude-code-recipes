# Adapter: Herdr

Herdr is a terminal multiplexer for
coding agents. It organizes sessions into workspaces, tabs and panes, recognizes
agents running in panes, and exposes them through the `herdr` CLI. With it, each
fleet role is a separate Claude Code or Codex session in its own pane.

Herdr is optional. Without it, use the Claude Code or Codex adapter, with the
limits described there.

Command syntax changes between Herdr versions. Treat the commands below as the
shape of each step and confirm the exact flags with `herdr --help` and
`herdr <group> --help` before use. Run one command per shell call and read IDs
from the JSON each command returns; never predict them.

## Preconditions

- `HERDR_ENV=1` in the session that issues control commands, meaning it runs
  inside a Herdr-managed pane. If not, stop and report that setup must run
  inside Herdr; do not attach to an arbitrary focused session.
- Preserve each session's configured sandbox, approval settings, hooks and
  authentication. Never add a bypass flag or weaken permissions to make a role
  start.

## Inventory

| Need | Command shape | Field to read |
|---|---|---|
| Workspaces | `herdr workspace list` | ids |
| Sessions | `herdr agent list` | `name`, `model`, `agent_status`, `pane`, `tab_id` |
| Pane labels | `herdr pane list` | `label` (what the UI shows) |
| Tab labels | `herdr tab list` | `label`, `number` |
| Geometry | `herdr pane layout --pane <pane>` | character grid |
| Recent output | `herdr agent read <name> --source recent-unwrapped --lines 45` | text |

The agent `name` (set at spawn) and the pane `label` (shown in the UI) are
different fields. Check the label for naming compliance.

## Stand up the control tab

Starting from a leader and dispatcher side by side:

1. `herdr agent list`: note both pane IDs and the control `tab_id`.
2. Move the dispatcher to a temporary tab so the leader is alone:
   `herdr pane move <dispatcher-pane> --new-tab`.
3. Split the leader pane downward: `herdr pane split <leader-pane> --direction down`.
   Read the new pane ID from the result.
4. Start the operator in the new pane on its model:
   `herdr agent start fleet-operator --kind claude --pane <new-pane> -- --model <fast-model>`.
   Arguments after `--` go to the agent CLI; for a Codex session use
   `--kind codex`.
5. Make the operator the top pane. If it landed at the bottom, move the leader
   under it: `herdr pane move <leader-pane> --tab <control-tab> --split down --target-pane fleet-operator`.
6. Split the leader pane right: `herdr pane split <leader-pane> --direction right`.
7. Move the dispatcher back into the bottom-right slot:
   `herdr pane move <dispatcher-pane> --tab <control-tab> --split right --target-pane <leader-pane>`.
8. Rename every pane to its role: `herdr pane rename <pane> <role-name>`; read the
   returned label back. Rename tabs: `herdr tab rename <tab_id> <label>`.

Splits only go right or down. To fix a reversed pair, swap them:
`herdr pane swap --source-pane <a> --target-pane <b>`.

## Spawn a worker

Create the worker in a fresh tab (or create its pane and immediately move it
with `herdr pane move <pane> --new-tab`), start the agent with an explicit
model, label both the pane and the tab, then run the fleet-setup Verify list.
Confirm with `herdr agent list` that no worker shares the control `tab_id`.

## Send and read

- Send: `herdr agent prompt <name> "<text>"`. Keep payloads free of backticks and
  shell substitutions, since the shell may expand them.
- A returned `ok` is not delivery. Read the pane afterwards. If the text sits in
  the input box ("press up to edit queued messages"), send it again.
- Menus: answer with `herdr agent send-keys <name> <n>` then
  `herdr agent send-keys <name> enter`. Only the human answers permission or
  operator-decision menus.
- Switch a model in place where the agent supports it:
  `herdr agent prompt <name> "/model <id>"` then `send-keys ... enter`.
- Read a blocked pane with `--source visible`; scrolling history can be rejected
  while blocked.

## Manager-slot rule

Exactly one leader and one dispatcher occupy the control tab. A successor
replaces its predecessor in the same slot; succession never accumulates manager
tabs or keeps old terminals around for history. Before transfer, persist and
read back the custody manifest (artifacts, open items, pending gates, commands
in flight), confirm the old writer is quiescent from process evidence, transfer,
verify the successor accepted under its own identity, then retire the
predecessor. There is never a moment with two authorized writers for one role.
Closing a predecessor session does not close the work it handed over.

## Optional: a watch sidecar

Herdr does not push notifications into a busy coordinator. A small watch process
in its own tab can poll `herdr agent list` on a cadence (for example 30 seconds),
record working-to-idle and into-blocked transitions per session, queue them in a
file, and wake an idle coordinator at most every few minutes. Pair it with a
separate monitor process that writes its own heartbeat and checks the watcher's
heartbeat, so a dead watcher is detected rather than silently trusted.

Design points if you build one:

- Bind the watcher to the exact session identities of the leader and
  dispatcher, and rebind after every succession.
- An initial idle state is a baseline, not a transition. Never infer a
  transition across a gap in observation.
- Queued events stay pending until a coordinator actually consumes them. A
  successful wake is not consumption.
- Never type into the operator pane, and never answer a menu.
- Coverage lasts only while the machine and the Herdr session run. Report it as
  unsupported otherwise, and never claim operation from installed files alone.

## Sweep and Step C in Herdr

Enumerate with `herdr workspace list` then `herdr agent list` per workspace,
filter to the fleet's own inventory, and run fleet-sweep. Close a finished,
verified worker with `herdr tab close <tab_id>`, moving it out of the control tab
first if it is there. `done` in Herdr means idle after unseen work, not
completion.
