#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════
#  quantbot-pull-deploy.sh — pull-based deployer
#
#  WHY THIS EXISTS
#    GitHub Actions cannot reach this box. The OCI security list allows
#    SSH from one home IP only, and GitHub publishes 5,625 IPv4 runner
#    ranges against an OCI cap of 200 rules per security list — 5.6x over
#    the hard limit, and the list rotates weekly. Allowing them would also
#    mean trusting 28M addresses, which is not meaningfully safer than
#    0.0.0.0/0. A self-hosted runner is out because the repo is PUBLIC:
#    any fork's pull request could execute code on the machine holding the
#    Binance API keys.
#
#    So the box PULLS instead of being pushed to. No inbound port, no ssh
#    key in GitHub, no ORACLE_HOST / ORACLE_SSH_KEY secrets at all.
#
#  HOW IT DIFFERS FROM THE CI JOB IT REPLACES
#    The CI open-position gate FAILS OPEN (CLAUDE.md 7.2 #1): it ran the
#    probe as bare `docker exec ... 2>/dev/null || echo "none"`, so any
#    error — lost docker-group membership, container down, malformed JSON
#    — was swallowed and reported as "no position", and CI would restart
#    the bot mid-trade. Here the probe runs LOCALLY and FAILS CLOSED: any
#    failure is treated as "position open" and the bot is left alone.
#
#  SAFETY PROPERTIES
#    - flock: overlapping runs are impossible
#    - .env is gitignored and never touched; a missing .env aborts
#    - the Docker named volume (all trade state) is never touched
#    - `git reset --hard` makes the box mirror the repo exactly; any local
#      edit is logged before it is discarded, never silently dropped
#    - bot container is skipped while a position is open, everything else
#      still deploys, exactly like the CI gate intended
# ══════════════════════════════════════════════════════════════════════
set -uo pipefail

REPO="${QUANTBOT_REPO:-$HOME/quantbot}"
BRANCH="${QUANTBOT_BRANCH:-main}"
LOG="${QUANTBOT_DEPLOY_LOG:-$HOME/quantbot-deploy.log}"
LOCK="/tmp/quantbot-pull-deploy.lock"

exec 9>"$LOCK" || exit 1
flock -n 9 || exit 0            # a previous run is still going; stay quiet

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"; }

notify() {                       # best-effort Telegram; never fails the deploy
  local msg="$1"
  [ -f "$REPO/.env" ] || return 0
  local tok chat
  tok=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$REPO/.env" | head -1 | cut -d= -f2- | tr -d '"'"'"' ')
  chat=$(grep -E '^TELEGRAM_CHAT_ID='  "$REPO/.env" | head -1 | cut -d= -f2- | tr -d '"'"'"' ')
  [ -n "$tok" ] && [ -n "$chat" ] || return 0
  curl -sS --max-time 15 -o /dev/null \
    "https://api.telegram.org/bot${tok}/sendMessage" \
    -d chat_id="$chat" -d parse_mode=HTML -d text="$msg" || true
}

cd "$REPO" || { log "FATAL: $REPO missing"; exit 1; }
[ -f .env ] || { log "FATAL: .env missing — refusing to deploy"; notify "🚨 <b>Deploy aborted</b>%0A.env missing on the VM"; exit 1; }

git fetch --quiet origin "$BRANCH" 2>/dev/null || { log "fetch failed (network?)"; exit 0; }
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")
[ "$LOCAL" = "$REMOTE" ] && exit 0            # nothing new — exit silently

DIRTY=$(git status --porcelain | wc -l)
[ "$DIRTY" -gt 0 ] && log "WARN: $DIRTY locally modified path(s) will be discarded by reset:
$(git status --porcelain)"

CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE")
log "deploying ${LOCAL:0:8} -> ${REMOTE:0:8}"
log "changed files: $(echo "$CHANGED" | tr '\n' ' ')"

