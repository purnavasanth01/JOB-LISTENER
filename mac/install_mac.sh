#!/bin/bash
# Installs the resume runner as a macOS background job: runs at login, after wake, and every 5 minutes while awake.
# Re-run any time (idempotent). Remove with: bash mac/install_mac.sh --uninstall
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.purna.job-listener.resume"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/job-listener-resume.log"

if [ "$1" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Resume runner removed (your resumes in INTERNSHIP/RESUMES_2 are untouched)."
  exit 0
fi

if [ ! -x "$DIR/.venv/bin/python" ]; then
  python3 -m venv "$DIR/.venv"
  "$DIR/.venv/bin/pip" install -q --upgrade pip
  "$DIR/.venv/bin/pip" install -q playwright pdfminer.six openpyxl
  "$DIR/.venv/bin/python" -m playwright install chromium
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$DIR/.venv/bin/python</string><string>$DIR/resume_runner.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Runs at login, after wake, and every 5 minutes. Log: $LOG"
