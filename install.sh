#!/usr/bin/env bash
# Jinnee OS installer – Linux VPS (Docker), Linux desktop, macOS
#
# Works as `curl … | bash`: the questions are read from the terminal, not from the pipe the script arrives on.
# Without a terminal (CI, cron, ssh without -t) nothing can be asked, so the answers must be in the environment:
#   JINNEE_MODE=native TELEGRAM_BOT_TOKEN=… TELEGRAM_OWNER_ID=… bash install.sh
# A variable that is set is used instead of its question in a terminal too.
set -euo pipefail
REPO="https://github.com/barny-gif/Jinnee-os.git"
RAW="https://raw.githubusercontent.com/barny-gif/Jinnee-os/main/install.sh"
DIR="${JINNEE_DIR:-$HOME/jinnee-os}"
say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }
die(){ printf '\n%s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1; }

# --- answers: validators. Each one says what is acceptable; an answer that fails is asked again.
ok_mode(){ case "$1" in 1|2|docker|native) return 0;; esac; return 1; }
ok_token(){ case "$1" in ''|*[[:space:]]*|*\'*) return 1;; esac; }
ok_owner(){ case "$1" in ''|*[!0-9]*|0*) return 1;; esac; }
ok_name(){ case "$1" in ''|*\'*) return 1;; esac; }
ok_packs(){ case "$1" in ''|*[!a-z0-9_,-]*|,*|*,|*,,*) return 1;; esac; }

ask(){ # ask "question" → REPLY, read from the terminal (fd 3), surrounding spaces removed
  printf '%s ' "$1" >&2
  IFS= read -r REPLY <&3 || die "The terminal was closed before the question was answered. Nothing was installed."
  REPLY="${REPLY#"${REPLY%%[![:space:]]*}"}"; REPLY="${REPLY%"${REPLY##*[![:space:]]}"}"
}

answer(){ # answer VAR check "question" "what a good answer looks like" [default] → ANSWER
  local var="$1" check="$2" question="$3" hint="$4" default="${5:-}" given="${!1:-}"
  if [ -n "$given" ]; then
    if "$check" "$given"; then echo "  $var: taken from the environment"; ANSWER="$given"; return; fi
    [ "$TTY" = yes ] || die "$var is set, but not usable. $hint"
    echo "  $var is set, but not usable." >&2
  elif [ "$TTY" != yes ]; then
    [ -n "$default" ] || MISSING="$MISSING $var"
    ANSWER="$default"; return
  fi
  while :; do
    ask "$question"; ANSWER="${REPLY:-$default}"
    "$check" "$ANSWER" && return
    echo "  $hint" >&2
  done
}

no_terminal(){
  cat >&2 <<EOF

There is no terminal to ask the setup questions on, and these are not set:$MISSING

Set the answers as variables and run again. Nothing was installed.
  JINNEE_MODE         docker or native                               required
  TELEGRAM_BOT_TOKEN  the token @BotFather gave you                  required
  TELEGRAM_OWNER_ID   your numeric Telegram user ID (@userinfobot)   required
  JINNEE_NAME         what the agent is called                       default: Jinnee
  PACKS               general or general,ecom                        default: general

  curl -fsSL $RAW | JINNEE_MODE=native TELEGRAM_BOT_TOKEN=… TELEGRAM_OWNER_ID=… bash
EOF
  exit 1
}

set_env(){ # set_env KEY VALUE: rewrite the KEY= line of .env. The value is plain data here, never a pattern.
  local key="$1" val="$2" line found=no
  case "$val" in *[!A-Za-z0-9_.:,@/+-]*) val="'$val'";; esac  # single quotes: literal for core/env.py and Docker Compose
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in "$key="*) line="$key=$val"; found=yes;; esac
    printf '%s\n' "$line"
  done < .env > .env.new
  [ "$found" = yes ] || printf '%s\n' "$key=$val" >> .env.new
  cat .env.new > .env && rm -f .env.new
}

