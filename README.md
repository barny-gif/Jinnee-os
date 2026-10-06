# Jinnee OS

An AI agent team for small businesses. One installer, one core, and your industry is just a folder.

```
curl -fsSL https://raw.githubusercontent.com/barny-gif/Jinnee-os/main/install.sh | bash
```
Windows: `irm https://raw.githubusercontent.com/barny-gif/Jinnee-os/main/install.ps1 | iex`

## What you get
- **Jinnee** – the team lead. You talk to it on Telegram in your own language; it delegates, reviews and reports. Rename it to anything you like.
- **General pack** – included in every install: invoice monitoring, email triage, competitor watch, weekly report.
- **Vertical packs** (optional): `ecom` (online shops), `photographer` (coming – bookings, client reminders).
- **Dashboard** – one page in the browser: what happened today, what needs your approval, whether the team is alive. Attach your own domain.
- **Connectors** – Billingo, Számlázz.hu, Gmail, Shopify, GLS… swappable in a few clicks.

## Layout
```
core/        the core: Jinnee, delegation, memory, autonomy levels, heartbeat, registry client
packs/       general (base) + vertical packs; a pack = agents/ + pack.json + onboarding.md
connectors/  external systems; same operation set within a category → swappable
dashboard/   simple web UI (FastAPI + one HTML file), domain via Caddy
registry/    index.json – served by jinnee-os.com; updates come from here
```

Autonomy levels (per agent and per action, `brain/autonomy_config.json`):
`0` forbidden · `1` approval required · `2` act, report afterwards · `3` free.
On a fresh install every action touching money or customers is 0 or 1.

## Requirements
- A Linux VPS (Docker) **or** a Mac/Linux/Windows machine that stays on
- A Claude Pro/Max subscription (Claude Code CLI runs on it) – your account, your cost
- A Telegram bot token (@BotFather, 2 minutes) and your own Telegram user ID (@userinfobot): the bot obeys that user only and does not start without it
- API keys for the connectors you use (`.env`)

## First run
1. `install.sh` asks for: VPS or local machine, Telegram token, your Telegram user ID, the agent's name, which pack.
   No terminal (CI, a provisioning script)? Pass the answers as variables; `claude login` is then left for you to run:
   `curl -fsSL …/install.sh | JINNEE_MODE=native TELEGRAM_BOT_TOKEN=… TELEGRAM_OWNER_ID=… bash`
   (`JINNEE_MODE`: `docker` or `native`; optional `JINNEE_NAME`, `PACKS=general,ecom`).
2. Message Jinnee on Telegram – it walks you through onboarding.
3. Dashboard: `http://localhost:8080` on the machine itself; from another device or on your domain, set a password first – see `dashboard/README.md`.
4. In week one you approve everything. Jinnee proposes raising the level of whatever proves reliable.

## Managed setup
Don't want to touch a terminal? Setup package + monthly plan at jinnee-os.com.

## License
MIT.
