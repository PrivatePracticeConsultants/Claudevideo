#!/usr/bin/env bash
# Run the LAST LINE test suite headlessly.
#
#   ./game/run_tests.sh                    # uses `godot` from PATH
#   GODOT=/path/to/godot ./game/run_tests.sh
#
# The --import pass is not optional on a fresh checkout: Godot resolves
# `class_name` globals from .godot/global_script_class_cache.cfg, which is a
# build artifact and is not committed. Without it every test file fails to parse
# with "Identifier not declared".
set -euo pipefail

GODOT="${GODOT:-godot}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v "$GODOT" >/dev/null 2>&1 && [ ! -x "$GODOT" ]; then
	echo "Godot 4.x not found. Set GODOT=/path/to/godot or put it on PATH." >&2
	exit 127
fi

# --import writes the class cache and exits; it chatters about the missing main
# scene on a first run, which is harmless here.
"$GODOT" --headless --path "$HERE" --import >/dev/null 2>&1 || true

exec "$GODOT" --headless --path "$HERE" --script res://tests/run_tests.gd
