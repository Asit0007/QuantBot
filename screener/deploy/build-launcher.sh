#!/bin/bash
# Build and install "Screener Daily.app" -- the TCC identity the LaunchAgent
# runs as. See the header of screener-launcher.c for why this exists at all.
#
#   ./deploy/build-launcher.sh
#
# JobPipe's own docs (this pattern's origin) say a manual System Settings >
# Privacy & Security > Full Disk Access grant is required. **Verified live
# 2026-09-17 that this is NOT what actually happened for either app**:
# `sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db "SELECT
# service, client, auth_value FROM access WHERE client LIKE '%asitminz%'"`
# shows JobPipe's launcher holds `kTCCServiceSystemPolicyDocumentsFolder`
# (Documents Folder, a narrower grant than Full Disk Access) -- granted
# 2026-09-10, and Asit confirms he never manually toggled anything for it.
# The screener launcher had ZERO TCC row at all and still ran clean end to
# end via `launchctl kickstart` the same day this was written (git fetch,
# venv, full 5-market run, real Telegram send) -- no dialog, no block. Why
# JobPipe's docs describe a manual step that neither app actually needed is
# unresolved; if a FUTURE run ever fails with exit 126 / can't read the repo,
# grant Documents Folder access (System Settings > Privacy & Security >
# Files and Folders, NOT the Full Disk Access pane) to this app -- but don't
# assume that step is needed before trying.
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
echo "Try 'launchctl kickstart -p gui/\$(id -u)/com.asitminz.screener.daily' now."
echo "It worked with no manual TCC grant on 2026-09-17 -- see this script's"
echo "header before assuming you need to touch System Settings."
