#!/bin/bash
# Build AstroPaperDigest.app from source
# Usage: ./build_app.sh
# Requires: macOS with iconutil (built-in)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="AstroPaperDigest"
APP_DIR="../$APP_NAME.app"

echo "Building $APP_NAME.app..."

# Clean previous build
rm -rf "$APP_DIR"

# Create .app bundle structure
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

# 1. Info.plist
cat > "$APP_DIR/Contents/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>AstroPaperDigest</string>
    <key>CFBundleIdentifier</key>
    <string>com.arxivdailydigest.app</string>
    <key>CFBundleName</key>
    <string>AstroPaperDigest</string>
    <key>CFBundleDisplayName</key>
    <string>AstroPaperDigest</string>
    <key>CFBundleVersion</key>
    <string>1.0.2</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.2</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon.icns</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
EOF

# 2. Launcher script
cat > "$APP_DIR/Contents/MacOS/$APP_NAME" << 'LAUNCHER'
#!/bin/bash
# AstroPaperDigest - macOS App Launcher
# Double-click to run the full pipeline

# Log file for debugging
LOG_FILE="$HOME/Library/Logs/AstroPaperDigest.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "=== $(date) ==="

# macOS .app launches with minimal PATH - fix it
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$PATH"

# Force native arm64 on Apple Silicon (avoid Rosetta x86_64 mismatch with arm64 packages)
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ] && [ "$(arch)" != "arm64" ]; then
    exec arch -arm64 "$0" "$@"
fi

# Determine project directory
# Script is at: ROOT/AstroPaperDigest.app/Contents/MacOS/AstroPaperDigest
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PROJECT_DIR="$(cd "$APP_DIR/../AstroPaperDigest" && pwd)"

cd "$PROJECT_DIR"

# Detect Python: prefer .venv, then venv, then system python3
# On first run (or if venv is broken/moved/wrong arch), create .venv and install dependencies
if ! "$PROJECT_DIR/.venv/bin/python3" -c "import pydantic" 2>/dev/null; then
    rm -rf "$PROJECT_DIR/.venv"
    osascript -e 'display notification "Setting up Python environment..." with title "AstroPaperDigest"'
    python3 -m venv "$PROJECT_DIR/.venv"
    "$PROJECT_DIR/.venv/bin/pip" install --upgrade pip -q
    "$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q
fi

if [ -f "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python3"
elif [ -f "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON_BIN="$PROJECT_DIR/venv/bin/python3"
else
    PYTHON_BIN="python3"
fi

# Check if server is already running on port 5123
echo "Checking for existing server..."
STATUS_RESPONSE=$(curl -fsS --connect-timeout 2 http://127.0.0.1:5123/status 2>/dev/null || true)
if echo "$STATUS_RESPONSE" | grep -q '"app":"AstroPaperDigest"'; then
    echo "AstroPaperDigest is already running. Opening browser..."
    osascript -e 'display notification "AstroPaperDigest is already running." with title "AstroPaperDigest"' 2>/dev/null
    open "http://127.0.0.1:5123"
    exit 0
fi

# Never kill an unrelated process that owns the configured port
EXISTING_PID=$(lsof -ti :5123 2>/dev/null)
if [ -n "$EXISTING_PID" ]; then
    echo "ERROR: Port 5123 is used by another process (PID: $EXISTING_PID)."
    osascript -e 'display alert "AstroPaperDigest could not start" message "Port 5123 is being used by another application."' 2>/dev/null
    exit 1
fi

# Start Flask in background
"$PYTHON_BIN" "$PROJECT_DIR/src/gui.py" --no-browser &
FLASK_PID=$!

echo "Flask starting (PID: $FLASK_PID)..."

# Wait for server to be ready, then open browser
SERVER_READY=0
for i in $(seq 1 30); do
    sleep 1
    if "$PYTHON_BIN" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5123', timeout=2)" 2>/dev/null; then
        SERVER_READY=1
        echo "Server ready after ${i}s"
        break
    fi
    if command -v curl >/dev/null 2>&1 && curl -s -o /dev/null http://127.0.0.1:5123 2>/dev/null; then
        SERVER_READY=1
        echo "Server ready after ${i}s (curl)"
        break
    fi
done

if [ $SERVER_READY -eq 1 ]; then
    echo "Opening browser..."
    open "http://127.0.0.1:5123"
else
    echo "WARNING: Server didn't start within 30s, trying to open browser anyway..."
    open "http://127.0.0.1:5123"
fi

# Keep app alive while Flask runs
wait $FLASK_PID
LAUNCHER

chmod +x "$APP_DIR/Contents/MacOS/$APP_NAME"

# 3. App icon
PREBUILT_ICNS="assets/AppIcon.icns"
ICONSET_DIR="assets/icon.iconset"
ICNS_FILE="$APP_DIR/Contents/Resources/AppIcon.icns"

if [ -f "$PREBUILT_ICNS" ]; then
    echo "Installing prebuilt AppIcon.icns..."
    cp "$PREBUILT_ICNS" "$ICNS_FILE"
elif [ -d "$ICONSET_DIR" ]; then
    echo "Generating AppIcon.icns from iconset..."
    if ! iconutil -c icns "$ICONSET_DIR" -o "$ICNS_FILE"; then
        echo "WARNING: Icon conversion failed; app will use the default icon."
    fi
elif [ -f "assets/icon_1024.png" ]; then
    echo "Generating iconset from icon_1024.png..."
    TMPICON=$(mktemp -d)/icon.iconset
    mkdir -p "$TMPICON"
    sips -z 16 16     "assets/icon_1024.png" --out "$TMPICON/icon_16x16.png"      >/dev/null
    sips -z 32 32     "assets/icon_1024.png" --out "$TMPICON/icon_16x16@2x.png"   >/dev/null
    sips -z 32 32     "assets/icon_1024.png" --out "$TMPICON/icon_32x32.png"      >/dev/null
    sips -z 64 64     "assets/icon_1024.png" --out "$TMPICON/icon_32x32@2x.png"   >/dev/null
    sips -z 128 128   "assets/icon_1024.png" --out "$TMPICON/icon_128x128.png"    >/dev/null
    sips -z 256 256   "assets/icon_1024.png" --out "$TMPICON/icon_128x128@2x.png" >/dev/null
    sips -z 256 256   "assets/icon_1024.png" --out "$TMPICON/icon_256x256.png"    >/dev/null
    sips -z 512 512   "assets/icon_1024.png" --out "$TMPICON/icon_256x256@2x.png" >/dev/null
    sips -z 512 512   "assets/icon_1024.png" --out "$TMPICON/icon_512x512.png"    >/dev/null
    cp "assets/icon_1024.png" "$TMPICON/icon_512x512@2x.png"
    iconutil -c icns "$TMPICON" -o "$ICNS_FILE"
    rm -rf "$(dirname "$TMPICON")"
else
    echo "WARNING: No icon assets found, app will use default icon."
fi

# 4. Python environment
echo "Setting up Python environment..."
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.9+ first."
    echo "  brew install python3"
    exit 1
fi

if [ ! -f ".venv/bin/python3" ] || ! .venv/bin/python3 -c "import sys; assert sys.prefix != sys.base_prefix" 2>/dev/null; then
    rm -rf .venv
    python3 -m venv .venv
fi

echo "Installing dependencies..."
if ! .venv/bin/python3 -m pip install -r requirements.txt -q; then
    echo "ERROR: Failed to install dependencies."
    exit 1
fi

echo ""
echo "Done! Created: $APP_NAME.app"
echo "Python venv ready at .venv/"
echo "Double-click $APP_NAME.app to launch AstroPaperDigest."
