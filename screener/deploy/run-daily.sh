#!/bin/bash
# Scheduled entry point for the value+RSI screener's daily Telegram digest,
# driven by the LaunchAgent in com.asitminz.screener.daily.plist.
#
# Runs from a DEDICATED worktree (this directory's repo root, branch
# screener-scheduled) -- NOT the main quant_bot checkout. value-rsi-screener
# is a research branch (quant_bot's root CLAUDE.md: never merge to main), and
# the main checkout gets switched to `main` whenever there's live-bot work on
# bot.py/notifier.py. A scheduled job pointed at that checkout would silently
# stop finding screener/ the moment that happened. Instead this worktree
# mirrors origin/value-rsi-screener on every run -- the same pull pattern the
# VM already uses for the live bot (root CLAUDE.md §4.9: "the box mirrors the
# repo exactly"). A local commit on value-rsi-screener has no effect here
# until it's pushed.
#
# Same launchd gotchas as JobPipe's run-daily.sh (deploy/build-launcher.sh in
# that repo has the long version): no profile/PATH, no venv, no cwd, buffered
# output lost on a kill. All handled below.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"     # .../quant_bot-screener/screener
REPO_ROOT="$(cd "$ROOT/.." && pwd)"          # .../quant_bot-screener

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PYTHONUNBUFFERED=1
export LANG="${LANG:-en_US.UTF-8}"

PY="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily-$(date +%F).log"

exec > >(tee -a "$LOG") 2>&1

echo "==============================================================="
echo "screener daily  --  $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "  repo:   $REPO_ROOT"
echo "  python: $PY"
echo "==============================================================="

cd "$REPO_ROOT" || exit 1
echo "-- syncing worktree to origin/value-rsi-screener"
git fetch origin value-rsi-screener
git reset --hard origin/value-rsi-screener

cd "$ROOT" || exit 1

if [ ! -x "$PY" ]; then
  echo "! no venv at $PY -- run: python3.11 -m venv .venv && .venv/bin/pip install -r requirements-screener.txt"
  exit 1
fi

if [ ! -f "$ROOT/.env" ]; then
  echo "! no .env at $ROOT/.env -- this worktree needs its own copy (gitignored, not carried by git reset)"
  exit 1
fi

start=$(date +%s)
PYTHONPATH="$REPO_ROOT" "$PY" -m screener.main
rc=$?
echo
echo "--- exit $rc after $(( ($(date +%s) - start) / 60 ))m $(( ($(date +%s) - start) % 60 ))s"

# Keep a month.
find "$LOG_DIR" -name 'daily-*.log' -type f -mtime +30 -delete 2>/dev/null

exit $rc
