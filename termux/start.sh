#!/data/data/com.termux/files/usr/bin/bash
# Keeps the telegent server and the Cloudflare tunnel running (restarts either if it dies).
# The current public address goes to ~/telegent-url.txt; logs to ~/telegent-logs/.
# Started by Termux:Boot after a reboot, or by hand:  nohup bash ~/telegent/termux/start.sh >/dev/null 2>&1 &
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG=~/telegent-logs
PIDFILE=~/.telegent-start.pid
mkdir -p "$LOG"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "уже запущено (pid $(cat "$PIDFILE"))"; exit 0
fi
echo $$ > "$PIDFILE"
termux-wake-lock 2>/dev/null || true
cd "$REPO"

# keep logs small: the phone runs for weeks
for f in "$LOG"/server.log "$LOG"/tunnel.log; do
  [ -f "$f" ] && [ "$(wc -c < "$f")" -gt 5000000 ] && tail -c 1000000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
done

(
  while true; do
    python server.py serve --port 8765 >> "$LOG/server.log" 2>&1
    echo "$(date '+%F %T') server exited with $?, restarting" >> "$LOG/server.log"
    sleep 3
  done
) &

# Watchdog. A quick tunnel lives only while connected: after a long network outage Cloudflare drops it,
# and cloudflared keeps retrying the dead tunnel forever ("Tunnel not found") without exiting.
# Every minute: if our public address does not answer while the internet works, restart cloudflared;
# the loop below then gets a new address and publishes it.
(
  fails=0
  while true; do
    sleep 60
    url=$(cat ~/telegent-url.txt 2>/dev/null)
    [ -n "$url" ] || continue
    if curl -s -m 20 -o /dev/null "$url/api/info"; then fails=0; continue; fi
    curl -s -m 15 -o /dev/null https://www.cloudflare.com/cdn-cgi/trace || { fails=0; continue; }   # no internet: wait
    fails=$((fails + 1))
    if [ "$fails" -ge 3 ]; then
      echo "$(date '+%F %T') watchdog: $url does not answer for 3 min, restarting cloudflared" >> "$LOG/tunnel.log"
      pkill -f "cloudflared tunnel --no-autoupdate --url"
      fails=0
    fi
  done
) &

while true; do
  cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8765 2>&1 | while IFS= read -r line; do
    echo "$line" >> "$LOG/tunnel.log"
    case "$line" in   # the tunnel is gone for good: don't wait for the watchdog
      *"Tunnel not found"*)
        gone=$((gone + 1))
        if [ "$gone" -ge 5 ]; then
          echo "$(date '+%F %T') tunnel not found on Cloudflare, restarting cloudflared" >> "$LOG/tunnel.log"
          pkill -f "cloudflared tunnel --no-autoupdate --url"
        fi;;
    esac
    url=$(printf '%s' "$line" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | grep -v '^https://api\.' | head -1)
    if [ -n "$url" ] && [ "$url" != "$last_url" ]; then
      last_url="$url"
      echo "$url" > ~/telegent-url.txt
      echo "$(date '+%F %T') $url" >> "$LOG/urls.log"
      # publish it for clients and the permanent page (needs the deploy key, see publish_url.sh)
      [ -f ~/.ssh/telegent_deploy ] && (bash "$REPO/termux/publish_url.sh" "$url" &)
      [ -x ~/telegent-url-hook.sh ] && ~/telegent-url-hook.sh "$url" >> "$LOG/tunnel.log" 2>&1 &
    fi
  done
  echo "$(date '+%F %T') cloudflared exited, restarting" >> "$LOG/tunnel.log"
  sleep 5
done
