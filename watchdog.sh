#!/bin/bash
# ============================================================
# watchdog.sh - Highrise Bot Crash-Recovery
# ============================================================
# Prüft alle 30 Sekunden ob bot.py und stream_master.py laufen.
# Falls einer crashed → sofort beide neu starten.
# Alle 5 Minuten: Stream-Healthcheck (ffmpeg-Prozess prüfen).
# ============================================================

BOT_DIR="/opt/highrise-bot"
LOG="$BOT_DIR/logs/watchdog.log"
MAX_BOT_MEMORY_MB=500      # Bot soll nicht mehr als 500MB RAM
MAX_STREAM_MEMORY_MB=200   # Stream-Master soll nicht mehr als 200MB RAM
RESTART_COOLDOWN=120       # Sekunden zwischen Restarts (verhindert Restart-Loop)
HEALTHCHECK_INTERVAL=300   # Sekunden zwischen Healthchecks (5 Min)

mkdir -p "$BOT_DIR/logs"

log() {
    echo "$(date '+%Y-%m-%d %H:%M%S') [WATCHDOG] $1" >> "$LOG"
}

last_restart=0
last_healthcheck=0

log "========================================="
log "Watchdog v1.0 gestartet"
log "Bot: $BOT_DIR/bot.py"
log "Stream: $BOT_DIR/stream_master.py"
log "========================================="

while true; do
    now=$(date +%s)
    restarted=false

    # ── 1. Bot-Status prüfen ──
    bot_status=$(pm2 jlist 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for p in data:
    if p['name'] == 'highrise-bot':
        print(p['pm2_env']['status'])
        break
else:
    print('missing')
" 2>/dev/null || echo "error")

    if [ "$bot_status" != "online" ] && [ "$bot_status" != "stopping" ] && [ "$bot_status" != "launching" ]; then
        elapsed=$((now - last_restart))
        if [ "$elapsed" -ge "$RESTART_COOLDOWN" ]; then
            log "⚠️ Bot Status: $bot_status → Neustart beider Services..."
            cd "$BOT_DIR"
            pm2 restart stream-master --silent 2>/dev/null || true
            sleep 2
            pm2 restart highrise-bot --silent 2>/dev/null || true
            log "✅ Bot + Stream-Master neu gestartet"
            last_restart=$now
            restarted=true
        else
            log "⚠️ Bot Status: $bot_status — Cooldown (${elapsed}s/${RESTART_COOLDOWN}s)"
        fi
    fi

    # ── 2. Stream-Master Status prüfen ──
    stream_status=$(pm2 jlist 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for p in data:
    if p['name'] == 'stream-master':
        print(p['pm2_env']['status'])
        break
else:
    print('missing')
" 2>/dev/null || echo "error")

    if [ "$stream_status" != "online" ] && [ "$stream_status" != "stopping" ] && [ "$stream_status" != "launching" ]; then
        elapsed=$((now - last_restart))
        if [ "$elapsed" -ge "$RESTART_COOLDOWN" ]; then
            log "⚠️ Stream-Master Status: $stream_status → Neustart..."
            cd "$BOT_DIR"
            pm2 restart stream-master --silent 2>/dev/null || true
            log "✅ Stream-Master neu gestartet"
            last_restart=$now
            restarted=true
        else
            log "⚠️ Stream-Master Status: $stream_status — Cooldown (${elapsed}s/${RESTART_COOLDOWN}s)"
        fi
    fi

    # ── 3. Memory-Check (Bot zu viel RAM?) ──
    bot_mem=$(pm2 jlist 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for p in data:
    if p['name'] == 'highrise-bot' and p['pm2_env']['status'] == 'online':
        print(p['memory'] // 1024 // 1024)
        break
" 2>/dev/null || echo "0")

    if [ "$bot_mem" -gt "$MAX_BOT_MEMORY_MB" ] 2>/dev/null; then
        elapsed=$((now - last_restart))
        if [ "$elapsed" -ge "$RESTART_COOLDOWN" ]; then
            log "⚠️ Bot RAM zu hoch: ${bot_mem}MB > ${MAX_BOT_MEMORY_MB}MB → Neustart..."
            cd "$BOT_DIR"
            pm2 restart highrise-bot --silent 2>/dev/null || true
            sleep 1
            pm2 restart stream-master --silent 2>/dev/null || true
            log "✅ Bot + Stream wegen RAM neu gestartet"
            last_restart=$now
            restarted=true
        fi
    fi

    # ── 4. Stream-Healthcheck (alle 5 Min) ──
    healthcheck_elapsed=$((now - last_healthcheck))
    if [ "$healthcheck_elapsed" -ge "$HEALTHCHECK_INTERVAL" ]; then
        last_healthcheck=$now

        # Prüfen ob ffmpeg läuft (für Icecast)
        ffmpeg_count=$(pgrep -c ffmpeg 2>/dev/null || echo "0")
        if [ "$ffmpeg_count" -lt 1 ] && [ "$stream_status" = "online" ]; then
            log "⚠️ ffmpeg nicht gefunden aber stream-master online → Restart stream"
            cd "$BOT_DIR"
            pm2 restart stream-master --silent 2>/dev/null || true
            log "✅ Stream-Master wegen fehlendem ffmpeg neu gestartet"
            last_restart=$now
            restarted=true
        fi

        # Icecast prüfen
        if ! pgrep -x icecast > /dev/null 2>&1; then
            log "⚠️ Icecast nicht gefunden! Versuch Neustart..."
            sudo systemctl restart icecast2 2>/dev/null || true
            log "Icecast Neustart versucht"
        fi

        # FIFO prüfen
        if [ ! -p "/tmp/highrise-audio-pipe" ]; then
            log "⚠️ FIFO /tmp/highrise-audio-pipe fehlt → Temp-Blackout möglich"
        fi
    fi

    # ── 5. PN2 Auto-Restart aktivieren (falls disabled) ──
    # PM2 watching ist disabled, aber der Autorestart nach Crash soll aktiv sein
    pm2 startup systemd -u nico --hp /home/nico > /dev/null 2>&1 || true

    sleep 60
done
