---
name: dependency-scan
description: Scan a project's dependencies for known vulnerabilities and outdated versions using the ecosystem's own tools (pip-audit, npm or pnpm audit and outdated), and propose updates toward latest stable. Invoke when asked to check dependencies, audit for vulnerable packages, list outdated packages, or plan a dependency update.
user-invocable: true
---

# Dependency Scan

Goal: stay on the latest stable version of everything, and know about every
known vulnerability. This skill uses each ecosystem's own tools; it installs
nothing and changes nothing unless asked to update.

## Detect ecosystems

Look for manifests in the project root (and workspace packages):

| Manifest | Ecosystem | Vulnerabilities | Outdated |
|---|---|---|---|
| `pyproject.toml`, `requirements*.txt` | Python | `pip-audit` | `pip list --outdated` (inside the project's environment) |
| `package.json` + `pnpm-lock.yaml` | Node (pnpm) | `pnpm audit` | `pnpm outdated` |
| `package.json` + `package-lock.json` | Node (npm) | `npm audit` | `npm outdated` |
| `Cargo.toml` | Rust | `cargo audit` (if installed) | `cargo outdated` (if installed) |
| `go.mod` | Go | `govulncheck ./...` (if installed) | `go list -m -u all` |

Each tool is optional. If one is missing, say which and how to install it, and
continue with the rest. Never fall back to guessing versions from memory.

## Run

1. Vulnerabilities first. Prefer machine-readable output (`--json` or the
   tool's equivalent) and summarize by severity: package, installed version,
   fixed version, advisory ID.
2. Then outdated packages, grouped as patch, minor and major.
3. Quote each command you ran and its exit status in the report.

## Update policy

- Patch: apply.
- Minor: usually apply; skim the changelog for behavior changes.
- Major: review the changelog and migration notes first; update in a separate
  change with its own test run.
- Security fixes take priority over the grouping above.

Only update when the user asks. Use the ecosystem's own update command, update
the lockfile in the same change, run the project's tests, and report what
changed.

## Report

```text
VULNERABILITIES: <critical> critical, <high> high, <other> other
| Package | Installed | Fixed in | Severity | Advisory |

OUTDATED: <patch> patch, <minor> minor, <major> major
| Package | Installed | Latest | Kind |

Commands run: <command> -> exit <code>
```

## Exit-code convention for CI

When wrapping this in a script for CI, a common contract is: 0 no issues,
1 high-severity vulnerabilities, 2 critical vulnerabilities.
