#!/bin/sh
set -eu

DISPLAY_VALUE="${DISPLAY:-:1}"
VNC_RESOLUTION="${VNC_RESOLUTION:-1366x768x24}"
VNC_PORT="${VNC_PORT:-5901}"
NOVNC_PORT="${NOVNC_PORT:-6080}"

export DISPLAY="$DISPLAY_VALUE"

mkdir -p /app/.indah_session /app/run_state /app/ui_runs /app/ui_uploads

Xvfb "$DISPLAY" -screen 0 "$VNC_RESOLUTION" -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
sleep 1

fluxbox >/tmp/fluxbox.log 2>&1 &

if [ -n "${VNC_PASSWORD:-}" ]; then
  x11vnc -display "$DISPLAY" -localhost -forever -shared -rfbport "$VNC_PORT" -passwd "$VNC_PASSWORD" >/tmp/x11vnc.log 2>&1 &
else
  x11vnc -display "$DISPLAY" -localhost -forever -shared -rfbport "$VNC_PORT" -nopw >/tmp/x11vnc.log 2>&1 &
fi

websockify --web=/usr/share/novnc/ "0.0.0.0:$NOVNC_PORT" "127.0.0.1:$VNC_PORT" >/tmp/novnc.log 2>&1 &

exec python indah_ui.py --host 0.0.0.0 --port 8765