main(){
  say "Jinnee OS installer"
  case "$(uname -s)" in Linux) PLAT=linux;; Darwin) PLAT=mac;; *) echo "On Windows use install.ps1"; exit 1;; esac

  # Where do answers come from? stdin when it is a terminal; otherwise the controlling terminal
  # (stdin is the script itself under `curl … | bash`); otherwise only the environment.
  TTY=yes; MISSING=""
  if [ -t 0 ]; then exec 3<&0
  elif (exec 3</dev/tty) 2>/dev/null; then exec 3</dev/tty
  else TTY=no; fi

  # All questions first, so a run that cannot finish stops before it installs anything.
  answer JINNEE_MODE ok_mode "Where should it run? [1] VPS (Docker)  [2] directly on this machine  →" \
    "Answer 1 (Docker) or 2 (this machine). As a variable: JINNEE_MODE=docker or JINNEE_MODE=native."
  case "$ANSWER" in 1|docker) MODE=docker;; *) MODE=native;; esac
  answer TELEGRAM_BOT_TOKEN ok_token "Telegram bot token (@BotFather):" \
    "The token is required: message @BotFather on Telegram, send /newbot, and paste the token it gives you."
  TOKEN="$ANSWER"
  answer TELEGRAM_OWNER_ID ok_owner "Your Telegram user ID (@userinfobot):" \
    "A number is required, for example 123456789 (not your @username): message @userinfobot on Telegram and it replies with your ID. The bot obeys this user only and does not start without it."
  OWNER="$ANSWER"
  answer JINNEE_NAME ok_name "What should your agent be called? [Jinnee]" \
    "The name cannot contain a ' character." Jinnee
  NAME="$ANSWER"
  if [ -n "${PACKS:-}" ] || [ "$TTY" != yes ]; then
    answer PACKS ok_packs "Packs, comma separated (general or general,ecom):" \
      "Pack names separated by commas, for example general,ecom." general
  else
    while :; do
      ask "Pack: [1] general only  [2] general + ecom  →"
      case "${REPLY:-1}" in 1) ANSWER=general; break;; 2) ANSWER="general,ecom"; break;; esac
      echo "  Answer 1 or 2." >&2
    done
  fi
  PACK_LIST="$ANSWER"
  [ -z "$MISSING" ] || no_terminal
  # From here on .env is the only source: a rejected value must not reach the programs through the environment.
  unset JINNEE_MODE TELEGRAM_BOT_TOKEN TELEGRAM_OWNER_ID JINNEE_NAME PACKS

  need git || die "git is required. Install it and run again."
  if [ "$MODE" = docker ]; then
    need docker || { say "Installing Docker…"; curl -fsSL https://get.docker.com | sh; }
  else
    if ! need node; then
      say "Node.js is required for Claude Code. Installing…"
      if [ "$PLAT" = mac ]; then need brew || die "Homebrew required: https://brew.sh"; brew install node
      else curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs; fi
    fi
    need claude || { say "Installing Claude Code…"; npm install -g @anthropic-ai/claude-code; }
    need python3 || die "python3 is required."
  fi

  if [ -d "$DIR/.git" ]; then (cd "$DIR" && git pull -q); else git clone -q "$REPO" "$DIR"; fi
  cd "$DIR"
  [ -f .env ] || cp .env.example .env
  set_env TELEGRAM_BOT_TOKEN "$TOKEN"
  set_env TELEGRAM_OWNER_ID "$OWNER"
  set_env JINNEE_NAME "$NAME"
  set_env PACKS "$PACK_LIST"
  mkdir -p brain handoffs
  PACKS="$PACK_LIST" python3 core/pack_loader.py --init

  # `claude login` is interactive: it gets the terminal, not the pipe. Without a terminal it is left to the owner.
  LOGIN_LATER=""
  if [ "$MODE" = docker ]; then
    say "Starting in Docker…"; docker compose up -d --build
    if [ "$TTY" = yes ]; then say "Claude login inside the container (one time):"; docker compose exec jinnee claude login <&3 || true
    else LOGIN_LATER="cd $DIR && docker compose exec jinnee claude login"; fi
  else
    if [ "$TTY" = yes ]; then say "Claude login (one time):"; claude login <&3 || true
    else LOGIN_LATER="claude login"; fi
    pip3 install -q -r requirements.txt --break-system-packages 2>/dev/null || pip3 install -q -r requirements.txt
    nohup python3 core/jinnee.py >> logs.txt 2>&1 3<&- &
    nohup python3 dashboard/app.py >> logs.txt 2>&1 3<&- &
  fi
  say "Done. Message $NAME on Telegram and open: http://localhost:8080"
  [ -z "$LOGIN_LATER" ] || echo "One step is left, and it needs a terminal. $NAME cannot answer until it is done: $LOGIN_LATER"
  echo "Custom domain: dashboard/README.md"
}

# The whole file is read before anything runs: a download cut off half way does nothing.
main "$@"
