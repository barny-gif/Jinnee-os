# Jinnee OS

An AI agent team for small businesses. One installer, one core, and your industry is just a folder.

```
curl -fsSL https://raw.githubusercontent.com/barny-gif/Jinnee-os/main/install.sh | bash
```
Windows: `irm https://raw.githubusercontent.com/barny-gif/Jinnee-os/main/install.ps1 | iex`

## What you get
- **Jinnee** – the team lead. You talk to it on Telegram in your own language; it delegates, reviews and reports. Rename it to anything you like.
  You can send it photos (a delivery note, a damaged parcel, a screenshot), PDFs, Excel and text files; your conversation
  continues where it left off, also after a restart, until you send `/new`. `/status` tells you at once what it is doing.
- **General pack** – included in every install: invoice monitoring, email triage, competitor watch, weekly report.
- **Vertical packs** (optional): `ecom` (online shops), `photographer` (coming – bookings, client reminders).
- **Dashboard** – one page in the browser: what happened today, what needs your approval, whether the team is alive. Attach your own domain.
- **Connectors** – Billingo, Számlázz.hu, Gmail, Shopify, GLS… swappable in a few clicks.

## Layout
```
core/        the core: Jinnee, delegation, memory, autonomy levels, heartbeat, registry client, business settings, key store
config/      business.example.yaml (template); your own business.yaml stays here and out of git
packs/       general (base) + vertical packs; a pack = agents/ + pack.json + onboarding.md
connectors/  external systems; same operation set within a category → swappable
dashboard/   simple web UI (FastAPI + one HTML file), domain via Caddy
registry/    index.json – served by jinnee-os.com; updates come from here
tools/       guard.py – keeps business data out of this public repo (with .githooks/ and .github/workflows/);
             design_lint.py, visual_check.py – the design checks; skills_fetch.py – the skills not kept in git
.claude/     skills/ – design and email skills the agents use (third-party ones: THIRD_PARTY.md)
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
- **A reply reads the whole mail thread first.** Before Echo queues a reply to a customer, the thread gate reads every mail in the
  thread: a cancellation, return or complaint word, a reply someone from the shop wrote by hand (or a case already with you) in the
  last 72 hours, or several customer mails since the last answer, and there is no ready reply, only a "thread warning" with the
  reasons (which signal, which mail; no customer name or address). You decide; a reply follows only after your yes. Right before
  sending, the thread is read again: a mail that came in since stops the send.
- Never in the quiet hours, and never twice for the same time (also after a restart and on the nights the clocks change). A scheduled
  job does not start while Jinnee is working on your message; if one is already running when you write, Jinnee says so and your
  message is next. A failed run is reported to you once.
- **"Done" is checked, not believed.** A job that promises a file (Ledger's invoice watch, Scout's round, Muse's plan…) and did not
  write it, or answered with nothing at all, shows as **problem** in `/schedule` and you are told once, outside the quiet hours.

## Long work is not lost
One run of Claude is stopped after 10 minutes (`JINNEE_TIMEOUT` in `.env`). For anything longer, the agents write as they go into a
work file under `handoffs/<agent>/`, ticking each step. If a run is stopped half-way, you get where it got to instead of "I stopped":
which file, how many steps are done, what is next. Send `/continue` (or just "continue") and it goes on from that file, without
redoing or resending what is already done. A scheduled job that was stopped continues at its next run, or now with `/continue <job>`.
A run that ended normally but left a work file unfinished says so in one line at the end of its answer, and `/continue` finds that
file too; with several, it lists them (newest first) and you pick one with `/continue_2`. Files untouched for more than 7 days are no
longer offered. Nothing runs in the background: every agent works inside the run that answers you, so the answer reports finished
work (a pull request address, a test result), never "someone is working on it".
- Your time zone, quiet hours, another time or off per job: `schedule` in `config/business.yaml` (examples in
  `config/business.example.yaml`). `python3 core/scheduler.py plan` prints what would run when and starts nothing;
  `python3 core/scheduler.py check` checks your settings.

## Design and email skills
The agents run Claude Code in the install folder, so the skills in `.claude/skills/` are theirs to use. Third-party ones are pinned
to one upstream commit, with their licences next to them (`THIRD_PARTY.md`):
- **impeccable**: design, critique and polish of a page; its `detect` finds the template-made look. Forge may use it on its own.
- **emails**: planning and writing email sequences (welcome, post-purchase, win-back). Muse and Echo use it. It writes drafts only:
  every email to a customer is still a `send_customer_email` approval (locked, "asks you first").
- **awesome-design-md**: the design systems of 83 well-known products, as patterns when you say "make it feel like …". Never a brand to copy.
- **design-taste-frontend** (the taste skill): runs only when you start it by name, as `/design_taste_frontend <what to build>` on
  Telegram, because it has strong taste of its own (no purple, a random palette) that would fight your brand. Fetched at install time.

Before any page, shop section or email template reaches you, Forge runs three checks: `tools/design_lint.py` against your own
design rules in `brain/brand/DESIGN.md` (filled in during onboarding from your brandbook; the template is `config/DESIGN.example.md`),
Impeccable's `detect`, and `tools/visual_check.py`, which takes screenshots at phone, tablet and desktop size (`handoffs/visual/`)
and reports console errors, missing files and pages wider than the screen. Where a check disagrees with your brand, you decide.
- The screenshots need a headless browser: the installer sets it up (`python3 -m playwright install --with-deps chromium`; on Linux
  this may ask for your password once). `python3 tools/visual_check.py --self-check` says whether it works and what to run if not.
  `JINNEE_DESIGN_TOOLS=no` skips it (a small server where nobody builds pages).
- No usage data is sent: every agent run has `DO_NOT_TRACK=1` and `IMPECCABLE_NO_TELEMETRY=1`.

## Updating an install
Installed directly on a machine: `cd ~/jinnee-os && git pull && pip3 install -r requirements.txt && python3 core/pack_loader.py --init
&& python3 tools/skills_fetch.py sync && ./run.sh restart` (`--init` only adds what is missing and never overwrites your files; when a
release adds the screenshot browser, also `python3 -m playwright install --with-deps chromium`, and put the path that
`python3 tools/visual_check.py --self-check` prints into `.env` as `CHROME_PATH=`). In Docker: `git pull && docker compose up -d --build`.
An install from before the key store: after the update, open the dashboard's **Your keys** box and press *Move them into the
encrypted store* (or `python3 core/vault.py import`; in Docker `docker compose exec jinnee python core/vault.py import`), then
restart (`./run.sh restart`, or `docker compose up -d`). In Docker the master key is then in `./keys`, inside the install folder:
move it out as "Your keys" says.

## Requirements
- A Linux VPS (Docker) **or** a Mac/Linux/Windows machine that stays on
- A Claude Pro/Max subscription (Claude Code CLI runs on it) – your account, your cost
- A Telegram bot token (@BotFather, 2 minutes) and your own Telegram user ID (@userinfobot): the bot obeys that user only and does not start without it
- API keys for the connectors you use (pasted on the dashboard, kept encrypted: "Your keys" below)

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
  --goods "1× Wooden puzzle" --cod 7350` prints a label note exactly as it would go to GLS, without sending anything.
