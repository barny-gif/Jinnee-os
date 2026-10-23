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
core/        the core: Jinnee, delegation, memory, autonomy levels, heartbeat, registry client, business settings
config/      business.example.yaml (template); your own business.yaml stays here and out of git
packs/       general (base) + vertical packs; a pack = agents/ + pack.json + onboarding.md
connectors/  external systems; same operation set within a category → swappable
dashboard/   simple web UI (FastAPI + one HTML file), domain via Caddy
registry/    index.json – served by jinnee-os.com; updates come from here
tools/       guard.py – keeps business data out of this public repo (with .githooks/ and .github/workflows/)
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

## The team's own rhythm
The agents also work without being asked, at the times their pack promises: the morning brief at 08:00, Milo through your own inbox
and Echo through customer mail every hour, Ledger's invoice watch every morning and finance line on Friday, Scout's competitor round on
Thursday, and with the shop pack Milo's hourly order watch and daily stock alert, Muse's Monday plan and Friday measurement, Spark's
weekly calendar, and a Friday report from the lead. `/schedule` on Telegram (and the Schedule box on the dashboard) shows each one: when
it runs next, when it last ran, and how it went.
- **A job whose connector is not set up does not run.** It shows as off, with the setting it is missing.
- **Nothing goes out from a scheduled run**, whatever the autonomy level: no e-mail, post, invoice, label or shop change. What should go
  out is put in front of you for approval, as always. A run that has nothing to do (no new mail, no open orders) costs no Claude run at all.
- Never in the quiet hours, and never twice for the same time (also after a restart and on the nights the clocks change). A scheduled
  job does not start while Jinnee is working on your message; if one is already running when you write, Jinnee says so and your
  message is next. A failed run is reported to you once.
- Your time zone, quiet hours, another time or off per job: `schedule` in `config/business.yaml` (examples in
  `config/business.example.yaml`). `python3 core/scheduler.py plan` prints what would run when and starts nothing;
  `python3 core/scheduler.py check` checks your settings.

## Requirements
- A Linux VPS (Docker) **or** a Mac/Linux/Windows machine that stays on
- A Claude Pro/Max subscription (Claude Code CLI runs on it) – your account, your cost
- A Telegram bot token (@BotFather, 2 minutes) and your own Telegram user ID (@userinfobot): the bot obeys that user only and does not start without it
- API keys for the connectors you use (`.env`)

## First run
1. `install.sh` asks for: VPS or local machine, Telegram token, your Telegram user ID, the agent's name, which pack, and then your
   business: its name, the language you and your customers use, how the team should sound, and (with the ecom pack) the shop name for
   the parcel label. The business answers go to `config/business.yaml` (see "Your business settings" below).
   No terminal (CI, a provisioning script)? Pass the answers as variables; `claude login` is then left for you to run:
   `curl -fsSL …/install.sh | JINNEE_MODE=native TELEGRAM_BOT_TOKEN=… TELEGRAM_OWNER_ID=… BUSINESS_NAME=… BUSINESS_VOICE=… bash`
   (`JINNEE_MODE`: `docker` or `native`; optional `JINNEE_NAME`, `PACKS=general,ecom`, `BUSINESS_LANGUAGE` (default `hu`),
   `BUSINESS_INTRO`; with ecom also `SHOP_LABEL_NAME=…`, optional `SHOP_NOT_REVENUE_TAGS`, `BILLING_INVOICE_PREFIX`).
   Already have a filled-in file? `BUSINESS_FILE=/path/to/business.yaml` instead of the `BUSINESS_*` answers.
2. Message Jinnee on Telegram – it walks you through onboarding.
3. Dashboard: `http://localhost:8080` on the machine itself; from another device or on your domain, set a password first – see `dashboard/README.md`.
4. In week one you approve everything. Jinnee proposes raising the level of whatever proves reliable; the proposal is an item you approve like any other.

## Your business settings
Everything that is about *your* business lives in one file on your machine, `config/business.yaml`, and never in the code:
the business name, the language, the tone of voice and a short intro (the team reads these on every run), the note printed on
the parcel label, which invoice series the Payments view shows, and which order tags are not revenue (test, gift, barter...).
- The repo only has `config/business.example.yaml`, with made-up values. Your own file is in `.gitignore`, so it never goes to GitHub.
- Change it any time; the programs read it again on the next use. In Docker the `config/` folder is mounted, no rebuild needed.
- `python3 core/business.py check` says whether everything your packs need is there. `python3 core/business.py setup` asks the
  questions again (it does not overwrite an existing file without `--force yes`). `python3 core/business.py label --order "#1001"
  --goods "1× Wooden puzzle" --cod 9890` prints a label note exactly as it would go to GLS, without sending anything.
- A value a step needs and that is missing **stops that step**: no parcel label and no invoice list goes out with a guessed value,
  and the error names the key to set. The team tells you when the file is missing, instead of inventing your name or tone.
- What goes where: `.env` holds *how the system connects* (API keys, tokens, the Shopify store handle, the GLS pickup address,
  VAT); `config/business.yaml` holds *who you are* and what goes on your documents. `JINNEE_LANG` in `.env` still works and
  wins over `business.language`; `BILLINGO_PAYMENTS_PREFIX` is no longer read (an old value that differs from
  `billing.invoice_prefix` stops the Payments view until you delete the `.env` line).

## Keeping your business out of the repo
This repo is public. Two checks keep a business's own data out of it:
- **GitHub Actions** (`.github/workflows/guard.yml`) checks every push and pull request with generic patterns (`tools/guard.py`):
  phone numbers, e-mail addresses, real `*.myshopify.com` store handles and access tokens in the added lines, plus order numbers
  (`#1xxx`, "order 1234") and money amounts (`… Ft`, `… HUF`) in commit messages. Old commits are not checked or rewritten.
- **Local git hooks** (`.githooks/pre-commit`, `.githooks/commit-msg`) run the same patterns before a commit is made, plus your
  own words from `config/private_patterns.txt`: your shop name and domain, product names, invoice series, your own name. That
  file is in `.gitignore`: your list of words is never published. Turn the hooks on once per clone:
  `python3 tools/guard.py install-hooks`. One entry per line, `#` for comments, `re:` in front for a regular expression:
  ```
  # config/private_patterns.txt
  my-shop.hu
  My Shop Kft.
  re:\bMS-\d
  ```
- Tests and examples use made-up values only (`example-shop.hu`, `Example Kft.`, numbers like `06 30 123 4567`).
  `python3 tools/guard.py scan` lists anything in the files that the patterns would refuse.

## Keeping it running (installed directly on a machine)
- `./run.sh status | restart | stop | start` in the install folder. Running the installer again restarts; it never starts a second copy.
- After a reboot nothing starts by itself. Add one line with `crontab -e`: `@reboot cd ~/jinnee-os && ./run.sh start`
- Logs: `logs/jinnee.log`, `logs/dashboard.log`, rotated at 1 MB. If a program does not come up, the reason is in `logs/<name>.out`.
- Docker restarts by itself (`restart: unless-stopped`). Windows: see the note the installer prints; autostart there is still a manual Task Scheduler entry.

## Managed setup
Don't want to touch a terminal? Setup package + monthly plan at jinnee-os.com.

## License
MIT.
