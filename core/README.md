# core

| file | purpose |
|---|---|
| `jinnee.md` | the team lead's persona (renamable; the rules are not) |
| `jinnee.py` | entry point: Telegram bridge + Claude Code call + morning brief; records the owner's `/ok` `/change` `/drop` `/undo` itself |
| `approvals.py` | the approval queue and the only writer of `brain/approvals.json`: pending → decided → consumed, each once |
| `autonomy.py` | autonomy levels with locks; the one gate (`allowed(action) → level, needs_approval, reason`) |
| `alerts.py` | what the owner is told unasked: new items, reminders, stuck decisions; quiet hours; state in `brain/alerts_state.json` |
| `store.py` | one lock for everything that writes `brain/*.json`, atomic writes, reads that survive a broken file |
| `logs.py` | rotating log files for a native install |
| `env.py` | reads `.env` at startup for native installs; a variable already set in the environment wins |
| `pack_loader.py` | loads packs from `PACKS`, merges autonomy defaults and connector lists |
| `heartbeat.py` | agent liveness in `brain/heartbeat.json`: working / ok / failed; flags a run that never finished or failed |
| `registry_client.py` | checks `REGISTRY_URL` daily; only reports newer versions, never installs them on its own |

The core knows nothing about any industry. Everything that is "shop" or "photographer" lives under `packs/`.

State lives in `brain/` as plain files so the owner can read them:
`company.md` · `lessons.md` · `decisions.log.md` · `autonomy_config.json` · `approvals.json` · `alerts_state.json` · `heartbeat.json` · `brief_today.md`

## Who may write what
- `approvals.json`, `autonomy_config.json`, `alerts_state.json`, `heartbeat.json`: only through the modules above. Agents use the command lines
  (`python3 core/approvals.py …`, `core/autonomy.py …`, `core/heartbeat.py beat …`); `jinnee.py` starts Claude Code with exactly those three
  commands allowed in the shell and with edits to the first three files denied.
- `autonomy_config.json` is also the owner's to edit by hand; that is the only way to lift a lock. The owner gets a Telegram message whenever
  the levels change, whoever changed them.
- There is no command that approves. A decision comes from the dashboard or from the owner's Telegram command, both handled in plain code.

This is a guard rail on a file-based system, not a sandbox: an owner whose own Claude Code settings allow every shell command, or an agent in
developer mode that rewrites `core/`, is outside what it can stop.

## An approval item
`id` · `agent` · `action` (the autonomy key) · `title` · `summary` · `text` or `file` (what goes out; `outbound: false` for background material) ·
`payload` · `replaces` · `created_at` · `state` · `decision` · `note` · `decided_by` · `decided_at` · `sendable` · `blocked` · `sha` (fingerprint of
what was approved) · `consumed_at` · `outcome` (executing, done, failed, returned, dropped, superseded, withdrawn, closed) · `result` · `finished_at`.
Closed items leave the file after 30 days; `decisions.log.md` keeps their lines.
