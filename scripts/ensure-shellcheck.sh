#!/bin/sh
# Resolve a pinned shellcheck and print its path on stdout.
#
# The pre-push gate (scripts/check.sh) and the CI shellcheck job both call
# this so they run the *same* shellcheck version — no apt-version drift
# between a contributor's machine and CI, and no silent skip when the tool
# isn't installed (which would let a lint regression sail through locally
# and only red in CI). The version is pinned to match the
# koalaman/shellcheck-precommit rev in .pre-commit-config.yaml.
#
# A binary already on PATH is used as-is (respect a contributor's install);
# otherwise the pinned release is downloaded once into an XDG cache and
# reused. Everything but the resolved path goes to stderr so callers can do
#   SHELLCHECK="$(scripts/ensure-shellcheck.sh)"
#
# Usage: scripts/ensure-shellcheck.sh

set -eu

VERSION="0.10.0"

log() { printf '%s\n' "$*" >&2; }

# A shellcheck already on PATH wins — don't second-guess a dev's toolchain.
if command -v shellcheck >/dev/null 2>&1; then
    command -v shellcheck
    exit 0
fi

cache_root="${XDG_CACHE_HOME:-$HOME/.cache}/claude-code-recipes/shellcheck/v$VERSION"
bin="$cache_root/shellcheck"

if [ -x "$bin" ]; then
    printf '%s\n' "$bin"
    exit 0
fi

# Map uname -> shellcheck release asset triple.
os="$(uname -s)"
arch="$(uname -m)"
case "$os" in
    Linux)  asset_os="linux" ;;
    Darwin) asset_os="darwin" ;;
    *) log "ensure-shellcheck: unsupported OS '$os'; install shellcheck $VERSION manually"; exit 1 ;;
esac
case "$arch" in
    x86_64|amd64)   asset_arch="x86_64" ;;
    aarch64|arm64)  asset_arch="aarch64" ;;
    *) log "ensure-shellcheck: unsupported arch '$arch'; install shellcheck $VERSION manually"; exit 1 ;;
esac

tarball="shellcheck-v$VERSION.$asset_os.$asset_arch.tar.xz"
url="https://github.com/koalaman/shellcheck/releases/download/v$VERSION/$tarball"

log "ensure-shellcheck: fetching pinned shellcheck $VERSION ($asset_os/$asset_arch)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if ! curl -fsSL "$url" -o "$tmp/$tarball"; then
    log "ensure-shellcheck: download failed: $url"
    exit 1
fi
tar -xJf "$tmp/$tarball" -C "$tmp"

mkdir -p "$cache_root"
# Atomic-ish install: stage then move into place.
mv "$tmp/shellcheck-v$VERSION/shellcheck" "$bin.tmp"
chmod 0755 "$bin.tmp"
mv "$bin.tmp" "$bin"

printf '%s\n' "$bin"
