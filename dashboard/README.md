# Dashboard

One page, five parts: Today · Awaiting approval · Team · Connections · Decision log.
The owner works on Telegram; this exists so they can *see*.

- Runs at `http://localhost:8080` (in Docker it's bound to the VPS only; Caddy exposes it).
- Password: `.env` → `DASHBOARD_PASSWORD=something`, then `?key=something` at the end of the URL.
- The buttons write `brain/approvals.json`; Jinnee reads and executes on its next turn.

## Your own domain (VPS)
1. DNS: `jinnee.yourbusiness.com` A record → VPS IP.
2. `sudo apt install caddy`
3. Edit the domain in the repo's `Caddyfile`: `sudo cp Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy`
4. Done: automatic HTTPS at `https://jinnee.yourbusiness.com`.

On a Mac/Windows machine the dashboard is visible on the local network only. From outside: Cloudflare Tunnel (free) – guide in the registry docs later.
