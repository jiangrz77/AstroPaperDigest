#!/bin/bash
set -e

cd "$(dirname "$0")/AstroPaperDigest"
./build_app.sh
echo ""
read -r -p "Done! Press Enter to close..."
