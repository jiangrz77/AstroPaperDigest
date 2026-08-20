#!/bin/bash
# Build a release zip + version.json for publishing a GitHub Release.
# Usage: ./release.sh   (run AFTER: git tag vX.Y.Z && git push origin vX.Y.Z)
set -euo pipefail
cd "$(dirname "$0")"

VERSION="$(cat version.txt | tr -d '[:space:]')"
case "$VERSION" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) echo "ERROR: version.txt contains an invalid version (expected x.y.z, got: '$VERSION')" >&2; exit 1 ;;
esac

TAG="v$VERSION"
ZIP="AstroPaperDigest-$TAG.zip"

if ! git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "ERROR: git tag $TAG does not exist."
    echo "  Run first:"
    echo "    git tag $TAG && git push origin $TAG"
    exit 1
fi

echo "==> Building source package $ZIP from tag $TAG ..."
git archive --format=zip -o "$ZIP" "$TAG"

SHA="$(shasum -a 256 "$ZIP" | awk '{print $1}')"
echo "==> SHA-256: $SHA"

cat > version.json <<EOF
{
  "version": "$VERSION",
  "tag": "$TAG",
  "url": "https://github.com/jiangrz77/AstroPaperDigest/releases/download/$TAG/$ZIP",
  "sha256": "$SHA",
  "min_system_version": "10.15"
}
EOF

echo ""
echo "Done! Next steps (GitHub web UI):"
echo "  1. Open https://github.com/jiangrz77/AstroPaperDigest/releases/new"
echo "  2. Select tag $TAG, set the title to $TAG"
echo "  3. Write release notes and paste this line (the client uses it to verify the package):"
echo "     SHA256: $SHA"
echo "  4. Attach $ZIP (you may also attach version.json)"
echo "  5. Click Publish release (do NOT mark as Pre-release)"
