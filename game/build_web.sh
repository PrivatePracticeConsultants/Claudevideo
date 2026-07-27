#!/usr/bin/env bash
# Build the HTML5 version of LAST LINE.
#
#   ./game/build_web.sh                        # -> build/web/index.html
#   GODOT=/path/to/godot ./game/build_web.sh
#
# Requires Godot's web export templates for the matching engine version. If they
# are missing the export fails with a bare "configuration errors" and no detail,
# so this script checks for them first and says what to do about it.
#
# The result is served over plain HTTP with no special headers - see
# export_presets.cfg for why that is worth the single-threaded trade.
set -euo pipefail

GODOT="${GODOT:-godot}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$(cd "$HERE/.." && pwd)/build/web"

if ! command -v "$GODOT" >/dev/null 2>&1 && [ ! -x "$GODOT" ]; then
	echo "Godot 4.x not found. Set GODOT=/path/to/godot or put it on PATH." >&2
	exit 127
fi

VERSION="$("$GODOT" --headless --version 2>/dev/null | head -1 | sed 's/\.official.*//')"
TEMPLATES="${HOME}/.local/share/godot/export_templates/${VERSION}"
if [ ! -f "${TEMPLATES}/web_nothreads_release.zip" ]; then
	echo "Missing web export templates for ${VERSION}." >&2
	echo "Expected: ${TEMPLATES}/web_nothreads_release.zip" >&2
	echo "" >&2
	echo "Install them from the editor (Editor > Manage Export Templates), or:" >&2
	echo "  curl -L -o templates.tpz \\" >&2
	echo "    https://github.com/godotengine/godot-builds/releases/download/${VERSION}/Godot_v${VERSION}_export_templates.tpz" >&2
	echo "  mkdir -p '${TEMPLATES}' && cd '${TEMPLATES}' && unzip -j -o templates.tpz 'templates/*'" >&2
	exit 1
fi

rm -rf "$OUT"
mkdir -p "$OUT"
"$GODOT" --headless --path "$HERE" --import >/dev/null 2>&1 || true
"$GODOT" --headless --path "$HERE" --export-release "Web" "../build/web/index.html"

echo ""
echo "Built $(du -sh "$OUT" | cut -f1) into $OUT"
echo "Serve it (it will not run from file://, browsers block wasm there):"
echo "  python3 -m http.server 8000 --directory $OUT"
echo "  then open http://localhost:8000/"
