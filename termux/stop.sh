#!/data/data/com.termux/files/usr/bin/bash
# Stops everything termux/start.sh started.
rm -f ~/.telegent-start.pid
pkill -f "termux/start.sh"
pkill -f "server.py serve"
pkill -x cloudflared
echo "остановлено"