- A value a step needs and that is missing **stops that step**: no parcel label and no invoice list goes out with a guessed value,
  and the error names the key to set. The team tells you when the file is missing, instead of inventing your name or tone.
- What goes where: `.env` holds *how the system connects* (the Shopify store handle, the GLS pickup address, VAT; the API keys
  and tokens themselves go to the encrypted key store, see "Your keys"); `config/business.yaml` holds *who you are* and what goes on your documents. `JINNEE_LANG` in `.env` still works and
  wins over `business.language`; `BILLINGO_PAYMENTS_PREFIX` is no longer read (an old value that differs from
  `billing.invoice_prefix` stops the Payments view until you delete the `.env` line).
- **The system's own words follow the language too.** What the bridge and the dashboard say by themselves (alerts, `/status`,
  `/schedule`, `/pending`, the approve/undo answers, error messages, the dashboard's labels) comes from `core/locale/<lang>.json`.
  `hu` and `en` are built in; any other language gets these in English, and adding `core/locale/de.json` (same keys as
  `en.json`) adds one. `python3 core/texts.py check` lists what a language file is missing. The commands (`/ok`, `/pending`…)
  stay the same in every language, and so do the logs and the agents' instructions.

## Cash on delivery: invoice at delivery, paid after the transfer
The shop pack follows one rule for cash-on-delivery (COD) orders; a paid order (card, transfer) keeps its usual path.
1. **New order:** the invoice is made as a Billingo *draft* and the GLS label is uploaded (as far as the autonomy table allows).
   You get one line on Telegram and the dashboard: "Anna, a new order came in: #1001, cash on delivery, 12 345 Ft. The invoice
   draft and the GLS label are ready." If a field is missing, it says which one (never the data).
