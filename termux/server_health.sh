#!/data/data/com.termux/files/usr/bin/bash
# Sourced by start.sh. Keep origin recovery separate from tunnel recovery.
server_fails=0

is_managed_server() {
  [ -n "$1" ] && [ "$(cat "$SERVER_PIDFILE" 2>/dev/null)" = "$1" ] &&
    [ "$(readlink "/proc/$1/cwd" 2>/dev/null)" = "$REPO" ] &&
    [ "$(tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null)" = "python server.py serve --port 8765 " ]
}

restart_server() {
  local pid
  pid=$(cat "$SERVER_PIDFILE" 2>/dev/null)
  is_managed_server "$pid" || return
  echo "$(date '+%F %T') watchdog: local server does not answer, restarting server" >> "$LOG/server.log"
  kill -TERM "$pid" 2>/dev/null || return
  sleep 10
  # The server loop may already have started its replacement. Never kill that process.
  is_managed_server "$pid" && kill -KILL "$pid" 2>/dev/null
  return 0
}

check_server() {
  if curl --noproxy '*' -sf --connect-timeout 3 -m 10 -o /dev/null http://127.0.0.1:8765/api/info; then
    server_fails=0
    return 0
  fi
  server_fails=$((server_fails + 1))
  if [ "$server_fails" -ge 3 ]; then
    restart_server
    server_fails=0
  fi
  return 1
}
