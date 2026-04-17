#!/usr/bin/env bash
# restart-self-healing-daemon.sh
# Stops any existing metroplex-daemon tmux session, rescues in-flight queue entries,
# starts a fresh session, and verifies the heartbeat is renewed within 60 seconds.
set -euo pipefail

# Project root resolved from this script's location: deploy/restart...sh -> ../
PROJ="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
QUEUE_ROOT="${PROJ}/data/self_healing_queue"
IN_FLIGHT="${QUEUE_ROOT}/in_flight/worker-1"
FAILED_DIR="${QUEUE_ROOT}/failed"
HEARTBEAT="${QUEUE_ROOT}/heartbeat-worker-1.txt"
LOG="${PROJ}/data/self_healing_daemon_restart.log"
SESSION="metroplex-daemon"

ts() { date '+%Y-%m-%dT%H:%M:%S'; }

log() { echo "$(ts) $*" | tee -a "${LOG}"; }

log "=== restart-self-healing-daemon START ==="

# --- Step 1: Kill existing session (errors non-fatal) ---
if tmux has-session -t "${SESSION}" 2>/dev/null; then
    log "Killing existing tmux session '${SESSION}'"
    tmux kill-session -t "${SESSION}" || true
else
    log "No existing tmux session '${SESSION}' -- nothing to kill"
fi

# --- Step 2: Rescue in-flight queue entries ---
mkdir -p "${FAILED_DIR}"
if [ -d "${IN_FLIGHT}" ]; then
    for f in "${IN_FLIGHT}"/*.json; do
        [ -e "$f" ] || continue
        base=$(basename "$f" .json)
        dest="${FAILED_DIR}/${base}.restart-orphan-$(date +%s).json"
        mv "$f" "$dest"
        log "Rescued orphan: $(basename "$f") -> $(basename "$dest")"
    done
else
    log "No in-flight directory found at ${IN_FLIGHT} -- skipping rescue"
fi

# --- Step 3: Start fresh tmux session ---
log "Starting new tmux session '${SESSION}' in ${PROJ}"
tmux new-session -d -s "${SESSION}" -c "${PROJ}" "claude --dangerously-skip-permissions"
log "tmux session created"

# --- Step 4: Wait for claude prompt to initialise ---
log "Waiting 20s for Claude prompt to be ready..."
sleep 20

# --- Step 5: Send the skill invocation ---
log "Sending /self-healing-daemon start to tmux session"
tmux send-keys -t "${SESSION}" "/self-healing-daemon start" Enter

# --- Step 6: Wait 30s then verify heartbeat ---
log "Waiting 30s for daemon to write heartbeat..."
sleep 30

if [ ! -f "${HEARTBEAT}" ]; then
    log "ERROR: Heartbeat file not found at ${HEARTBEAT} -- daemon may not have started"
    exit 1
fi

AGE=$(( $(date +%s) - $(stat -c '%Y' "${HEARTBEAT}") ))
if [ "${AGE}" -lt 60 ]; then
    log "OK: Heartbeat is fresh (age=${AGE}s) -- daemon is running"
    log "=== restart-self-healing-daemon COMPLETE (exit 0) ==="
    exit 0
else
    log "ERROR: Heartbeat exists but is ${AGE}s old (>60s) -- daemon did not start cleanly"
    log "=== restart-self-healing-daemon FAILED (exit 1) ==="
    exit 1
fi
