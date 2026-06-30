#!/bin/bash
# Keeps Opera reachable over CDP for market_watcher.py / ad_repricer.py.
# Only intervenes when the CDP port is actually unreachable - a healthy
# session (Opera open, flag set, port answering) is never touched, so this
# never interrupts normal browsing. Recovers from: Opera not running, Opera
# running but macOS session-restore dropped the --remote-debugging-port flag
# (the common case after a reboot).
set -u

CDP_URL="http://localhost:9222/json/version"
LOG="$HOME/Projects/Check/scripts/opera_watchdog.log"
OPERA_BIN="/Applications/Opera.app/Contents/MacOS/Opera"
PROFILE="$HOME/Library/Application Support/com.operasoftware.Opera"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >>"$LOG"; }

if curl -s --max-time 3 "$CDP_URL" >/dev/null 2>&1; then
    exit 0
fi

log "CDP unreachable - recovering Opera"

if pgrep -x Opera >/dev/null; then
    osascript -e 'tell application "Opera" to quit' >/dev/null 2>&1
    for i in $(seq 1 10); do
        pgrep -x Opera >/dev/null || break
        sleep 1
    done
    if pgrep -x Opera >/dev/null; then
        log "Opera didn't quit cleanly, force killing"
        pkill -9 -x Opera
        sleep 1
    fi
fi

log "relaunching Opera with --remote-debugging-port=9222"
nohup "$OPERA_BIN" --remote-debugging-port=9222 --user-data-dir="$PROFILE" >/dev/null 2>&1 &
disown

for i in $(seq 1 25); do
    sleep 1
    if curl -s --max-time 2 "$CDP_URL" >/dev/null 2>&1; then
        log "Opera recovered, CDP answering after ${i}s"
        exit 0
    fi
done
log "Opera relaunch did NOT bring CDP back up after 25s - needs manual look"
