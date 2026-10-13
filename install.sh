#!/bin/sh
# Install Swift Agent's launcher and Claude Code subagent for ukisai models on splash.
# Safe to re-run: the links are updated in place.
set -eu

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

die() {
    echo "install: $*" >&2
    exit 1
}

[ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] || die "Apple Silicon macOS only"
command -v python3 >/dev/null || die "python3 not found: brew install python"
command -v splash >/dev/null || die "splash not found: brew install incoai/tap/splash"

mkdir -p "$HOME/.local/bin" "$HOME/.claude/agents"
ln -sf "$REPO_DIR/swift-ask" "$HOME/.local/bin/swift-ask"
ln -sf "$REPO_DIR/agents/swift.md" "$HOME/.claude/agents/swift.md"

echo "Installed Swift Agent. Try: swift-ask --no-think 'What is 17 * 23?'"
