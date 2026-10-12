---
name: release-readiness
description: One-pass, read-only go/no-go report for a dependency upgrade or a branch promotion (for example develop to main), covering branch divergence by content, dependency-bot PR state, version freshness, and the project's own checks. Invoke as /release-readiness [target-bump] when asked whether an upgrade is already done, whether it is safe to do, or whether a branch is ready to promote.
user-invocable: true
---

# Release Readiness

Answer in one auditable pass: is this upgrade or promotion already done, and if
not, is it safe to do? This skill is read-only. It never bumps, commits,
promotes, or closes PRs; it says which of those is safe.

Arguments: an optional free-text `[target-bump]` (for example
"framework 7 / bundler 8") that turns on the compatibility step. Ask which
branches are involved if the repository has more than one long-lived branch
(call them `<lower>` and `<upper>`, for example `develop` and `main`).

## Steps: run each, quote the raw result, never assert from memory

1. **Divergence, by content.** Ahead/behind commit counts mislead: after
   squash or rebase merges, a branch can be "behind" while holding all the same
   content. Compare content instead:
   - `git fetch` first.
   - `git diff --stat <upper>...<lower>`: what `<lower>` would bring.
   - `git diff --stat <lower>...<upper>`: content on `<upper>` that `<lower>`
     never had. Non-empty means real divergence; investigate it before any
     promotion.
2. **Dependency-bot PRs** (needs `gh`): `gh pr list --state open --author app/dependabot`
   (or your bot). For each, compare the target version with the lockfile on the
   base branch:
   - SUBSUMED: the base already has the target or newer, so the PR is redundant.
   - PENDING: the bump is still outstanding.
   - OVERRIDE-CONFLICT: a resolution or override rule pins below the target, so
     the override must change first.
   - UNKNOWN: grouped, lockfile-only, or unparseable. Inspect by hand; never
     count it as done.
3. **Compatibility**, only when `[target-bump]` is given or step 2 shows
   PENDING:
   - Outdated check with the ecosystem's tool (see dependency-scan).
   - Peer-dependency and engine constraints for the bump (the package's
     `peerDependencies`, required runtime versions, any platform compatibility
     date your deployment target uses). Cite the release notes you used.
4. **The project's own gates, if it has them.** CI status on the branch head
   (`gh run list --branch <lower> --limit 5`), open issues labeled as release
   blockers, any security-scan results. Report what is live; do not assume a
   clean board.
5. **Environment consistency, if relevant.** For multi-service or multi-env
   repositories, differences in configuration between environments that the
   promotion would expose.

## Output: one verdict line plus a table

```text
VERDICT: <ALREADY-DONE | READY-TO-BUMP | READY-TO-PROMOTE | BLOCKED | NEEDS-REMEDIATION(n)>

| Dimension     | Command                                | Result                     |
|---------------|----------------------------------------|----------------------------|
| Divergence    | git diff --stat <lower>...<upper>      | empty / N files            |
| Bot PRs       | gh pr list --author app/dependabot     | 0 open / #295 SUBSUMED     |
| Compatibility | <outdated / peer check>                | clean / N issues           |
| Project gates | gh run list --branch <lower>           | passing / failing: <name>  |
```

Verdict rules:

- ALREADY-DONE: the named bump is SUBSUMED and the branch already carries it.
- READY-TO-BUMP: the bump is PENDING, compatibility is clean, nothing blocks.
- READY-TO-PROMOTE: no real divergence and every project gate is green. Claim
  this only when every blocker is shown absent.
- BLOCKED / NEEDS-REMEDIATION(n): list each blocker with its command and result.

Never pre-declare an all-clear. The job is to surface blockers.
