# core

| file | purpose |
|---|---|
| `jinnee.md` | the team lead's persona (renamable; the rules are not) |
| `jinnee.py` | entry point: Telegram bridge + Claude Code call + morning brief |
| `env.py` | reads `.env` at startup for native installs; a variable already set in the environment wins |
| `pack_loader.py` | loads packs from `PACKS`, merges autonomy defaults and connector lists |
| `heartbeat.py` | agent liveness in `brain/heartbeat.json`; the lead flags anyone who went quiet |
| `registry_client.py` | checks `REGISTRY_URL` daily; only reports updates, never installs them on its own |

The core knows nothing about any industry. Everything that is "shop" or "photographer" lives under `packs/`.

State lives in `brain/` as plain files so the owner can read them:
`company.md` · `lessons.md` · `decisions.log.md` · `autonomy_config.json` · `approvals.json` · `heartbeat.json` · `brief_today.md`
