---
name: hermes-tweet
version: 0.1.6
author: Xquik
description: Use Hermes Tweet from Hermes Agent for X/Twitter search, social listening, publishing, monitors, webhooks, media, draws, and trend reads.
tags:
  - hermes-agent
  - xquik
  - twitter
  - x
  - social-media
  - automation
metadata:
  repository: https://github.com/Xquik-dev/hermes-tweet
  plugin: hermes plugins install Xquik-dev/hermes-tweet --enable
capabilities:
  shell:
    required: false
    justification: Optional Hermes CLI checks are used only for installation and diagnostics.
  network:
    required: true
    justification: Hermes Tweet tools call catalog-listed X/Twitter API routes.
  files:
    required: false
    justification: Normal use does not require local file reads or writes.
  environment:
    required: true
    variables:
      - XQUIK_API_KEY
      - HERMES_TWEET_ENABLE_ACTIONS
      - HERMES_ENABLE_PROJECT_PLUGINS
    justification: Runtime environment controls authenticated reads, gated actions, and trusted project-local plugin loading.
  mcp:
    required: false
    justification: No MCP server access is required.
  tools:
    - tweet_explore
    - tweet_read
    - tweet_action
---

# Hermes Tweet

Use this skill when a Hermes Agent workflow needs X/Twitter context or
controlled X account actions through the native Hermes Tweet plugin.

## Install

Install and enable the plugin in the Hermes runtime:

```bash
hermes plugins install Xquik-dev/hermes-tweet --enable
hermes tools list
```

Set `XQUIK_API_KEY` in the Hermes runtime environment before authenticated
reads. Do not paste keys into chat, prompts, logs, issue bodies, or tool input.

Keep account-changing actions disabled unless the session needs them:

```bash
export HERMES_TWEET_ENABLE_ACTIONS=false
```

Set `HERMES_TWEET_ENABLE_ACTIONS=true` only for sessions that need approved
posting, DMs, follows, webhooks, monitors, extraction jobs, draws, or media
actions.

## Workflow

1. Use `tweet_explore` to find a catalog-listed `/api/v1/...` endpoint.
2. Use `tweet_read` for public read-only endpoints after the route is known.
3. Use `tweet_action` only for writes, private reads, monitors, webhooks,
   extraction jobs, media, or giveaway draws after the user approves the exact
   action.

## When to Use

- Social listening and launch monitoring.
- Creator, brand, and community research.
- Support triage from public mentions or profiles.
- Giveaway and follower evidence checks.
- Drafting or publishing X posts through an explicit approval step.
- Hermes Desktop, TUI, CLI, remote gateway, or cron sessions that need the same
  enabled `hermes-tweet` toolset.

## Safety Rules

- Never ask for or reveal API keys, passwords, cookies, signing keys, or TOTP
  secrets.
- Never pass credentials in tool arguments.
- Do not guess endpoint paths. Use `tweet_explore`.
- Do not use dashboard-admin, billing, credit top-up, API-key, account
  re-authentication, or support-ticket endpoints.
- Keep `tweet_action` disabled for unattended or read-only workflows.
- For remote gateway profiles, install and configure Hermes Tweet on the
  remote Hermes host where plugin tools execute.

## Checks

After setup, verify:

```bash
hermes plugins list
hermes tools list
```

Confirm `hermes-tweet` is enabled, `tweet_explore` appears without
`XQUIK_API_KEY`, `tweet_read` appears after the key is configured, and
`tweet_action` appears only when actions are enabled.
