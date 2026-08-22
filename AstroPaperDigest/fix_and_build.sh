#!/bin/bash
# Fix + rebuild the native AstroPaperDigest.app after source changes.
#
# The project .venv is currently broken (no python, no dependencies), which
# makes the app at the workspace root unable to start.
# This script:
#   1. recreates .venv and installs requirements
#   2. regenerates the root native AstroPaperDigest.app
#
# Usage: ./fix_and_build.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 1/2 Recreating .venv ..."
rm -rf .venv
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -r requirements.txt -q
echo "    .venv ready: $(./.venv/bin/python3 --version)"

echo "==> 2/2 Regenerating AstroPaperDigest.app (workspace root) ..."
./build_app.sh

echo ""
echo "Done. Now open:"
echo "  /Users/jerome/Workspace/Personal/AstroPaperDigest.app"
echo ""
echo "Tip: if pip failed above, your default python3 may be too new/old for"
echo "the pyobjc pins; try 'python3.9 -m venv .venv' instead."
