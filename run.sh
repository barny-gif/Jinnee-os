#!/usr/bin/env bash
# Jinnee OS on this machine (not Docker): start, stop, restart, status. Never more than one copy of each program.
#   ./run.sh start | stop | restart | status
#
# After the machine restarts nothing starts by itself. One line in your crontab (`crontab -e`) takes care of it:
#   @reboot cd ~/jinnee-os && ./run.sh start
#
# logs/jinnee.log and logs/dashboard.log rotate at 1 MB (three old files are kept).
# logs/*.out holds what a program printed since its last start, including the reason if it did not come up.
set -u
cd "$(dirname "$0")" || exit 1
HERE="$(pwd -P)"
mkdir -p logs
export JINNEE_LOG_DIR="${JINNEE_LOG_DIR:-logs}"
PROGRAMS="jinnee:core/jinnee.py dashboard:dashboard/app.py"

runs(){ # runs PID SCRIPT: is PID alive and running SCRIPT? A number left in a pid file may belong to something else by now.
  case "$1" in ''|*[!0-9]*) return 1;; esac
  kill -0 "$1" 2>/dev/null || return 1
  case "$(ps -p "$1" -o args= 2>/dev/null)" in *"$2"*) return 0;; esac
  return 1
}

ours(){ # ours NAME SCRIPT → the pid on stdout, if the copy from the pid file is running
  local pid; pid="$(cat "logs/$1.pid" 2>/dev/null)" || return 1
  runs "$pid" "$2" && echo "$pid"
}

folder(){ # folder PID → the directory that process works in, when the system can tell
  if [ -e "/proc/$1/cwd" ]; then (cd "/proc/$1/cwd" 2>/dev/null && pwd -P)
  elif command -v lsof >/dev/null 2>&1; then lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'
  fi
}

halt(){ # halt PID: ask, wait up to 10 seconds, then insist
  local n=0
  kill "$1" 2>/dev/null || return 0
  while kill -0 "$1" 2>/dev/null && [ $n -lt 100 ]; do sleep 0.1; n=$((n+1)); done
  kill -9 "$1" 2>/dev/null || true
}

strays(){ # strays NAME SCRIPT: copies started from this folder without a pid file (by an installer before run.sh existed)
  local pid known
  command -v pgrep >/dev/null 2>&1 || return 0
  known="$(ours "$1" "$2" || true)"
  for pid in $(pgrep -f "$2" 2>/dev/null); do
    [ "$pid" = "$known" ] && continue
    case "$(ps -p "$pid" -o args= 2>/dev/null)" in *[Pp]ython*"$2"*) ;; *) continue;; esac  # not an editor with the file open
    [ "$(folder "$pid")" = "$HERE" ] || continue  # and not another install's copy
    echo "$1: stopping a copy started earlier (pid $pid)"; halt "$pid"
  done
}

start(){
  local p name script pid
  for p in $PROGRAMS; do
    name="${p%%:*}"; script="${p#*:}"
    strays "$name" "$script"
    if pid="$(ours "$name" "$script")"; then echo "$name is already running (pid $pid)"; continue; fi
    nohup python3 "$script" > "logs/$name.out" 2>&1 3<&- &
    echo $! > "logs/$name.pid"
    echo "$name started (pid $!)"
  done
}

stop(){
  local p name script pid
  for p in $PROGRAMS; do
    name="${p%%:*}"; script="${p#*:}"
    strays "$name" "$script"
    if pid="$(ours "$name" "$script")"; then halt "$pid"; echo "$name stopped"; else echo "$name was not running"; fi
    rm -f "logs/$name.pid"
  done
}

status(){
  local p name script pid code=0
  for p in $PROGRAMS; do
    name="${p%%:*}"; script="${p#*:}"
    if pid="$(ours "$name" "$script")"; then echo "$name is running (pid $pid)"
    else echo "$name is not running. Last output: logs/$name.out"; code=1; fi
  done
  return $code
}

case "${1:-}" in
  start) start;;
  stop) stop;;
  restart) stop; start;;
  status) status;;
  *) echo "Usage: ./run.sh start | stop | restart | status" >&2; exit 2;;
esac
