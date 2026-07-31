#!/usr/bin/env bash
# Durable local Intro Show CRM v2 (uvicorn, no --reload).
# Double-forks so the process survives shell/agent teardown (macOS-friendly).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PORT="${PORT:-8000}"
LOG="${LOG:-$ROOT/tmp_meeting/uvicorn-v2.log}"
PIDFILE="${PIDFILE:-$ROOT/tmp_meeting/uvicorn-v2.pid}"
PY="${PY:-$ROOT/venv_latest/bin/python}"
mkdir -p "$(dirname "$LOG")"

healthy() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 "http://127.0.0.1:${PORT}/login" || true)"
  [[ "$code" == "200" ]]
}

if [[ -f "$PIDFILE" ]]; then
  old="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
    if healthy; then
      echo "Already healthy pid=$old port=$PORT"
      echo "URL=http://127.0.0.1:${PORT}/login"
      echo "LOG=$LOG"
      exit 0
    fi
    echo "Stale/unhealthy pid=$old — stopping"
    kill "$old" 2>/dev/null || true
    sleep 1
  fi
fi

if lsof -i ":$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  if healthy; then
    echo "Port $PORT already healthy (foreign listener)"
    echo "URL=http://127.0.0.1:${PORT}/login"
    exit 0
  fi
  echo "Port $PORT busy but not healthy; try: PORT=8001 $0" >&2
  exit 1
fi

rm -f "$PIDFILE"
: > "$LOG"

"$PY" - "$ROOT" "$PY" "$PORT" "$LOG" "$PIDFILE" <<'PY'
import os, sys, time
root, py, port, log, pidfile = sys.argv[1:6]
args = [py, "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", port]

pid = os.fork()
if pid > 0:
    for _ in range(100):
        if os.path.exists(pidfile):
            with open(pidfile) as f:
                print("Started pid=" + f.read().strip())
            break
        time.sleep(0.05)
    else:
        print("Failed to confirm daemon pid", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)

os.setsid()
if os.fork() > 0:
    sys.exit(0)

os.chdir(root)
os.umask(0)
sys.stdin.close()
fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(fd, 1)
os.dup2(fd, 2)
if fd > 2:
    os.close(fd)
with open(pidfile, "w") as f:
    f.write(str(os.getpid()))
os.execv(py, args)
PY

sleep 1
if healthy; then
  echo "login_http=200"
else
  echo "login_http=FAIL — see $LOG" >&2
  exit 1
fi
echo "URL=http://127.0.0.1:${PORT}/login"
echo "LOG=$LOG"
echo "PIDFILE=$PIDFILE"
