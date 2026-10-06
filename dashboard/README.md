# Dashboard

One page: Today · Awaiting your decision · Decided, not carried out yet · Recently closed · Team · Connections · What the team may do · Decision log.
The owner works on Telegram; this exists so they can *see*.

- Runs at `http://localhost:8080` (in Docker it's bound to the VPS only; Caddy exposes it).
- **Approve / Change / Drop** record your decision (through `core/approvals.py`). Change asks what to change; Drop asks why, and the reason
  is saved in `brain/lessons.md` for the team. The team picks a decision up about two minutes later, without you writing on Telegram.
- **Undo** is there until the decision has been picked up. After that the item is closed and shows what came of it.
- One decision per item: a second click, another tab or a repeated request gets "already approved" (HTTP 409) and changes nothing.
- "Not sendable as it is" means the text is empty or still marked as a draft. You can approve it, but it will not go out; the team fixes it
  and asks again.
- "What the team may do" shows the autonomy levels and which are locked. It is read only: there is nothing on the dashboard that changes a
  level. A request to raise one arrives as an ordinary item; a lock can only be lifted by editing `brain/autonomy_config.json` yourself.

## Access
- **No password** (`DASHBOARD_PASSWORD` empty): this machine only. The dashboard listens on `127.0.0.1` and refuses anything
  that is not a direct request to `localhost` – other devices, and anything relayed by Caddy or a tunnel, get a 403.
  On a VPS you can still look at it through an SSH tunnel: `ssh -L 8080:127.0.0.1:8080 you@vps`.
- **With a password** (`.env` → `DASHBOARD_PASSWORD=something`, restart): a sign-in page, then a session cookie for 30 days
  (or until the dashboard restarts). The dashboard listens on all interfaces, so it is reachable on your local network
  at `http://<ip>:8080`. That is plain HTTP: fine at home, not on a network you don't trust. Use a domain (below) for HTTPS.
- The password no longer goes in the URL: `?key=…` is ignored and old bookmarks land on the sign-in page.
- Scripts: send the password in the `x-dash-key` header.
  `curl -H "x-dash-key: something" http://localhost:8080/api/state`
- `DASHBOARD_HOST` overrides the listen address. Set a password before pointing a domain or a tunnel at the dashboard;
  the no-password mode recognises a proxy by the headers it adds and is not a substitute for one.

## Your own domain (VPS)
1. DNS: `jinnee.yourbusiness.com` A record → VPS IP.
2. `sudo apt install caddy`
3. Edit the domain in the repo's `Caddyfile`: `sudo cp Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy`
4. Set `DASHBOARD_PASSWORD` in `.env` and restart – without it the domain answers 403.
5. Done: automatic HTTPS at `https://jinnee.yourbusiness.com`.

On a Mac/Windows machine the dashboard is visible on that machine only, or on the local network once a password is set. From outside: Cloudflare Tunnel (free) – guide in the registry docs later.
