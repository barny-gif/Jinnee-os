#!/usr/bin/env bash
# Jinnee OS installer – Linux VPS (Docker), Linux desktop, macOS
#
# Works as `curl … | bash`: the questions are read from the terminal, not from the pipe the script arrives on.
# Without a terminal (CI, cron, ssh without -t) nothing can be asked, so the answers must be in the environment:
#   JINNEE_MODE=native TELEGRAM_BOT_TOKEN=… TELEGRAM_OWNER_ID=… BUSINESS_NAME=… BUSINESS_VOICE=… bash install.sh
# (with the ecom pack also SHOP_LABEL_NAME=…), or BUSINESS_FILE=/path/to/your/business.yaml instead of the BUSINESS_* answers.
# A variable that is set is used instead of its question in a terminal too.
# Your business answers go to config/business.yaml, which never goes to git (see config/business.example.yaml).
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
ok_text(){ [ -n "$1" ]; }
ok_lang(){ case "$1" in [a-z][a-z]|[a-z][a-z][a-z]|[a-z][a-z]-[A-Za-z][A-Za-z]) return 0;; esac; return 1; }
ok_tz(){ case "$1" in *[!A-Za-z0-9_/+-]*|/*|*/) return 1;; UTC|*/*) return 0;; esac; return 1; }  # Area/City, checked again by core/scheduler.py check

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

optional(){ # optional VAR "question" → ANSWER: the variable if set, else asked once in a terminal (Enter = empty), else empty
  local given="${!1:-}"
  if [ -n "$given" ]; then echo "  $1: taken from the environment"; ANSWER="$given"; return; fi
  ANSWER=""; [ "$TTY" = yes ] || return 0
  ask "$2"; ANSWER="$REPLY"
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
  BUSINESS_NAME       your business name, as customers know it       required
  BUSINESS_VOICE      how the team sounds to your customers          required
  BUSINESS_LANGUAGE   language for you and your customers            default: hu
  BUSINESS_INTRO      a sentence or two about the business           optional
  BUSINESS_TIMEZONE   your time zone, for the schedule               default: Europe/Budapest
  SHOP_LABEL_NAME     shop name on the parcel label note             required with the ecom pack
  SHOP_NOT_REVENUE_TAGS, BILLING_INVOICE_PREFIX                      optional (ecom), see config/business.example.yaml
  BUSINESS_FILE       a ready config/business.yaml to use instead of the BUSINESS_* answers

  curl -fsSL $RAW | JINNEE_MODE=native TELEGRAM_BOT_TOKEN=… TELEGRAM_OWNER_ID=… BUSINESS_NAME=… BUSINESS_VOICE=… bash
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

  # Your business: name, language, tone of voice (and for a shop, the parcel label). It goes to config/business.yaml, never to git.
  BIZ=ask; B_NAME=""; B_LANG=""; B_VOICE=""; B_INTRO=""; B_TZ=""; B_SHOP=""; B_TAGS=""; B_PREFIX=""
  if [ -f "$DIR/config/business.yaml" ]; then BIZ=keep
  elif [ -n "${BUSINESS_FILE:-}" ]; then
    [ -f "$BUSINESS_FILE" ] || die "BUSINESS_FILE=$BUSINESS_FILE: no such file. Nothing was installed."
    BIZ=file; B_FILE="$(cd "$(dirname "$BUSINESS_FILE")" && pwd)/$(basename "$BUSINESS_FILE")"
  fi
  if [ "$BIZ" = ask ]; then
    answer BUSINESS_NAME ok_text "Your business name, as customers know it:" "A name is required: the team uses it with your customers."
    B_NAME="$ANSWER"
    answer BUSINESS_LANGUAGE ok_lang "Language for you and your customers (hu, en, de…) [hu]:" "A language code like hu, en or de." hu
    B_LANG="$ANSWER"
    answer BUSINESS_VOICE ok_text "How should the team sound to your customers? (e.g. warm, informal, short sentences):" \
      "A few words are required, for example: friendly, informal, no exclamation marks."
    B_VOICE="$ANSWER"
    optional BUSINESS_INTRO "One or two sentences about the business (Enter to skip):"; B_INTRO="$ANSWER"
    answer BUSINESS_TIMEZONE ok_tz "Your time zone, for the schedule [Europe/Budapest]:" "A name like Europe/Budapest or Europe/Vienna." Europe/Budapest
    B_TZ="$ANSWER"
    case ",$PACK_LIST," in *,ecom,*)
      answer SHOP_LABEL_NAME ok_text "Shop name for the parcel label note (e.g. your-shop.com):" \
        "Required with the ecom pack: it is the first thing on every GLS label note."
      B_SHOP="$ANSWER"
      optional SHOP_NOT_REVENUE_TAGS "Order tags that are not revenue, comma separated (e.g. test,gift; Enter = none):"; B_TAGS="$ANSWER"
      optional BILLING_INVOICE_PREFIX "Show only invoice numbers starting with (Enter = every series):"; B_PREFIX="$ANSWER";;
    esac
  fi
  [ -z "$MISSING" ] || no_terminal
  # From here on .env is the only source: a rejected value must not reach the programs through the environment.
  unset JINNEE_MODE TELEGRAM_BOT_TOKEN TELEGRAM_OWNER_ID JINNEE_NAME PACKS
  unset BUSINESS_NAME BUSINESS_LANGUAGE BUSINESS_VOICE BUSINESS_INTRO BUSINESS_TIMEZONE SHOP_LABEL_NAME SHOP_NOT_REVENUE_TAGS BILLING_INVOICE_PREFIX BUSINESS_FILE

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
  case "$BIZ" in
    keep) echo "  config/business.yaml is already here: kept as it is.";;
    file) mkdir -p config; cp "$B_FILE" config/business.yaml; echo "  config/business.yaml: copied from BUSINESS_FILE";;
    ask) PACKS="$PACK_LIST" BUSINESS_NAME="$B_NAME" BUSINESS_LANGUAGE="$B_LANG" BUSINESS_VOICE="$B_VOICE" BUSINESS_INTRO="$B_INTRO" BUSINESS_TIMEZONE="$B_TZ" \
           SHOP_LABEL_NAME="$B_SHOP" SHOP_NOT_REVENUE_TAGS="$B_TAGS" BILLING_INVOICE_PREFIX="$B_PREFIX" \
           python3 core/business.py init >/dev/null || die "config/business.yaml could not be written."
         echo "  config/business.yaml written (your business settings; edit any time, check with: python3 core/business.py check)";;
  esac

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
    bash run.sh restart  # running it again replaces the running copy; it never adds a second one
  fi
  say "Done. Message $NAME on Telegram and open: http://localhost:8080"
  [ -z "$LOGIN_LATER" ] || echo "One step is left, and it needs a terminal. $NAME cannot answer until it is done: $LOGIN_LATER"
  [ "$MODE" = docker ] || echo "After a reboot: cd $DIR && ./run.sh start   (to make that automatic: README, \"Keeping it running\")"
  echo "Custom domain: dashboard/README.md"
}

# The whole file is read before anything runs: a download cut off half way does nothing.
main "$@"
