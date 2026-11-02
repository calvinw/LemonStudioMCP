#!/usr/bin/env bash
# Installs lemon-studio-mcp into .venv and registers it with Claude Code.
# Usage: ./install.sh [path/to/lemonstudio]
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="${1:-${LEMON_STUDIO_BIN:-$(command -v lemonstudio || true)}}"

if [ -n "$BIN" ]; then
  BIN="$(cd "$(dirname "$BIN")" && pwd)/$(basename "$BIN")"
  [ -x "$BIN" ] || { echo "Not an executable: $BIN"; exit 1; }
else
  echo "Lemon Studio's binary not given; lemon_launch and render_project will need LEMON_STUDIO_BIN."
fi

PYTHON="$(command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3)"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python 3.10+ needed"; exit 1; }

echo "Setting up .venv with $PYTHON..."
"$PYTHON" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -e "$DIR"
echo "  ✓ lemon-studio-mcp installed"

if command -v claude >/dev/null; then
  claude mcp remove lemon-studio 2>/dev/null || true
  env_args=()
  [ -n "$BIN" ] && env_args=(-e "LEMON_STUDIO_BIN=$BIN")
  claude mcp add --transport stdio lemon-studio "${env_args[@]}" -- "$DIR/.venv/bin/python" -m lemon_studio_mcp
  echo "  ✓ registered with Claude Code as 'lemon-studio'"
else
  echo "Claude Code not found. Register it yourself:"
  echo "  claude mcp add --transport stdio lemon-studio -e LEMON_STUDIO_BIN=$BIN -- $DIR/.venv/bin/python -m lemon_studio_mcp"
fi

echo
echo "Start Lemon Studio with:  ${BIN:-lemonstudio} --control /tmp/lemon.sock"
echo "(or ask Claude to call lemon_launch)."
