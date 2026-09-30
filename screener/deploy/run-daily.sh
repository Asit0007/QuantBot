#!/bin/bash
# Scheduled entry point for the value+RSI screener's daily Telegram digest,
# driven by the LaunchAgent in com.asitminz.screener.daily.plist.
#
# Runs from a DEDICATED worktree (this directory's repo root, branch
# screener-scheduled) -- NOT the main quant_bot checkout. value-rsi-screener
# and multi-market were merged to main 2026-09-23 (both verified inert for
# the live bot: value-rsi-screener's only production-relevant change was a
# docker-compose.yml security fix; multi-market never touches a file the
# deployer classifies), so this worktree now mirrors origin/main instead --
# the same pull pattern the VM already uses for the live bot (root CLAUDE.md
# §4.9: "the box mirrors the repo exactly"). Still a dedicated worktree, not
# the interactive quant_bot checkout: that one gets switched to arbitrary
# branches for live-bot work, and a scheduled `reset --hard` landing there
# would discard whatever was checked out. A local commit on `main` has no
# effect here until it's pushed.
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

# launchd replays a missed 19:00 at the next wake, and that wake can be a
# 10-second maintenance DarkWake with no network: on 2026-09-23 and 2026-09-29
# DNS failed, the run exited 1 after a few seconds and no digest was sent.
# So wait for the network first. The limit counts ATTEMPTS, not wall-clock
# time: a process frozen by sleep resumes at the next wake and keeps trying,
# which turns a DarkWake firing into "run at the first real wake".
wait_for_network() {
  local url="${NET_CHECK_URL:-https://github.com}" tries="${NET_WAIT_TRIES:-60}" i
  for ((i = 1; i <= tries; i++)); do
    curl -sI --max-time 5 -o /dev/null "$url" && return 0
    [ "$i" -eq 1 ] && echo "-- no network yet, waiting (up to $tries tries)"
    sleep "${NET_WAIT_SLEEP:-10}"
  done
  echo "! no network after $tries tries -- nothing was run"
  return 1
}
wait_for_network || exit 1

cd "$REPO_ROOT" || exit 1
echo "-- syncing worktree to origin/main"
git fetch origin main
git reset --hard origin/main

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
