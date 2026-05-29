#!/usr/bin/env bash
# Self-healing daemon health monitor.
# Runs via cron every 5 min. Edge-triggered Telegram alerts only
# (no spam on repeat-same-state ticks).
set -u

QUEUE_DIR=/home/apexaipc/projects/metroplex/data/self_healing_queue
STATE_FILE=/tmp/metroplex-daemon-monitor.state
HEARTBEAT_MAX_AGE=600      # seconds; must exceed daemon keep-alive cadence (60s) with margin. in_flight watchdog at 1800s independently catches stuck tasks.
IN_FLIGHT_MAX_AGE=1800     # seconds (30 min)

set +u; source /home/apexaipc/.env.shared; set -u
TOKEN=${METROPLEX_TELEGRAM_BOT_TOKEN:-}
CHAT_ID=${METROPLEX_TELEGRAM_CHAT_ID:-}

notify() {
    local msg="$1"
    if [[ -n "$TOKEN" && -n "$CHAT_ID" ]]; then
        curl -sS --max-time 10 \
            "https://api.telegram.org/bot${TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${CHAT_ID}" \
            --data-urlencode "text=${msg}" >/dev/null || true
    fi
    echo "[$(date -Iseconds)] $msg"
}

heartbeat_age() {
    local f="$QUEUE_DIR/heartbeat-worker-1.txt"
    [[ -f "$f" ]] || { echo 999999; return; }
    echo $(( $(date +%s) - $(stat -c %Y "$f") ))
}

oldest_in_flight_age() {
    local dir="$QUEUE_DIR/in_flight/worker-1"
    [[ -d "$dir" ]] || { echo 0; return; }
    local now=$(date +%s)
    local oldest=0
    for f in "$dir"/*.json; do
        [[ -f "$f" ]] || continue
        local age=$(( now - $(stat -c %Y "$f") ))
        (( age > oldest )) && oldest=$age
    done
    echo $oldest
}

hb=$(heartbeat_age)
inflight=$(oldest_in_flight_age)

if (( hb > HEARTBEAT_MAX_AGE )); then
    state="dead"
    detail="heartbeat ${hb}s stale (threshold ${HEARTBEAT_MAX_AGE}s)"
elif (( inflight > IN_FLIGHT_MAX_AGE )); then
    state="stuck"
    detail="in_flight task ${inflight}s old (threshold ${IN_FLIGHT_MAX_AGE}s)"
else
    state="ok"
    detail="heartbeat ${hb}s ago, in_flight ${inflight}s old"
fi

prev=$(cat "$STATE_FILE" 2>/dev/null || echo "unknown")

if [[ "$state" != "$prev" ]]; then
    case "$state" in
        dead)  notify "ALERT: self-healing daemon appears DEAD. $detail" ;;
        stuck) notify "WARN: self-healing daemon appears STUCK. $detail" ;;
        ok)
            if [[ "$prev" != "unknown" ]]; then
                notify "RECOVERED: self-healing daemon is OK. $detail"
            fi
            ;;
    esac
    echo "$state" > "$STATE_FILE"
fi

exit 0
