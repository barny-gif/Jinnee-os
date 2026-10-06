#!/usr/bin/env bash
# Jinnee OS installer – Linux VPS (Docker), Linux desktop, macOS
set -euo pipefail
REPO="https://github.com/barny-gif/Jinnee-os.git"
DIR="${JINNEE_DIR:-$HOME/jinnee-os}"
say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }
ask(){ local v; read -r -p "$1 " v; echo "$v"; }
need(){ command -v "$1" >/dev/null 2>&1; }

say "Jinnee OS installer"
case "$(uname -s)" in Linux) PLAT=linux;; Darwin) PLAT=mac;; *) echo "On Windows use install.ps1"; exit 1;; esac
MODE="$(ask "Where should it run? [1] VPS (Docker)  [2] directly on this machine  →")"
need git || { say "git is required. Install it and run again."; exit 1; }

if [ "$MODE" = "1" ]; then
  need docker || { say "Installing Docker…"; curl -fsSL https://get.docker.com | sh; }
else
  if ! need node; then
    say "Node.js is required for Claude Code. Installing…"
    if [ "$PLAT" = mac ]; then need brew || { echo "Homebrew required: https://brew.sh"; exit 1; }; brew install node
    else curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs; fi
  fi
  need claude || { say "Installing Claude Code…"; npm install -g @anthropic-ai/claude-code; }
  need python3 || { echo "python3 is required."; exit 1; }
fi

if [ -d "$DIR/.git" ]; then (cd "$DIR" && git pull -q); else git clone -q "$REPO" "$DIR"; fi
cd "$DIR"
[ -f .env ] || cp .env.example .env

say "Setup"
TOKEN="$(ask "Telegram bot token (@BotFather):")"
OWNER="$(ask "Your Telegram user ID (@userinfobot):")"
NAME="$(ask "What should your agent be called? [Jinnee]")"; NAME="${NAME:-Jinnee}"
PACK="$(ask "Pack: [1] general only  [2] general + ecom  →")"
PACKS=general; [ "$PACK" = "2" ] && PACKS="general,ecom"
sed -i.bak -e "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=$TOKEN|" \
           -e "s|^TELEGRAM_OWNER_ID=.*|TELEGRAM_OWNER_ID=$OWNER|" \
           -e "s|^JINNEE_NAME=.*|JINNEE_NAME=$NAME|" \
           -e "s|^PACKS=.*|PACKS=$PACKS|" .env && rm -f .env.bak
mkdir -p brain handoffs
PACKS="$PACKS" python3 core/pack_loader.py --init

if [ "$MODE" = "1" ]; then
  say "Starting in Docker…"; docker compose up -d --build
  say "Claude login inside the container (one time):"; docker compose exec jinnee claude login || true
else
  say "Claude login (one time):"; claude login || true
  pip3 install -q -r requirements.txt --break-system-packages 2>/dev/null || pip3 install -q -r requirements.txt
  nohup python3 core/jinnee.py >> logs.txt 2>&1 &
  nohup python3 dashboard/app.py >> logs.txt 2>&1 &
fi
say "Done. Message $NAME on Telegram and open: http://localhost:8080"
echo "Custom domain: dashboard/README.md"
