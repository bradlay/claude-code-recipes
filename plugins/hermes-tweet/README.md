# hermes-tweet

Claude Code skill wrapper for the native Hermes Agent X/Twitter plugin.

Hermes Tweet installs into the Hermes runtime and exposes `tweet_explore`,
`tweet_read`, and approval-gated `tweet_action` tools. This wrapper gives
Claude Code users the install and operating guidance without vendoring runtime
code.

## Install in Claude Code

```text
/plugin marketplace add bradlay/claude-code-recipes
/plugin install hermes-tweet@claude-code-recipes
/reload-plugins
```

## Install in Hermes

```bash
hermes plugins install Xquik-dev/hermes-tweet --enable
hermes tools list
```

Set `XQUIK_API_KEY` in the Hermes runtime environment before authenticated
reads. Keep `HERMES_TWEET_ENABLE_ACTIONS=false` until a session explicitly
needs approved account-changing actions.

## Workflow

1. Use `tweet_explore` to find a catalog-listed `/api/v1/...` route.
2. Use `tweet_read` for public read-only endpoints.
3. Use `tweet_action` only after the user approves the exact endpoint and
   payload.

## Safety

- Never ask for or echo API keys, passwords, cookies, or TOTP secrets.
- Never pass credentials in tool arguments.
- Do not guess routes or call direct HTTP fallbacks.
- Keep writes, DMs, follows, monitors, webhooks, extraction jobs, media, and
  draws behind `HERMES_TWEET_ENABLE_ACTIONS=true` plus explicit user approval.

## Source

- Repository: <https://github.com/Xquik-dev/hermes-tweet>
- Package: <https://pypi.org/project/hermes-tweet/>
- License: MIT