2. **GLS picks it up:** "GLS picked up the parcel of #1001."
3. **The customer takes it (GLS status 05):** only now may the COD invoice be issued and e-mailed. With `issue_invoice` at
   level 1 (the default) you get a question per order ("#1001: the customer took the parcel. May I issue and send the
   invoice?"); at level 2 it is issued and sent by itself and you are told afterwards. **Before delivery a COD invoice is never
   issued**: `connectors/order_flow.py invoice_issue` checks the GLS state itself and refuses, whoever asks. A parcel that went
   back stays a draft.
4. **GLS transfers the money:** only then is the order set *paid* in Shopify (`SHOPIFY_AUTO_MARK_PAID`, off by default). Delivered
   is not enough. The proof is GLS's own COD settlement e-mail, read by the carrier ledger (`GLS_LEDGER_ACCOUNT`): the parcel
   must be on it with the same amount, matched to the order without doubt. Two rows for one order, a parcel under another
   reference, or a different amount: nothing is set, you get a message. Still not settled after `COD_TRANSFER_WARN_DAYS` (10): a
   reminder to ask GLS.

Each message is said once; nothing is sent in the quiet hours. Switches in `.env`: `ORDER_NOTICES=off` (no news),
`COD_INVOICE_ON_DELIVERY=off` (no question or automatic issue at delivery; the gate in step 3 stays). How you are addressed:
`business.owner_name` in `config/business.yaml` (optional).

## Your keys
API keys and tokens (Telegram, Shopify, Billingo, Gmail, GLS…) are not kept in plain text. They are in an encrypted key store,
`config/secrets.vault.json` (AES-256-GCM, each value bound to its name), and the programs read them when they start (`core/env.py`
reads `.env` first, then the store; a variable already set in the environment still wins, an empty `KEY=` line does not).
- **Adding or changing a key:** the dashboard's **Your keys** box. Pick the name, paste the key, *Store encrypted*. The page never
  shows a key again, only where it is (stored encrypted / in `.env`, readable / not set). The team's next run uses it; Jinnee's
  own Telegram connection needs a restart for a new bot token. Without the dashboard: `python3 core/vault.py set NAME` (asks for the
  value without showing it; never put a key on the command line, it stays in the shell history).
- **The installer** asks for the Telegram token as before, puts it into the store, and leaves `.env` without it.
- **Keys left in `.env`** (an older install, or one typed in by hand): the box lists them in red with a button to move them;
  after your OK they are encrypted, checked to read back the same, and only then removed from `.env`. No plain copy is kept.
- **The check:** `python3 core/vault.py doctor` (and the same box) reports any key still readable: a line in `.env`, a `.env`
  copy saved under another name (`.env.bak`, `.env~`, a typo), a stored key's value pasted into a note, a log or the team's memory,
  and files other users of the machine can read. It names the file and the key, never the value.
- **The master key** is 32 random bytes in a file only you can read, kept **outside the install folder**, so a copy, zip or backup
  of the folder carries no key: `~/.config/jinnee-os/master.key` directly on a machine (`%APPDATA%\jinnee-os` on Windows); in
  Docker the installer writes that folder into `.env` as `JINNEE_KEY_DIR` and `docker-compose.yml` mounts it at `/run/jinnee-keys`.
  **Back it up** somewhere else (a password manager): without it the stored keys cannot be read, only typed in again. To move it
  out of an older Docker install: `mkdir -p ~/.config/jinnee-os && sudo mv keys/master.key ~/.config/jinnee-os/ && sudo chown
  "$USER" ~/.config/jinnee-os/master.key && chmod 600 ~/.config/jinnee-os/master.key`, add `JINNEE_KEY_DIR=$HOME/.config/jinnee-os`
  (written out in full) to `.env`, then `docker compose up -d`.
- **What it protects, honestly.** On a VPS the master key and the store are on the same disk: there is no hardware key safe. So
  this guards against *copies and leaks of files*: a backup or zip, a mistyped save, a commit, a screen share, an agent or a person
  reading the folder. It does **not** guard against someone who can read the master key file as well: root on the server, a stolen
  disk image, or any program running as the same user (in Docker: anything inside the container, the agents included). The running
  programs hold the keys in memory and in their environment, as every setup must. For a VPS, an encrypted disk (LUKS) adds the next
  layer.

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
