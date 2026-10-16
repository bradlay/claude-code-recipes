---
name: pr-ci-watch
description: Report-only CI status for a repository's open pull requests, one line per PR, using the GitHub CLI. Invoke when asked whether open PRs are green, which are ready to merge, or which checks are failing; pair with /loop for continuous monitoring. Never merges anything.
user-invocable: true
---

# PR CI Watch (report only)

Check CI status on the current repository's open pull requests. Report which are
ready to merge and which have failing checks. This skill never merges; a human
does.

Requires the GitHub CLI (`gh`), authenticated for the repository.

## Steps

1. List open PRs, optionally filtered to a base branch:
   `gh pr list --state open --json number,title,headRefName,baseRefName,isDraft`
   (add `--base <base-branch>` when the user names one).
2. For each PR: `gh pr checks <number>` and, if needed,
   `gh pr view <number> --json mergeable,reviewDecision`.
3. Report one line per PR:
   - number, title, head and base branch
   - checks: passing, failing (name the failing check), or pending (n of m done)
   - if every check passes and it is mergeable: suggest
     `gh pr merge <number>` with the repository's usual merge method
4. Keep it to one line per PR plus a one-line summary.

## Example output

```text
my-repo open PRs:
  #174 feature-x -> main: checks passing, ready to merge (gh pr merge 174)
  #175 deps-bump -> main: checks pending (2/3 complete)
  #176 fix-y -> main: failing: unit-tests
```

## With /loop

```text
/loop 5m /pr-ci-watch
```

Stop the loop once nothing is pending.
