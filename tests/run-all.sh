#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> syntax check"
bash -n "$ROOT_DIR/statusbar/statusbar.tmux"
bash -n "$ROOT_DIR/tests"/lib/*.sh "$ROOT_DIR/tests"/*.sh

echo "==> unit: parse/render"
python3 "$ROOT_DIR/tests/test-classify.py"

echo "==> unit: viz classify"
python3 "$ROOT_DIR/tests/test-viz-classify.py"

echo "==> integration: status segment on isolated tmux socket"
"$ROOT_DIR/tests/test-indicator.sh"

echo "==> integration: state coloring on isolated tmux socket"
"$ROOT_DIR/tests/test-colorize.sh"

echo "==> integration: hook-based adapters (claude/codex/kimi)"
"$ROOT_DIR/tests/test-agent.sh"

echo "PASS: all tests"
