#!/bin/bash
# Build release artifacts for a GitHub Release.
# Usage: ./release.sh   (run AFTER: git tag vX.Y.Z && git push origin vX.Y.Z)
#
# Produces (all under dist/):
#   AstroPaperDigest-v<v>.source.zip  source package (Install.command / dev channel)
#   AstroPaperDigest-v<v>.app.zip     self-contained update package (dmg channel)
#   AstroPaperDigest-<v>.dmg          drag-to-install disk image
#   version.json                      manifest used by self-hosted update mirrors
set -euo pipefail
cd "$(dirname "$0")"

VERSION="$(cat version.txt | tr -d '[:space:]')"
case "$VERSION" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) echo "ERROR: version.txt contains an invalid version (expected x.y.z, got: '$VERSION')" >&2; exit 1 ;;
esac
TAG="v$VERSION"

if ! git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "ERROR: git tag $TAG does not exist."
    echo "  Run first:"
    echo "    git tag $TAG && git push origin $TAG"
    exit 1
fi

echo "==> Building self-contained app + dmg (build_dmg.sh) ..."
./build_dmg.sh

echo "==> Building source package ..."
SOURCE_ZIP="dist/AstroPaperDigest-$TAG.source.zip"
rm -f "$SOURCE_ZIP"
git archive --format=zip -o "$SOURCE_ZIP" "$TAG"

APP_ZIP="dist/AstroPaperDigest-$TAG.app.zip"
DMG="dist/AstroPaperDigest-$VERSION.dmg"

SHA_SOURCE="$(shasum -a 256 "$SOURCE_ZIP" | awk '{print $1}')"
SHA_APP="$(shasum -a 256 "$APP_ZIP" | awk '{print $1}')"
SHA_DMG="$(shasum -a 256 "$DMG" | awk '{print $1}')"

cat > dist/version.json <<EOF
{
  "version": "$VERSION",
  "tag": "$TAG",
  "url": "https://github.com/jiangrz77/AstroPaperDigest/releases/download/$TAG/AstroPaperDigest-$TAG.app.zip",
  "sha256": "$SHA_APP",
  "min_system_version": "10.15"
}
EOF

shasum -a 256 "$SOURCE_ZIP" "$APP_ZIP" "$DMG" > dist/SHA256SUMS

echo ""
echo "Done! Artifacts in dist/:"
echo "  $SOURCE_ZIP   SHA256: $SHA_SOURCE"
echo "  $APP_ZIP      SHA256: $SHA_APP"
echo "  $DMG          SHA256: $SHA_DMG"
echo ""
echo "Next steps (GitHub web UI):"
echo "  1. Open https://github.com/jiangrz77/AstroPaperDigest/releases/new"
echo "  2. Select tag $TAG, set the title to $TAG"
echo "  3. Write release notes and paste these lines (clients verify the packages):"
echo "     SHA256 (source zip): $SHA_SOURCE"
echo "     SHA256 (app zip):    $SHA_APP"
echo "  4. Attach $SOURCE_ZIP, $APP_ZIP and $DMG"
echo "  5. Click Publish release (do NOT mark as Pre-release)"