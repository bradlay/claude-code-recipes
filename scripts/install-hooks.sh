#!/bin/sh
# Activate the tracked git hooks under scripts/hooks/ for this clone.
#
# Idempotent — safe to run repeatedly. Sets core.hooksPath so future
# pushes invoke scripts/hooks/pre-push, which runs scripts/check.sh
# (every CI gate) before allowing the push.

set -eu

cd "$(dirname "$0")/.."

git config core.hooksPath scripts/hooks
chmod 0755 scripts/hooks/* 2>/dev/null || true

echo "Installed: core.hooksPath = $(git config core.hooksPath)"
echo "Pre-push will now run scripts/check.sh on every push."
echo "Override for one push: SKIP_PREPUSH=1 git push ..."
