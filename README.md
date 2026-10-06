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

## Autonomy levels
Every kind of action has a level in `brain/autonomy_config.json`:
`0` forbidden (you do it yourself) · `1` asks you first · `2` does it, then tells you · `3` free.

Some actions are **locked**. A lock is a ceiling: however well the team does, it cannot move that action above the ceiling, and neither can a
click on the dashboard. On a fresh install everything that touches money or customers is locked: spending, ad budget, prices and refunds are
forbidden and stay forbidden; customer emails, invoices and social posts start at "asks you first" and can at most reach "does it, then tells
you", and only after you approve that step. The only way past a lock is for you to open the file and change it by hand, and Jinnee sends you a
Telegram message whenever the levels change, so nothing moves without you noticing. Anything the file does not mention is treated as "asks you first".

## Approvals
What needs your OK shows up on Telegram and on the dashboard: `/ok`, `/change <what>`, `/drop <why>`, or the buttons. Each item is decided once
and carried out once; you can `/undo` for two minutes. You are reminded once a day about anything waiting more than two days, and told if a
decision of yours led nowhere for six hours. Nothing is sent between 21:00 and 07:00 (`QUIET_HOURS` in `.env`; in Docker set `JINNEE_TZ` too).

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
4. In week one you approve everything. Jinnee proposes raising the level of whatever proves reliable; the proposal is an item you approve like any other.

## Keeping it running (installed directly on a machine)
- `./run.sh status | restart | stop | start` in the install folder. Running the installer again restarts; it never starts a second copy.
- After a reboot nothing starts by itself. Add one line with `crontab -e`: `@reboot cd ~/jinnee-os && ./run.sh start`
- Logs: `logs/jinnee.log`, `logs/dashboard.log`, rotated at 1 MB. If a program does not come up, the reason is in `logs/<name>.out`.
- Docker restarts by itself (`restart: unless-stopped`). Windows: see the note the installer prints; autostart there is still a manual Task Scheduler entry.

## Managed setup
Don't want to touch a terminal? Setup package + monthly plan at jinnee-os.com.

## License
MIT.
