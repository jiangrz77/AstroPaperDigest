#!/bin/bash
# Download MathJax 3 for offline LaTeX rendering in the digest page.
#
# The digest page references /static/mathjax/tex-svg-full.js. This script
# fetches the single-file MathJax build with all fonts embedded (~2.2 MB) so
# formulas render even without an internet connection.
#
# Usage: ./download_mathjax.sh   (run once; re-run to restore if missing)
set -euo pipefail
cd "$(dirname "$0")"

DEST="static/mathjax/tex-svg-full.js"
URL="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg-full.js"

mkdir -p "$(dirname "$DEST")"

if [ -f "$DEST" ] && [ "$(wc -c < "$DEST" 2>/dev/null || echo 0)" -gt 1000000 ]; then
    echo "MathJax already present: $DEST ($(du -h "$DEST" | cut -f1))"
    exit 0
fi

echo "Downloading MathJax 3 SVG output (single file, embedded fonts, ~2.2 MB) ..."
curl -fL --retry 3 --connect-timeout 20 -o "$DEST" "$URL"

SIZE="$(wc -c < "$DEST")"
if [ "$SIZE" -lt 1000000 ]; then
    echo "ERROR: download looks wrong ($SIZE bytes). Removing it; check network / proxy." >&2
    rm -f "$DEST"
    exit 1
fi

echo "OK: $DEST ($(du -h "$DEST" | cut -f1))"
echo "Done. Restart AstroPaperDigest.app to see formulas rendered."
