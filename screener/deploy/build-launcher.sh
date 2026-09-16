#!/bin/bash
# Build and install "Screener Daily.app" -- the TCC identity the LaunchAgent
# runs as. See the header of screener-launcher.c for why this exists at all.
#
#   ./deploy/build-launcher.sh
#
# Then, ONCE, by hand (cannot be scripted -- TCC grants are GUI-only):
#   System Settings > Privacy & Security > Full Disk Access > +
#   > select ~/Applications/Screener Daily.app > toggle it on
#
# Re-running this rebuilds the binary, which changes its cdhash, which can
# make macOS ask for the grant again. Nothing here needs to change day to
# day -- all the logic lives in run-daily.sh, which the bundle only execs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Applications/Screener Daily.app"
BIN="$APP/Contents/MacOS/ScreenerDaily"

mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Screener Daily</string>
    <key>CFBundleDisplayName</key><string>Screener Daily</string>
    <key>CFBundleIdentifier</key><string>com.asitminz.screener.launcher</string>
    <key>CFBundleExecutable</key><string>ScreenerDaily</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <!-- No Dock icon, no menu bar: it execs a shell and exits. -->
    <key>LSBackgroundOnly</key><true/>
</dict>
</plist>
PLIST

cc -O2 -Wall -Wextra \
   -DSCRIPT_PATH="\"$ROOT/deploy/run-daily.sh\"" \
   -o "$BIN" "$ROOT/deploy/screener-launcher.c"

# Ad-hoc signature. TCC needs a stable code identity to hang the grant on; an
# unsigned binary gets re-evaluated and the grant does not stick.
codesign --force --sign - --identifier com.asitminz.screener.launcher "$APP"
codesign --verify --strict "$APP"

echo "built  $APP"
echo "  targets: $ROOT/deploy/run-daily.sh"
echo
echo "NEXT, and it cannot be scripted -- TCC grants are GUI-only:"
echo "  System Settings > Privacy & Security > Full Disk Access > + "
echo "  > $APP > turn it on"
