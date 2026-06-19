#!/bin/sh
# Run every pre-push gate locally. Bails on the first failure.
#
# Why: CI runs ruff check, ruff format --check, mypy --strict per
# plugin, pytest per plugin, the empty-except gate, JSON manifest
# validation, shellcheck on bin/, and the namespace-leak gate.
# Running them all locally before pushing avoids the loop where
# something passes one gate, fails another in CI, and turns one push
# into three.
#
# Usage:
#   ./scripts/check.sh
#
# All checks invoke their tools directly; this script does not assume
# `sddc lint` etc. are available so it works on a clean machine.

set -eu

cd "$(dirname "$0")/.."

step() {
    printf '\n=== %s ===\n' "$1"
}

# Plugins are discovered from the filesystem (plugins/*/) by the mypy and
# pytest loops below — not a hand-maintained list. CI type-checks and
# tests every plugin on disk; deriving the set the same way keeps this
# gate a superset of CI so it can never silently skip one (which is how a
# stop-session-check break once passed locally yet reddened CI).

step "ruff check (lint)"
ruff check plugins/

step "ruff format --check"
ruff format --check plugins/

step "mypy --strict per plugin"
for d in plugins/*/; do
    p=$(basename "$d")
    [ -d "plugins/$p/scripts" ] || continue
    echo "  mypy $p"
    MYPYPATH="plugins/$p/scripts" mypy --strict --explicit-package-bases --ignore-missing-imports "plugins/$p/scripts"
done

step "pytest per plugin"
for d in plugins/*/; do
    p=$(basename "$d")
    test_dir="tests/$(echo "$p" | tr '-' '_')"
    [ -d "$test_dir" ] || continue
    echo "  pytest $test_dir"
    pytest -q "$test_dir"
done

step "Empty-except gate"
python3 scripts/find_empty_excepts.py

step "JSON manifest validation"
for f in .claude-plugin/marketplace.json plugins/*/.claude-plugin/plugin.json plugins/*/hooks/hooks.json; do
    [ -f "$f" ] || continue
    python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$f"
    echo "  OK: $f"
done

step "Namespace leak gate"
if grep -rn 'AUTOSRE_' plugins/*/scripts/ 2>/dev/null; then
    echo "FAIL: legacy AUTOSRE_ env var reference found"
    exit 1
fi
echo "  no AUTOSRE_ references"

step "Hook commands quote plugin root"
fail=0
for f in plugins/*/hooks/hooks.json; do
    if ! grep -q '"\${CLAUDE_PLUGIN_ROOT}/' "$f"; then
        echo "FAIL: $f must quote \${CLAUDE_PLUGIN_ROOT}"
        fail=1
    fi
done
[ "$fail" = "0" ] || exit 1
echo "  every hook command quotes plugin root"

step "bin/ executable bit"
fail=0
for f in plugins/*/bin/*; do
    [ -f "$f" ] || continue
    if [ ! -x "$f" ]; then
        echo "FAIL: $f is not executable (chmod 0755)"
        fail=1
    fi
done
[ "$fail" = "0" ] || exit 1
echo "  every launcher is executable"

step "shellcheck on launchers"
# Resolve a pinned shellcheck (downloaded on demand, cached) so this gate
# always runs — never silently skipped, which would let a lint regression
# pass here yet red CI — and runs the same version CI does.
SHELLCHECK="$(scripts/ensure-shellcheck.sh)"
# Only POSIX-sh launchers; bin/ also holds python launchers
# (plan-review-{probe,quality,shadow}). Filter by shebang so the set
# matches CI's shellcheck job and adapts to new launchers automatically.
for f in plugins/*/bin/*; do
    [ -f "$f" ] || continue
    case "$(head -n1 "$f")" in
        '#!'*sh) "$SHELLCHECK" -s sh "$f" ;;
    esac
done
echo "  shellcheck clean"

if command -v claude >/dev/null 2>&1; then
    step "claude plugin validate"
    for d in plugins/*/; do
        [ -f "${d}.claude-plugin/plugin.json" ] || continue
        claude plugin validate "$d" >/dev/null
        echo "  OK: $d"
    done
else
    echo
    echo "  (claude CLI not installed; skipping plugin validate)"
fi

echo
echo "All pre-push gates passed."