# ── open-position probe: LOCAL, and FAILS CLOSED ──────────────────────
POS="open"
if OUT=$(sudo docker exec quantbot_bot python3 -c '
import json, os, sys
try:
    s = json.load(open(os.environ.get("DATA_DIR", "/app/data") + "/bot_state.json"))
    print("open" if s.get("position") else "flat")
except Exception:
    sys.exit(1)
' 2>/dev/null); then
    POS="$OUT"
else
    log "position probe FAILED — assuming OPEN (fail closed), bot will not be restarted"
fi
log "position: $POS"

git reset --hard "$REMOTE" --quiet || { log "FATAL: reset failed"; notify "🚨 <b>Deploy failed</b>%0Agit reset error"; exit 1; }

# ── which services need rebuilding ────────────────────────────────────
BOT=0; NOTIFIER=0; DASHBOARD=0; INFRA=0
echo "$CHANGED" | grep -qE '^(bot|corpus_manager)\.py$'                         && BOT=1
echo "$CHANGED" | grep -qE '^notifier\.py$'                                     && NOTIFIER=1
echo "$CHANGED" | grep -qE '^dashboard\.py$|^assets/'                           && DASHBOARD=1
echo "$CHANGED" | grep -qE '^Dockerfile$|^\.dockerignore$|^requirements\.txt$|^docker-compose\.yml$|^nginx/' && INFRA=1

RESULT=""
if [ "$INFRA" -eq 1 ]; then
    log "INFRA changed — full rebuild"
    sudo docker compose build >>"$LOG" 2>&1
    if [ "$BOT" -eq 1 ] && [ "$POS" = "open" ]; then
        log "position OPEN — rebuilding everything EXCEPT bot"
        sudo docker compose up -d --no-deps notifier dashboard nginx >>"$LOG" 2>&1
        RESULT="infra (bot skipped: position open)"
    else
        sudo docker compose up -d >>"$LOG" 2>&1
        RESULT="infra (all services)"
    fi
    # nginx.conf is BIND-MOUNTED: `compose up` only recreates a container when
    # the service DEFINITION changes, so a config-only edit is otherwise never
    # reread by the running process (commit 259157d).
    sudo docker compose up -d --force-recreate --no-deps nginx >>"$LOG" 2>&1
else
    [ "$DASHBOARD" -eq 1 ] && { sudo docker compose up -d --no-deps --build dashboard >>"$LOG" 2>&1; RESULT="$RESULT dashboard"; }
    [ "$NOTIFIER"  -eq 1 ] && { sudo docker compose up -d --no-deps --build notifier  >>"$LOG" 2>&1; RESULT="$RESULT notifier"; }
    if [ "$BOT" -eq 1 ]; then
        if [ "$POS" = "flat" ]; then
            sudo docker compose up -d --no-deps --build bot >>"$LOG" 2>&1; RESULT="$RESULT bot"
        else
            log "position OPEN — bot restart SKIPPED (will apply on the next deploy while flat)"
            RESULT="$RESULT (bot skipped: position open)"
        fi
    fi
fi
[ -z "$RESULT" ] && RESULT="no service restart needed"

sleep 8
FAILED=0; STATUS=""
for svc in bot notifier dashboard nginx; do
    st=$(sudo docker inspect --format='{{.State.Status}}' "quantbot_$svc" 2>/dev/null || echo missing)
    STATUS="$STATUS $svc=$st"
    [ "$st" = "running" ] || FAILED=1
done
log "health:$STATUS"

if [ "$FAILED" -eq 0 ]; then
    log "deploy OK — $RESULT"
    notify "✅ <b>Deploy OK</b>%0A<code>${LOCAL:0:8} → ${REMOTE:0:8}</code>%0A${RESULT}%0Aposition was: ${POS}"
else
    log "DEPLOY UNHEALTHY —$STATUS"
    sudo docker compose logs --tail=30 >>"$LOG" 2>&1
    notify "🚨 <b>Deploy UNHEALTHY</b>%0A<code>${REMOTE:0:8}</code>%0A${STATUS}%0ACheck ~/quantbot-deploy.log"
fi
